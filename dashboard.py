"""
Tenant-facing dashboard: login, call history, transcripts.

Server-rendered HTML on purpose. A company evaluating this needs to open a URL
and see their calls; a build step and a frontend framework would add days and
change nothing they can see.

Every query is scoped by the session's tenant_id. There is no route that reads a
row without it.
"""
import os
import json
import html
import datetime
from typing import Optional

from fastapi import APIRouter, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse

import db

router = APIRouter()
COOKIE = "vsa_session"
CALL_DIR = os.path.join(os.path.dirname(__file__), "logs", "calls")


def current_user(request: Request):
    return db.session_user(request.cookies.get(COOKIE, ""))


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


# Presentation helpers. A dashboard reads better when a machine value is spoken
# the way a person would say it -- "Today 11:33" beats "2026-08-25 11:33:40".
_TOOL_SAID = {
    "check_calendar_slots": "Checked the calendar for free slots",
    "book_demo_slot": "Booked the slot and emailed the invite",
}

_DISPOSITION_TONE = {
    "demo_booked": ("good", "Demo booked"),
    "interested": ("good", "Interested"),
    "callback_requested": ("warn", "Call back"),
    "not_interested": ("", "Not interested"),
    "do_not_call": ("bad", "Do not call"),
    "wrong_number": ("", "Wrong number"),
    "no_conversation": ("", "No conversation"),
}


def _disp(value) -> str:
    if not value:
        return '<span class="disp" style="color:var(--ghost)">&mdash;</span>'
    tone, label = _DISPOSITION_TONE.get(value, ("", str(value).replace("_", " ")))
    return (f'<span class="disp {tone}"><span class="dot"></span>'
            f'{esc(label)}</span>')


def _queue_state(status) -> str:
    tone = {"queued": "warn", "dialing": "warn", "done": "good",
            "failed": "bad", "blocked": "bad"}.get(status, "")
    return (f'<span class="disp {tone}"><span class="dot"></span>'
            f'{esc(str(status).replace("_", " "))}</span>')


def _dur(seconds) -> str:
    if not seconds:
        return '<span style="color:var(--ghost)">&mdash;</span>'
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


# SQLite writes datetime('now') in UTC. Rendering that raw showed an 11:33 IST
# call as "Today 05:15" -- wrong by 5h30 for every user of an Indian product.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _when(stamp) -> str:
    """'Today 11:33' / 'Yesterday 18:22' / '24 Aug 09:14', in IST."""
    if not stamp:
        return "-"
    try:
        when = datetime.datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(stamp)[:16]
    when = when.replace(tzinfo=datetime.timezone.utc).astimezone(IST)
    today = datetime.datetime.now(IST).date()
    delta = (today - when.date()).days
    clock = when.strftime("%H:%M")
    if delta == 0:
        return f"Today {clock}"
    if delta == 1:
        return f"Yesterday {clock}"
    return when.strftime("%d %b ") + clock


# -- shared chrome ---------------------------------------------------------
CSS = """
/* Warm editorial. Paper, not a control panel: a cream ground, a serif for
   figures and headings, and one terracotta accent that only ever marks the
   thing you should act on. Numerals are monospace so durations and phone
   numbers line up down a column. */
:root{
  --paper:#F6F3EC; --surface:#FFFDF8; --raised:#FCF9F2;
  --rule:#E2DCCF; --rule-soft:#EFE9DC;
  --ink:#23201B; --body:#4A4439; --muted:#6E6659; --faint:#8C8478; --ghost:#B0A796;
  --accent:#A8442A; --accent-dark:#7C2E1B; --accent-wash:#F7EAE4;
  --good:#2F6B4F; --good-wash:#EEF3EF;
  --warn:#96601C; --warn-wash:#FBF1E0;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);min-height:100vh;
     font-family:'IBM Plex Sans',system-ui,-apple-system,sans-serif;
     font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-dark)}
h1{font-family:'Newsreader',Georgia,serif;font-size:34px;font-weight:500;
   letter-spacing:-0.015em;line-height:1.15;margin-bottom:5px}
h2{font-family:'Newsreader',Georgia,serif;font-size:19px;font-weight:500;
   margin:30px 0 12px}
code,.mono{font-family:'IBM Plex Mono',ui-monospace,monospace}

/* nav */
.nav{display:flex;align-items:center;gap:26px;padding:0 40px;height:64px;
     border-bottom:1px solid var(--rule);background:var(--surface)}
.nav .brand{display:flex;flex-direction:column;line-height:1.15;color:var(--ink);
     text-decoration:none}
.nav .brand-title{font-family:'Newsreader',Georgia,serif;font-size:21px;font-weight:600;
     letter-spacing:-0.01em;color:var(--ink);display:flex;align-items:baseline}
.nav .brand-title span{font-family:'IBM Plex Sans',sans-serif;font-size:11px;
     letter-spacing:0.09em;text-transform:uppercase;color:var(--faint);
     margin-left:8px;font-weight:400}
.nav .brand-sub{font-family:'IBM Plex Sans',sans-serif;font-size:10px;
     color:var(--faint);letter-spacing:0.02em;margin-top:1px;font-weight:400}
.nav a.tab{font-size:14px;padding:6px 13px;border-radius:6px;color:var(--muted)}
.nav a.tab:hover{background:var(--rule-soft);color:var(--ink)}
.nav a.tab.on{background:var(--rule-soft);color:var(--ink);font-weight:500}
.nav .spacer{flex:1}
.nav .who{font-size:13px;color:var(--muted)}
.wrap{max-width:1180px;margin:0 auto;padding:36px 40px 56px}
.sub{color:var(--muted);font-size:14px;margin-bottom:20px}
.head{display:flex;align-items:flex-end;justify-content:space-between;
      margin-bottom:30px;gap:24px}

/* figures — a ruled row, not a deck of cards */
.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
     border-top:1px solid var(--ink);border-bottom:1px solid var(--rule);
     margin-bottom:34px}
.fig{padding:20px 24px;border-right:1px solid var(--rule)}
.fig:first-child{padding-left:0}
.fig:last-child{border-right:none;padding-right:0}
.fig .n{font-family:'Newsreader',Georgia,serif;font-size:38px;font-weight:500;
     line-height:1;letter-spacing:-0.02em}
.fig .n small{font-size:19px;color:var(--faint)}
.fig .n.hi{color:var(--accent)}
.fig .l{font-size:11px;letter-spacing:0.09em;text-transform:uppercase;
     color:var(--faint);margin-top:7px}

/* ledger */
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:8px;
     overflow:hidden}
table{width:100%;border-collapse:collapse;background:var(--surface);
     border:1px solid var(--rule);border-radius:8px;overflow:hidden}
th{text-align:left;padding:11px 22px;border-bottom:1px solid var(--rule);
   font-size:11px;letter-spacing:0.09em;text-transform:uppercase;
   color:var(--faint);font-weight:500}
td{padding:15px 22px;border-top:1px solid var(--rule-soft);font-size:14px;
   vertical-align:middle}
tr:hover td{background:var(--raised)}
td .who{font-weight:500}
td .meta{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--faint)}
td.num{font-family:'IBM Plex Mono',monospace}

/* dispositions — a dot and a word, never a loud pill */
.disp{display:inline-flex;align-items:center;gap:8px;font-size:13px}
.disp .dot{width:6px;height:6px;border-radius:50%;background:var(--faint)}
.disp.good{color:var(--good);font-weight:500}
.disp.good .dot{background:var(--good)}
.disp.warn{color:var(--warn);font-weight:500}
.disp.warn .dot{background:var(--warn)}
.disp.bad{color:var(--accent)}
.disp.bad .dot{background:var(--accent)}

.btn{display:inline-flex;align-items:center;gap:9px;padding:9px 16px;
     border:1px solid var(--ink);border-radius:6px;font-size:13px;font-weight:500;
     color:var(--ink);background:transparent;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--ink);color:var(--surface)}
.btn.solid{background:var(--accent);border-color:var(--accent);color:#FFFDF8}
.btn.solid:hover{background:var(--accent-dark);border-color:var(--accent-dark)}

.empty{background:var(--surface);border:1px dashed var(--rule);border-radius:8px;
     padding:44px;text-align:center;color:var(--muted)}

/* transcript */
.turn{display:flex;gap:13px;margin-bottom:13px}
.turn .t{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--ghost);
     padding-top:2px;min-width:38px;flex-shrink:0}
.turn .lbl{font-size:11px;letter-spacing:0.07em;text-transform:uppercase;
     color:var(--faint);margin-bottom:3px}
.turn.agent .lbl{color:var(--accent)}
.turn .say{font-size:14px;line-height:1.55}
.turn.tool{align-items:center;background:var(--good-wash);border-radius:5px;
     padding:7px 11px;margin-bottom:13px}
.turn.tool .ran{font-family:'IBM Plex Mono',monospace;font-size:12px;
     color:var(--good)}

/* outcome */
.outcome{background:var(--surface);border:1px solid var(--rule);
     border-left:3px solid var(--good);border-radius:8px;padding:18px 20px}
.outcome.thin{border-left-color:var(--warn)}
.outcome .top{display:flex;align-items:center;justify-content:space-between;
     margin-bottom:11px}
.outcome .verdict{font-size:14px;font-weight:600;color:var(--good)}
.outcome.thin .verdict{color:var(--warn)}
.outcome .conf{font-family:'IBM Plex Mono',monospace;font-size:11px;
     color:var(--faint)}
.outcome p{font-size:14px;line-height:1.55;color:var(--body);margin-bottom:14px}
.outcome .next{padding-top:13px;border-top:1px solid var(--rule-soft)}
.outcome .next .l{font-size:11px;letter-spacing:0.09em;text-transform:uppercase;
     color:var(--faint);margin-bottom:5px}
.outcome .next .v{font-size:14px;font-weight:500}
.split{display:grid;grid-template-columns:1.35fr 1fr;gap:26px;align-items:start}

.warn{background:var(--accent-wash);border:1px solid #E8C4B6;color:var(--accent-dark);
     padding:12px 15px;border-radius:8px;margin:14px 0;font-size:13px;line-height:1.5}
.note{background:var(--good-wash);border:1px solid #CFE0D5;color:var(--good);
     padding:11px 14px;border-radius:8px;margin-bottom:16px;font-size:13px}

/* forms */
form.auth{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
     padding:30px;max-width:404px;margin:76px auto}
form.auth h1{font-size:27px;margin-bottom:6px}
label{display:block;font-size:12px;letter-spacing:0.06em;text-transform:uppercase;
     color:var(--faint);margin:16px 0 6px}
input{width:100%;background:var(--paper);border:1px solid var(--rule);
     border-radius:6px;padding:11px 13px;color:var(--ink);font-size:14px;
     font-family:'IBM Plex Sans',sans-serif}
input:focus{outline:none;border-color:var(--accent);background:var(--surface)}
button{width:100%;background:var(--accent);color:#FFFDF8;border:none;padding:12px;
     border-radius:6px;font-size:15px;font-weight:500;cursor:pointer;margin-top:22px;
     font-family:'IBM Plex Sans',sans-serif}
button:hover{background:var(--accent-dark)}
.err{background:var(--accent-wash);color:var(--accent-dark);padding:10px 13px;
     border-radius:6px;font-size:13px;margin-bottom:6px}
pre{background:#23201B;border-radius:8px;padding:15px;overflow-x:auto;
    font-size:12px;color:#E8E2D6;font-family:'IBM Plex Mono',monospace;
    line-height:1.6}
audio{width:100%;margin-top:8px}
@media (max-width:820px){.split{grid-template-columns:1fr}.wrap{padding:24px 20px}}
"""


def page(title: str, body: str, user=None, here: str = "") -> HTMLResponse:
    nav = ""
    if user:
        tabs = (("/app", "Calls"), ("/app/queue", "Queue"),
                ("/app/bookings", "Bookings"), ("/app/agent", "Agent"),
                ("/demo", "Test call"))
        links = "".join(
            f'<a class="tab{" on" if here == href else ""}" href="{href}">{label}</a>'
            for href, label in tabs)
        nav = f"""<div class="nav">
  <a href="/app" class="brand">
    <div class="brand-title">Aarav<span>Voice Desk</span></div>
    <div class="brand-sub">by AxonCrew &middot; experimental</div>
  </a>
  {links}
  <span class="spacer"></span>
  <span class="who">{esc(user['company_name'])}</span>
  <a href="/logout" class="tab">Sign out</a>
</div>"""
    return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style></head><body>{nav}{body}</body></html>""")


# -- auth ------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = ""):
    if current_user(request):
        return RedirectResponse("/app", status_code=303)
    err = f'<div class="err">{esc(error)}</div>' if error else ""
    return page("Sign in", f"""<form class="auth" method="post" action="/login">
  <h1>Sign in</h1>
  <p class="sub">Your voice agent&rsquo;s desk</p>{err}
  <label>Work email</label><input name="email" type="email" required autofocus>
  <label>Password</label><input name="password" type="password" required>
  <button type="submit">Sign in</button>
  <p class="sub" style="margin-top:16px">Ask the local operator for access.</p>
</form>""")


@router.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    user = db.authenticate(email, password)
    if not user:
        return RedirectResponse("/login?error=Wrong+email+or+password", status_code=303)
    token = db.create_session(user["id"])
    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, secure=os.getenv("COOKIE_SECURE", "1") == "1", samesite="strict", max_age=14 * 86400)
    return resp






@router.get("/logout")
async def logout(request: Request):
    db.destroy_session(request.cookies.get(COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# -- dashboard -------------------------------------------------------------
@router.get("/app", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not db.is_onboarded(user["tenant_id"]):
        return RedirectResponse("/app/onboarding", status_code=303)

    st = db.stats(user["tenant_id"])
    calls = db.recent_calls(user["tenant_id"])

    figures = (("Calls placed", st["calls"], False),
               ("Demos booked", st["demos_booked"], True),
               ("Average length", f'{st["avg_duration"]}<small>s</small>', False),
               ("Actions taken", st["tools_used"], False))
    cards = "".join(
        f'<div class="fig"><div class="n{" hi" if hi else ""}">{v}</div>'
        f'<div class="l">{k}</div></div>' for k, v, hi in figures)

    if calls:
        rows = "".join(f"""<tr onclick="location='/app/calls/{c['id']}'"
      style="cursor:pointer">
  <td class="num" style="color:var(--muted)">#{c['id']}</td>
  <td><span class="who">{esc(c['contact_name'] or 'Unknown')}</span>
      <span class="meta" style="margin-left:9px">{esc(c['phone_e164'] or '')}</span></td>
  <td style="color:var(--muted)">{esc(_when(c['started_at']))}</td>
  <td class="num">{_dur(c['duration_s'])}</td>
  <td>{_disp(c['disposition'])}</td>
</tr>""" for c in calls)
        table = (f"<table><tr><th>Call</th><th>Prospect</th><th>When</th>"
                 f"<th>Length</th><th>Outcome</th></tr>{rows}</table>")
    else:
        table = ('<div class="empty">No calls yet.<br><br>'
                 '<a href="/demo">Run a test call in the browser</a> '
                 'and it appears here with the transcript and outcome.</div>')

    booked = st["demos_booked"]
    line = (f"{st['calls']} call{'s' if st['calls'] != 1 else ''} so far"
            + (f", {booked} booked a demo." if booked else ", none booked yet."))         if st["calls"] else "Nothing has been dialled yet."

    return page("Calls", f"""<div class="wrap">
<div class="head">
  <div>
    <h1>Calls</h1>
    <p class="sub" style="margin-bottom:0">{esc(line)}</p>
  </div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <a class="btn" href="/demo">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2
           19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1
           4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6
           6l1.4-1.2a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2Z"/></svg>
      Place a test call
    </a>
    <a class="btn solid" href="/app/queue">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2
           19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1
           4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6
           6l1.4-1.2a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2Z"/></svg>
      Place a real call
    </a>
  </div>
</div>
<div class="figures">{cards}</div>
<h2 style="margin-top:0">Recent</h2>
{table}</div>""", user, here="/app")


@router.get("/app/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(request: Request, call_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    call, turns, outcome = db.call_detail(user["tenant_id"], call_id)
    if not call:
        return page("Not found", '<div class="wrap"><div class="empty">'
                                 'No such call.</div></div>', user)

    # Same check analyze_call.py runs: a promise with no matching tool means the
    # agent told the prospect something happened that did not.
    tool_names = {t["text"] for t in turns if t["speaker"] == "tool"}
    spoken = " ".join(t["text"] for t in turns if t["speaker"] == "agent").lower()
    claimed = [p for p, need in (("kar diya", "book_demo_slot"),
                                 ("mil jayengi", "send_whatsapp"),
                                 ("bhej diya", "book_demo_slot"))
               if p in spoken and not any(need in n for n in tool_names)]

    body = []
    if claimed:
        body.append('<div class="warn"><b>Unverified promise.</b> The agent said an '
                    'action was done but no matching tool ran &mdash; the prospect '
                    'may be waiting for something that never happened.</div>')

    for t in turns:
        kind = t["speaker"] if t["speaker"] in ("agent", "caller", "tool") else "agent"
        stamp = f'<span class="t">{t["t_offset"]:.1f}s</span>'
        if kind == "tool":
            name = t["text"].split(" ->")[0]
            body.append(
                f'<div class="turn tool">{stamp}'
                f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
                f'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" '
                f'style="color:var(--good);flex-shrink:0"><path d="M20 6 9 17l-5-5"/></svg>'
                f'<span class="ran">{esc(_TOOL_SAID.get(name, name))}</span></div>')
        else:
            label = "Aarav" if kind == "agent" else "Prospect"
            body.append(f'<div class="turn {kind}">{stamp}<div>'
                        f'<div class="lbl">{label}</div>'
                        f'<div class="say">{esc(t["text"])}</div></div></div>')
    if not turns:
        body.append('<div class="empty">No transcript recorded for this call.</div>')

    oc = '<div class="empty" style="padding:26px">Not classified yet.</div>'
    if outcome:
        conf = outcome["confidence"] or 0
        tone, label = _DISPOSITION_TONE.get(outcome["disposition"],
                                            ("", outcome["disposition"] or "-"))
        extras = "".join(
            f'<div class="next" style="border-top:none;padding-top:9px">'
            f'<div class="l">{lbl}</div><div class="v">{esc(val)}</div></div>'
            for lbl, val in (("Objection", outcome["objection"]),
                             ("Call back", outcome["callback_at"]),
                             ("Email given", outcome["contact_email"])) if val)
        thin = " thin" if conf < 0.5 else ""
        low = ('<div class="warn" style="margin:0 0 14px">Low confidence &mdash; '
               'read the transcript before acting on this.</div>') if conf < 0.5 else ""
        oc = f"""<div class="outcome{thin}">
  {low}
  <div class="top">
    <span class="verdict">{esc(label)}</span>
    <span class="conf">{conf:.0%} sure</span>
  </div>
  <p>{esc(outcome['notes'] or '')}</p>
  <div class="next"><div class="l">Next</div>
    <div class="v">{esc(outcome['next_action'] or 'Nothing needed')}</div></div>
  {extras}
</div>"""

    audio = ""
    if call["recording_dir"] and __import__("re").fullmatch(r"[A-Za-z0-9_-]{1,48}", call["recording_dir"]):
        for tag, title in (("out", "What Aarav said"), ("in", "What the line sent")):
            path = os.path.join(CALL_DIR, f"{call['recording_dir']}_{tag}.wav")
            if os.path.exists(path):
                audio += (f'<h2>{title}</h2><audio controls preload="none" '
                          f'src="/app/calls/{call_id}/audio/{tag}"></audio>')

    meta = " &middot; ".join(filter(None, [
        _when(call["started_at"]),
        _dur(call["duration_s"]).replace('<span style="color:var(--ghost)">&mdash;</span>',
                                         "no duration"),
        f'{call["tools_used"]} action{"s" if call["tools_used"] != 1 else ""} taken',
        f'{call["echo_suppressed"]} echo frames filtered' if call["echo_suppressed"] else "",
    ]))

    return page(f"Call #{call_id}", f"""<div class="wrap">
<p class="sub" style="margin-bottom:10px"><a href="/app">&larr; All calls</a></p>
<div class="head" style="margin-bottom:22px">
  <div><h1>Call #{call_id}</h1><p class="sub" style="margin-bottom:0">{meta}</p></div>
</div>
<div class="split">
  <div>
    <h2 style="margin-top:0">What was said</h2>
    <div class="panel" style="padding:18px 20px">{''.join(body)}</div>
    {audio}
  </div>
  <div>
    <h2 style="margin-top:0">Outcome</h2>
    {oc}
  </div>
</div></div>""", user, here="/app")


@router.get("/app/calls/{call_id}/audio/{tag}")
async def call_audio(request: Request, call_id: int, tag: str):
    user = current_user(request)
    if not user:
        return Response(status_code=401)
    if tag not in ("in", "out"):
        return Response(status_code=404)
    call, _, _ = db.call_detail(user["tenant_id"], call_id)
    if not call or not call["recording_dir"]:
        return Response(status_code=404)
    if not __import__("re").fullmatch(r"[A-Za-z0-9_-]{1,48}", call["recording_dir"]):
        return Response(status_code=404)
    path = os.path.join(CALL_DIR, f"{call['recording_dir']}_{tag}.wav")
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/wav")


@router.get("/app/agent", response_class=HTMLResponse)
async def agent_settings(request: Request, saved: str = "", onboarded: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    agent = db.agent_row(user["tenant_id"])

    note = ""
    if onboarded:
        note = ('<div class="note">Read your website. Check the facts below &mdash; '
                'Aarav will say these out loud to real people.</div>')
    elif saved:
        note = '<div class="note">Saved. New calls use this straight away.</div>'

    def val(field, fallback=""):
        return esc((agent[field] if agent and agent[field] else fallback))

    source = ""
    if agent and agent["website"]:
        source = (f'<p class="sub" style="font-size:12px;margin:-8px 0 18px">'
                  f'Read from <a href="{esc(agent["website"])}" target="_blank" '
                  f'rel="noopener">{esc(agent["website"])}</a> &middot; '
                  f'<a href="/app/onboarding">read it again</a></p>')

    return page("Agent", f"""<div class="wrap" style="max-width:760px">
<div class="head" style="display:block">
  <h1>What Aarav knows</h1>
  <p class="sub" style="margin-bottom:0">Every word here can be spoken to a real
  prospect. If a fact is wrong, the agent says it wrong.</p>
</div>
{note}{source}
<form method="post" action="/app/agent" style="background:var(--surface);
      border:1px solid var(--rule);border-radius:8px;padding:26px 28px">

  <label style="margin-top:0">Agent&rsquo;s name</label>
  <input name="name" value="{val('name','Aarav')}" required>

  <label>Company name he says on the call</label>
  <input name="company_name" value="{esc(user['company_name'])}" required>

  <label>What your company does, in one sentence</label>
  <input name="one_liner" value="{val('one_liner')}"
         placeholder="We build custom software and AI automation for founders">

  <label>What you sell</label>
  <input name="company_blurb" value="{val('company_blurb')}"
         placeholder="Custom software, AI agents, cloud and DevOps">

  <label>Who you sell to</label>
  <input name="who_they_sell_to" value="{val('who_they_sell_to')}"
         placeholder="Founders and brands building digital products">

  <label>Facts he may state &mdash; separate with a semicolon</label>
  <input name="key_facts" value="{val('key_facts')}"
         placeholder="300+ projects delivered; 10 years in the industry; team of 30+">

  <label>Goal of every call</label>
  <input name="goal" value="{val('goal','Book a short demo.')}">

  <button type="submit">Save</button>
</form>
<p class="sub" style="font-size:12px;margin-top:14px">He can only state the facts
listed above. Anything not here, he will say he needs to check.</p>
</div>""", user, here="/app/agent")


@router.post("/app/agent")
async def save_agent(request: Request, name: str = Form(...),
                     company_name: str = Form(""), one_liner: str = Form(""),
                     company_blurb: str = Form(""), who_they_sell_to: str = Form(""),
                     key_facts: str = Form(""), goal: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with db.connect() as conn:
        existing = conn.execute("SELECT id, profile_json FROM agent "
                                "WHERE tenant_id = ? LIMIT 1",
                                (user["tenant_id"],)).fetchone()
        # Keep the scraped profile in step with hand edits, so the prompt builder
        # and this form never disagree about the company's name.
        try:
            profile = json.loads(existing["profile_json"] or "{}") if existing else {}
        except (json.JSONDecodeError, TypeError):
            profile = {}
        profile.update({"company_name": company_name.strip(),
                        "one_liner": one_liner.strip(),
                        "what_they_sell": company_blurb.strip(),
                        "who_they_sell_to": who_they_sell_to.strip(),
                        "key_facts": [f.strip() for f in key_facts.split(";")
                                      if f.strip()]})
        blob = json.dumps(profile, ensure_ascii=False)
        if existing:
            conn.execute(
                "UPDATE agent SET name=?, one_liner=?, company_blurb=?, "
                "who_they_sell_to=?, key_facts=?, goal=?, profile_json=? WHERE id=?",
                (name, one_liner, company_blurb, who_they_sell_to, key_facts,
                 goal, blob, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO agent (tenant_id, name, one_liner, company_blurb, "
                "who_they_sell_to, key_facts, goal, profile_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user["tenant_id"], name, one_liner, company_blurb,
                 who_they_sell_to, key_facts, goal, blob))
        if company_name.strip():
            conn.execute("UPDATE tenant SET company_name=?, onboarded=1 WHERE id=?",
                         (company_name.strip(), user["tenant_id"]))
    return RedirectResponse("/app/agent?saved=1", status_code=303)


@router.get("/app/bookings", response_class=HTMLResponse)
async def bookings(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    rows = db.upcoming_bookings(user["tenant_id"])
    if rows:
        body = "".join(f"""<tr>
  <td class="num">{esc(_when(b['slot_start']))}</td>
  <td><span class="who">{esc(b['contact_name'] or 'Unknown')}</span></td>
  <td>{esc(b['contact_email'] or '')
        or '<span style="color:var(--ghost)">no email captured</span>'}</td>
  <td class="meta">{esc(b['contact_phone'] or '')}</td>
  <td>{'<span class="disp good"><span class="dot"></span>Invite sent</span>'
       if b['email_sent'] else
       '<span class="disp bad"><span class="dot"></span>No invite</span>'}</td>
  <td>{f"<a href='/app/calls/{b['call_id']}'>#{b['call_id']}</a>"
       if b['call_id'] else '<span style="color:var(--ghost)">&mdash;</span>'}</td>
</tr>""" for b in rows)
        table = (f"<table><tr><th>Slot (IST)</th><th>Name</th><th>Email</th>"
                 f"<th>Phone</th><th>Invite</th><th>Call</th></tr>{body}</table>")
    else:
        table = ('<div class="empty">No demos booked yet.<br><br>'
                 'When Aarav books one on a call it appears here, and the prospect '
                 'gets a calendar invite by email.</div>')

    return page("Bookings", f"""<div class="wrap">
<div class="head">
  <div>
    <h1>Bookings</h1>
    <p class="sub" style="margin-bottom:0">Slots Aarav reserved during calls. Each
    one blocks that time, so it is never offered twice.</p>
  </div>
</div>
{table}</div>""", user, here="/app/bookings")


@router.get("/app/queue", response_class=HTMLResponse)
async def queue_view(request: Request, placed: str = "", error: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    api_key = db.ensure_api_key(user["tenant_id"])
    rows = db.queue_rows(user["tenant_id"])

    if rows:
        body = "".join(f"""<tr>
  <td class="num" style="color:var(--muted)">#{r['id']}</td>
  <td><span class="who">{esc(r['lead_name'] or 'Unknown')}</span>
      <span class="meta" style="margin-left:9px">{esc(r['phone_e164'])}</span></td>
  <td>{esc(r['reason'] or '-')}</td>
  <td style="color:var(--muted)">{esc(r['source'])}</td>
  <td>{_queue_state(r['status'])}</td>
  <td class="num">{r['attempts']}/{r['max_attempts']}</td>
  <td style="color:var(--accent);font-size:12px">{esc((r['last_error'] or '')[:60])}</td>
</tr>""" for r in rows)
        table = (f"<table><tr><th>#</th><th>Who</th><th>Why</th><th>Source</th>"
                 f"<th>Status</th><th>Tries</th><th>Last error</th></tr>{body}</table>")
    else:
        table = ('<div class="empty">Nothing waiting.<br><br>'
                 'Place a call above, or point your CRM at the trigger endpoint.</div>')

    flash = ""
    if placed:
        flash = (f'<div class="note">Queued. Aarav dials within a few seconds &mdash; '
                 f'the call appears in <a href="/app">Calls</a> when it ends.</div>')
    if error:
        flash = f'<div class="warn">{esc(error)}</div>'

    curl = (f"curl -X POST {esc(str(request.base_url))}api/v1/trigger \
"
            f"  -H 'X-API-Key: {esc(api_key)}' \
"
            f"  -H 'Content-Type: application/json' \
"
            f"  -d '{{\"phone\":\"+91XXXXXXXXXX\",\"name\":\"Priya\","
            f"\"reason\":\"demo form submitted\","
            f"\"trigger_key\":\"form-8821\"}}'")

    return page("Call queue", f"""<div class="wrap">
<div class="head">
  <div>
    <h1>Place a call</h1>
    <p class="sub" style="margin-bottom:0">Aarav dials, has the conversation, books
    the demo, and writes back what happened.</p>
  </div>
</div>
{flash}

<form method="post" action="/app/queue/call" style="background:var(--surface);
      border:1px solid var(--rule);border-radius:8px;padding:22px 24px;
      display:grid;grid-template-columns:1fr 1fr 1.2fr auto;gap:16px;
      align-items:end;margin-bottom:12px">
  <div>
    <label style="margin-top:0">Mobile number</label>
    <input name="phone" placeholder="Your allowlisted test number" required autofocus>
  </div>
  <div>
    <label style="margin-top:0">Their name</label>
    <input name="lead_name" placeholder="Priya">
  </div>
  <div>
    <label style="margin-top:0">Why you are calling</label>
    <input name="reason" placeholder="Asked for a demo on the website">
  </div>
  <button type="submit" style="margin-top:0;width:auto;padding:11px 20px">Call now</button>
</form>
<p class="sub" style="font-size:12px">Calls only go out between 09:00 and 21:00 IST.
On a trial Exotel account the number must be verified in the Exotel dashboard first.</p>

<h2>Waiting to be dialled</h2>
{table}

<h2>Have your own system call for you</h2>
<p class="sub">Point your CRM, website form or billing system here and the call goes
out within seconds of the event &mdash; a lead reached in five minutes converts far
better than one reached in thirty. Always send a <code>trigger_key</code>: webhook
retries are normal, and the same key never dials twice.</p>
<pre>{curl}</pre>
<p class="sub" style="font-size:12px">Keep this key secret &mdash; it can place calls
on your account.</p>
</div>""", user, here="/app/queue")


@router.post("/app/queue/call")
async def queue_place_call(request: Request, phone: str = Form(...),
                           lead_name: str = Form(""), reason: str = Form("")):
    """Enqueue a call from the dashboard.

    Same queue the trigger endpoint writes to, so the dialer, the compliance gate
    and the retry policy are shared rather than duplicated for the UI.
    """
    from urllib.parse import quote
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    from contact_gate import run_contact_gate
    gate = run_contact_gate(phone, bypass_time_check_for_dev=True)
    if not gate.is_approved:
        return RedirectResponse(f"/app/queue?error={quote(gate.reason or 'Invalid number')}",
                                status_code=303)

    result = db.enqueue_call(user["tenant_id"], gate.phone_e164,
                             lead_name=lead_name.strip() or None,
                             reason=reason.strip() or None, source="dashboard")
    if result.get("duplicate"):
        return RedirectResponse("/app/queue?error=That+number+is+already+queued",
                                status_code=303)
    return RedirectResponse("/app/queue?placed=1", status_code=303)


# Manual company setup keeps untrusted website fetching out of this starter.
@router.get("/app/onboarding")
async def onboarding(request: Request):
    return RedirectResponse("/app/agent", status_code=303)
