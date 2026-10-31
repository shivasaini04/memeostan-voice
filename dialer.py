"""
The dialer: one worker that drains call_queue.

Both ways of asking for a call end up here. A webhook enqueues one row, a CSV
enqueues five hundred; the worker does not know or care which. That is the whole
reason the queue exists — the compliance gate, retries, calling window and
outcome write-back are written once instead of twice.

Run alongside the server:

    python dialer.py            # drain continuously
    python dialer.py --once     # single pass, useful in tests
"""
import os
import sys
import json
import base64
import asyncio
import logging
import urllib.parse
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

from dotenv import load_dotenv

import db
from contact_gate import run_contact_gate, is_trai_calling_hour

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
logger = logging.getLogger("dialer")

POLL_SECONDS = float(os.getenv("DIALER_POLL_SECONDS", "5"))
RETRY_MINUTES = int(os.getenv("DIALER_RETRY_MINUTES", "20"))
# Exotel trial accounts reject unverified destinations; treat that as terminal
# rather than retrying a call that can never succeed.
TERMINAL_HTTP = {400, 401, 403}


def tenant_telephony(tenant_id: int) -> Dict[str, str]:
    """Only the selected operator's explicit database credentials."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM tenant WHERE id = ?", (tenant_id,)).fetchone()
    return {k: (row[column] or "").strip() if row else "" for k, column in {
        "account_sid": "exotel_account_sid", "api_key": "exotel_api_key",  # pragma: allowlist secret
        "api_token": "exotel_api_token", "caller_id": "exotel_caller_id",
        "flow_url": "exotel_flow_url"}.items()} | {
            "subdomain": os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")}


def place_call(cfg: Dict[str, str], phone_e164: str,
               custom: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Exotel to dial. Returns {ok, sid|error, http}."""
    gate = run_contact_gate(phone_e164)
    if not gate.is_approved:
        return {"ok": False, "error": gate.reason, "http": None, "terminal": True}
    if cfg.get("subdomain") not in {"api.exotel.com", "api.in.exotel.com", "api.exotel.in", "api.sg.exotel.com"}:
        return {"ok": False, "error": "Unsupported Exotel API hostname", "http": None, "terminal": True}
    missing = [k for k in ("account_sid", "api_key", "api_token", "caller_id", "flow_url")
               if not cfg.get(k)]
    if missing:
        return {"ok": False, "error": f"telephony not configured: {', '.join(missing)}",
                "http": None, "terminal": True}

    national = "0" + phone_e164[3:] if phone_e164.startswith("+91") else phone_e164
    url = f"https://{cfg['subdomain']}/v1/Accounts/{cfg['account_sid']}/Calls/connect.json"
    auth = base64.b64encode(f"{cfg['api_key']}:{cfg['api_token']}".encode()).decode()

    # Custom params ride along to the Voicebot applet, which forwards them in the
    # start event -- that is how the agent learns who it is calling and why.
    fields = {
        "From": national,
        "CallerId": cfg["caller_id"],
        "CallType": "trans",
        "Url": cfg["flow_url"],
        "CustomField": json.dumps(custom)[:800],
    }
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            call = json.loads(resp.read().decode()).get("Call", {})
            return {"ok": True, "sid": call.get("Sid"), "status": call.get("Status"),
                    "http": resp.status}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}", "http": exc.code,
                "terminal": exc.code in TERMINAL_HTTP}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "http": None,
                "terminal": False}


def process_one(bypass_hours: bool = False) -> Optional[str]:
    """Take one due row and act on it. Returns a short description, or None."""
    row = db.claim_next_call()
    if not row:
        return None

    qid, tenant_id, phone = row["id"], row["tenant_id"], row["phone_e164"]

    # Same gate the manual trigger uses: valid Indian mobile, example calling window, DND.
    gate = run_contact_gate(phone, bypass_time_check_for_dev=bypass_hours)
    if not gate.is_approved:
        if gate.rejection_code == "CALLING_WINDOW":
            # Not a failure -- just too early or too late. Hold it until the
            # window reopens instead of burning an attempt.
            db.finish_queued(qid, "queued", last_error="outside example calling window",
                             not_before="")
            with db.connect() as conn:
                conn.execute(
                    "UPDATE call_queue SET attempts = attempts - 1, "
                    "not_before = datetime('now', '+30 minutes') WHERE id = ?", (qid,))
            return f"#{qid} held: outside calling hours"
        db.finish_queued(qid, "blocked", last_error=gate.reason)
        return f"#{qid} blocked: {gate.rejection_code}"

    if tenant_id != int(os.getenv("VOICE_TENANT_ID", "1")):
        db.finish_queued(qid, "blocked", last_error="Foreign tenant")
        return "blocked foreign tenant"
    cfg = tenant_telephony(tenant_id)
    context = json.loads(row["context"] or "{}")
    custom = {"lead_name": row["lead_name"] or "", "queue_id": qid,
              "reason": row["reason"] or ""}
    custom.update({k: v for k, v in context.items() if k not in custom and isinstance(v, (str, int, float))})

    result = place_call(cfg, gate.phone_e164, custom)
    if result["ok"]:
        call_id = db.start_call(tenant_id, campaign_id=row["campaign_id"],
                                contact_id=row["contact_id"],
                                provider_sid=result.get("sid"), channel="phone",
                                status="ringing", queue_id=qid)
        db.finish_queued(qid, "dialed", call_id=call_id, last_error=None)
        return f"#{qid} dialed {gate.phone_e164} sid={result.get('sid')}"

    if result.get("terminal"):
        db.finish_queued(qid, "failed", last_error=result["error"])
        return f"#{qid} failed permanently: {result['error'][:90]}"

    db.retry_later(qid, RETRY_MINUTES, result["error"])
    return f"#{qid} retry in {RETRY_MINUTES}m: {result['error'][:90]}"


async def run_forever(bypass_hours: bool = False) -> None:
    logger.info("dialer started (poll %.1fs, retry %dm, example calling window %s)",
                POLL_SECONDS, RETRY_MINUTES,
                "bypassed" if bypass_hours else "enforced")
    while True:
        try:
            worked = process_one(bypass_hours)
            if worked:
                logger.info(worked)
            else:
                await asyncio.sleep(POLL_SECONDS)
        except Exception as exc:
            logger.exception("dialer loop error: %s", exc)
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    from security import configure_private_logging
    configure_private_logging()
    db.init_db()
    bypass = False
    if "--once" in sys.argv:
        while True:
            out = process_one(bypass)
            if not out:
                print("queue empty")
                break
            print(out)
    else:
        asyncio.run(run_forever(bypass))
