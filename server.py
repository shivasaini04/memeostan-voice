"""
FastAPI ingress: authenticated Exotel Gemini audio and browser voice demo.

Exotel notes that cost us a working call the first time round:
  * connect.json's `Url` must be an App Bazaar flow
    (http://my.exotel.com/{sid}/exoml/start_voice/{app_id}). Pointing it at this
    server made Exotel fetch something it could not interpret as a flow, so the
    call dropped the moment it was answered.
  * The dynamic-audio applet expects **JSON** with Content-Type application/json,
    not TwiML-style XML, and it also probes the endpoint with HEAD.
  * Audio must be 8 kHz / 16-bit / mono and under 2 MB. Anything else is refused.
"""
import os
import sys
import json
import base64
import asyncio
import logging
import time
import urllib.parse

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from contact_gate import run_contact_gate
from tts import voice, wav_format, TELEPHONY_SAMPLE_RATE, BROWSER_SAMPLE_RATE
from voice_session import (VoiceSession, get_or_create_session, drop_session,
                           build_greeting, build_system_prompt, company_of)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Windows consoles default to cp1252, which renders Hindi transcripts as \uXXXX
# escapes and makes live call logs unreadable when debugging.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
logger = logging.getLogger("server")

# Calls cannot be replayed from a terminal that has scrolled away, so everything
# also goes to logs/server.log and each call is recorded to logs/calls/.
from call_log import setup_file_logging, CallRecorder
logger.info("file logging -> %s", setup_file_logging())

@asynccontextmanager
async def lifespan(app):
    # No provider connections are opened automatically.
    yield
    await voice.close()
    import stt
    await stt.close()

app = FastAPI(
    lifespan=lifespan,
    title="Memeostan Voice — Experimental Exotel Starter",
    description="Low-latency conversational voice sales agent for the Indian market.",
    version="2.0.0",
)

GREETING_PATH = os.path.join(os.path.dirname(__file__), "greeting.wav")

# Operator dashboard: login, call history, transcripts.
import db as _db
from dashboard import router as dashboard_router
_db.init_db()
app.include_router(dashboard_router)
from security import StarterSecurity, configure_private_logging
configure_private_logging()
app.add_middleware(StarterSecurity)


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/app")




# -- static audio ----------------------------------------------------------


@app.get("/health")
async def health():
    """Surfaces the things that silently break a live call."""
    audio = {"exists": os.path.exists(GREETING_PATH)}
    if audio["exists"]:
        with open(GREETING_PATH, "rb") as fh:
            head = fh.read(64)
        rate, ch, bits = wav_format(head)
        size = os.path.getsize(GREETING_PATH)
        audio.update(sample_rate=rate, channels=ch, bit_depth=bits, bytes=size,
                     exotel_compatible=(rate, ch, bits) == (8000, 1, 16) and size < 2 * 1024 * 1024)
    return {
        "sarvam_configured": voice.is_configured(),
        "openrouter_configured": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        "status": "ok",
        "greeting_audio": audio,
    }


# -- Exotel dynamic-audio applets -----------------------------------------
# The two applets do NOT share a response format, and sending the wrong one
# plays nothing:
#   Greeting applet -> text/plain, the bare audio URL on its own line
#   Connect applet  -> application/json, {"start_call_playback": {...}}
# Both probe with HEAD before playing and expect identical headers.

def _caller_of(request: Request) -> str:
    return (request.query_params.get("From")
            or request.query_params.get("CallFrom") or "unknown")






# -- Exotel Voicebot bidirectional stream ---------------------------------
# Exotel sends raw/slin: 16-bit little-endian mono PCM, base64 per frame.
EXOTEL_FRAME_BYTES = 3200          # 100 ms at 8 kHz, a multiple of 320
SILENCE_FRAMES_TO_END_TURN = 8     # ~800 ms of quiet ends the caller's turn
# PSTN lines carry comfort noise, so this is a level gate, not a silence gate.
SPEECH_RMS_THRESHOLD = int(os.getenv("SPEECH_RMS_THRESHOLD", "500"))
DEFAULT_TELEPHONY_ENGINE = os.getenv("TELEPHONY_ENGINE", "gemini")
# Extra suppression after our audio stops, covering network and handset delay.
ECHO_TAIL_S = float(os.getenv("ECHO_TAIL_S", "0.5"))
# How much louder than the measured echo the caller must be to get through while
# the agent is speaking. Too low and the agent hears itself again; too high and
# the caller has to shout to interrupt.
ECHO_BREAK_RATIO = float(os.getenv("ECHO_BREAK_RATIO", "1.2"))
# How much of our outbound level comes back as echo. A phone leg typically
# returns well under half; anything above coupling x ratio is the caller.
ECHO_COUPLING = float(os.getenv("ECHO_COUPLING", "0.5"))
# Speak the opening line with Sarvam when the model is slow to connect. Off while
# 3.1 is being evaluated on real calls -- a second voice makes the call unjudgeable.
SARVAM_COVER = os.getenv("SARVAM_COVER", "0") not in ("0", "false", "False")


@app.websocket("/api/v1/telephony/exotel/stream")
async def exotel_voicebot_stream(websocket: WebSocket):
    """Basic authentication is checked by StarterSecurity before accepting audio."""
    await websocket.accept()
    await _exotel_gemini_stream(websocket, int(websocket.query_params.get("sample-rate", "8000")))


async def _speak_text_now(text: str, sample_rate: int, send_fn, flush_fn) -> None:
    """Say one line through Sarvam, straight down the Exotel socket.

    Used only to cover a slow model handshake. Sarvam is a plain REST call and
    answers in about a second, so the caller hears the opening line on time even
    when the Live session is still negotiating.
    """
    try:
        pcm = await voice.synthesize_pcm(text, sample_rate=sample_rate)
        if not pcm:
            return
        # send_audio_to_exotel expects Gemini's 24 kHz and resamples down, so hand
        # it audio already at that rate rather than double-converting.
        from audio import resample_pcm
        from engines import GEMINI_OUTPUT_RATE
        as_gemini, _ = resample_pcm(pcm, sample_rate, GEMINI_OUTPUT_RATE)
        await send_fn(as_gemini)
        await flush_fn()
    except Exception as exc:
        logger.error("fallback greeting failed: %s", exc)


async def _exotel_gemini_stream(websocket: WebSocket, sample_rate: int) -> None:
    """Exotel's Voicebot protocol driven by Gemini Live.

    Exotel speaks base64 PCM inside JSON at a negotiated rate; Gemini wants 16 kHz
    in and always answers at 24 kHz. Both directions are resampled with a carried
    filter state so chunk boundaries do not click.
    """
    from engines import (GeminiLiveEngine, GEMINI_INPUT_RATE, GEMINI_OUTPUT_RATE,
                         GEMINI_LIVE_MODEL)
    from voice_session import SYSTEM_PROMPT
    from audio import resample_pcm

    tenant_id = int(os.getenv("VOICE_TENANT_ID", "1"))
    stream_sid = None
    engine = None
    pump = None
    seq = 0
    up_state = down_state = None
    outbuf = bytearray()
    playout_until = 0.0        # monotonic time our sent audio stops sounding
    nonlocal_level = [0.0]     # decaying peak of how loud we have recently been
    echo_seen = 0
    db_call_id = None
    turns_log: list = []
    t_start = time.monotonic()
    # AgentStream spec: each audio chunk must be 3,200-100,000 bytes and a multiple
    # of 320. 100 ms is the recommended size, but 100 ms at 8 kHz is only 1,600
    # bytes -- under the floor -- so the floor wins when Exotel negotiates 8 kHz.
    FRAME_MS = 100
    EXOTEL_MIN_CHUNK = 3200

    def frame_bytes_for(rate: int) -> int:
        return max(EXOTEL_MIN_CHUNK, (rate * 2 * FRAME_MS // 1000 + 319) // 320 * 320)

    OUT_FRAME_BYTES = frame_bytes_for(sample_rate)
    stats = {"events": {}, "in_frames": 0, "in_bytes": 0,
             "out_frames": 0, "out_bytes": 0, "rms_max": 0.0,
             "echo_suppressed": 0}

    async def _emit(frame: bytes) -> None:
        nonlocal playout_until
        """Exactly the documented outbound message -- nothing more.

        The AgentStream reference is explicit: event, stream_sid (snake_case) and
        media.payload. sequence_number, chunk and timestamp are NOT required for
        events sent from the bot. Three live calls died 80-190 ms after the first
        frame while we were sending those extras plus a duplicate camelCase
        streamSid; the camelCase key came from a blog post, not the spec.
        """
        nonlocal seq
        seq += 1
        await websocket.send_json({
            "event": "media",
            "stream_sid": stream_sid,
            "media": {"payload": base64.b64encode(frame).decode()},
        })
        stats["out_frames"] += 1
        stats["out_bytes"] += len(frame)
        # Peak-hold with decay, not the last frame. A single frame is often a
        # pause between words, and during the tail window the last frame is
        # usually silence -- referencing that collapsed the threshold to zero and
        # let the echo straight through.
        nonlocal_level[0] = max(_rms(frame), nonlocal_level[0] * 0.98)
        if recorder:
            recorder.write_out(frame)
        # Frames leave faster than real time, so track when this audio will have
        # finished playing in the caller's ear rather than when we sent it.
        now = time.monotonic()
        playout_until = max(playout_until, now) + len(frame) / (sample_rate * 2)

    async def send_audio_to_exotel(pcm24: bytes) -> None:
        """24 kHz from Gemini -> the rate Exotel negotiated, in whole frames.

        Gemini emits chunks of wildly varying size, including 2-byte ones. Framing
        each chunk on its own produced a single 320-byte runt frame on the first
        real call, and Exotel tore the call down 86 ms later. Audio is buffered and
        released only in exact OUT_FRAME_BYTES pieces.
        """
        nonlocal down_state
        pcm, down_state = resample_pcm(pcm24, GEMINI_OUTPUT_RATE, sample_rate, down_state)
        outbuf.extend(pcm)
        while len(outbuf) >= OUT_FRAME_BYTES:
            await _emit(bytes(outbuf[:OUT_FRAME_BYTES]))
            del outbuf[:OUT_FRAME_BYTES]
            await asyncio.sleep(0)

    async def flush_audio_to_exotel() -> None:
        """Pad and release the tail once the agent has finished speaking."""
        if not outbuf:
            return
        frame = bytes(outbuf) + bytes(OUT_FRAME_BYTES - len(outbuf))
        outbuf.clear()
        await _emit(frame)

    async def pump_from_gemini() -> None:
        first = True
        try:
            async for chunk in engine.receive():
                if chunk.audio:
                    if first:
                        logger.info("[%s] first Gemini audio -> Exotel", stream_sid)
                        first = False
                    await send_audio_to_exotel(chunk.audio)
                if chunk.tool:
                    stats["tools"] = stats.get("tools", 0) + 1
                    status = (chunk.tool_result or {}).get("status")
                    logger.info("[%s] TOOL %s -> %s", stream_sid, chunk.tool, status)
                    turns_log.append({"t": time.monotonic() - t_start,
                                      "speaker": "tool",
                                      "text": f"{chunk.tool} -> {status}"})
                    if recorder:
                        recorder.note("tool", name=chunk.tool,
                                      result=chunk.tool_result)
                if chunk.text:
                    who = "caller" if chunk.speaker == "user" else "agent"
                    logger.info("[%s] %s: %s", stream_sid, who, chunk.text.strip()[:90])
                    turns_log.append({"t": time.monotonic() - t_start,
                                      "speaker": who, "text": chunk.text.strip()})
                    if recorder:
                        recorder.note(who, text=chunk.text.strip())
                if chunk.final:
                    await flush_audio_to_exotel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[%s] gemini pump failed: %s", stream_sid, exc)

    # Connecting to Gemini takes ~2.5s. Waiting for Exotel's start event first put
    # that on the caller's clock: an early call had 4.9s of dead air between pickup
    # and the first word. Start the session now, while Exotel finishes handshaking.
    #
    # The system prompt must be FINAL before connect(): the Live API takes it in
    # the session config, so assigning engine.system_prompt afterwards is silently
    # ignored. That is why a Example Company call still opened with "the default company" even
    # though the profile was stored. Tenant-level facts are available here
    # (tenant_id is on the URL); only the prospect's name and reason wait for the
    # start event, and those ride in on the greeting turn instead.
    warm_agent = warm_tenant = None
    try:
        warm_agent = _db.agent_row(tenant_id)
        warm_tenant = _db.tenant_row(tenant_id)
    except Exception as exc:
        logger.warning("could not load tenant %s profile: %s", tenant_id, exc)

    engine = GeminiLiveEngine(
        build_system_prompt(warm_agent, lead_name="ji",
                            lead_phone="the number on file"),
        tool_context={"tenant_id": tenant_id,
                      "company_name": company_of(warm_agent, warm_tenant)})
    warmup = asyncio.create_task(engine.connect())
    recorder: CallRecorder | None = None

    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                break
            if raw.get("bytes") is not None:
                stats["events"]["<binary>"] = stats["events"].get("<binary>", 0) + 1
                continue

            event = json.loads(raw.get("text") or "{}")
            kind = event.get("event")
            stats["events"][kind] = stats["events"].get(kind, 0) + 1

            if kind == "start":
                start = event.get("start", {})
                stream_sid = (event.get("stream_sid") or event.get("streamSid")
                              or start.get("stream_sid") or start.get("streamSid"))
                caller = start.get("from", "")
                params = start.get("custom_parameters", {}) or {}
                lead_name = params.get("lead_name", "ji")

                # Attribute the call to whoever queued it. Without this every
                # phone call lands on tenant 1 and the company that placed it
                # sees an empty dashboard.
                # queue_id only arrives when the applet URL carries it; a plain
                # applet gives us nothing, so fall back to the number we just
                # dialled.
                queued = _db.queue_row_by_id(params.get("queue_id"))
                if not queued:
                    queued = _db.recent_queue_for_phone(caller)
                    if queued:
                        logger.info("[%s] matched queue #%s by phone number",
                                    stream_sid, queued["id"])
                if queued:
                    if not lead_name or lead_name == "ji":
                        lead_name = queued["lead_name"] or lead_name
                if queued and queued["tenant_id"] != tenant_id:
                    await websocket.close(code=1008)
                    return
                logger.info("[%s] gemini call from %s (%s) | media_format=%s | model=%s",
                            stream_sid, caller, lead_name,
                            start.get("media_format"), GEMINI_LIVE_MODEL)

                # Exotel does NOT pass the ?sample-rate query string through to the
                # socket, so the URL default (8 kHz) was being used while Exotel
                # actually ran at 16 kHz. That mismatch played our audio back at
                # double speed and stretched the caller's audio to half speed, so
                # Gemini could not recognise it and never replied. The start event
                # is the authoritative source for the negotiated rate.
                reason = params.get("reason", "") or (
                    queued["reason"] if queued and queued["reason"] else "")
                company = company_of(warm_agent, warm_tenant)
                agent_name = (warm_agent["name"] if warm_agent and warm_agent["name"]
                              else "Aarav")
                engine.greeting = build_greeting(lead_name, company=company,
                                                 agent_name=agent_name, reason=reason)
                logger.info("[%s] calling for %s as %s | reason=%r",
                            stream_sid, company, agent_name, reason[:60])
                engine.lead_name = lead_name
                engine.lead_phone = caller or ""
                # A phone call is a dashboard row too. Without this the real
                # calls -- the ones that matter -- never reach the CRM view.
                try:
                    # Prefer the row the dialer already opened for this queue
                    # entry; only insert a new one for a call nothing queued
                    # (an inbound call, or the shell trigger script).
                    db_call_id = _db.adopt_queued_call(
                        params.get("queue_id"), stream_sid or "",
                        stream_sid or "call")
                    if db_call_id:
                        logger.info("[%s] adopted queued call row #%s",
                                    stream_sid, db_call_id)
                    else:
                        db_call_id = _db.start_call(
                            tenant_id, channel="phone", direction="outbound",
                            status="in_progress", provider_sid=stream_sid,
                            recording_dir=(stream_sid or "call"),
                            queue_id=queued["id"] if queued else None,
                            contact_name=lead_name if lead_name != "ji" else None,
                            contact_phone=caller or None)
                    # The adopted row was created before Exotel told us who
                    # answered; fill the name in either way so the list does not
                    # read "Unknown" for a call we know the prospect of.
                    if db_call_id:
                        _db.finish_call(db_call_id, close=False,
                                        contact_name=(lead_name
                                                      if lead_name != "ji" else None),
                                        contact_phone=caller or None)
                    engine.tool_context.update({"tenant_id": tenant_id,
                                                "call_id": db_call_id,
                                                "lead_name": lead_name,
                                                "lead_phone": caller or ""})
                except Exception as exc:
                    logger.warning("[%s] could not record call: %s", stream_sid, exc)
                recorder = CallRecorder(stream_sid or "call", sample_rate)
                recorder.note("start", caller=caller, lead_name=lead_name,
                              media_format=start.get("media_format"),
                              model=GEMINI_LIVE_MODEL)
                declared = (start.get("media_format") or {}).get("sample_rate")
                try:
                    declared = int(declared)
                except (TypeError, ValueError):
                    declared = None
                if declared and declared not in (8000, 16000, 24000):
                    raise ValueError("Unsupported audio rate")
                if declared and declared != sample_rate:
                    logger.info("[%s] sample rate %d Hz (was assuming %d Hz)",
                                stream_sid, declared, sample_rate)
                    sample_rate = declared
                    OUT_FRAME_BYTES = frame_bytes_for(sample_rate)
                    recorder.set_rate(sample_rate)

                # The caller has already said hello into a live line. If the model
                # is not ready almost immediately, speak the opening line with
                # Sarvam rather than leaving them in silence -- a real call spent
                # 32 seconds dead while the handshake retried, and the prospect
                # hung up.
                spoken_by_fallback = False
                try:
                    await asyncio.wait_for(asyncio.shield(warmup), timeout=1.5)
                except asyncio.TimeoutError:
                    if not SARVAM_COVER:
                        # Disabled on purpose while 3.1 is being judged on a real
                        # call: a Sarvam voice cutting in mid-greeting changes what
                        # the prospect hears and masks how often the model is slow.
                        logger.warning("[%s] model still connecting (Sarvam cover "
                                       "disabled) - waiting", stream_sid)
                        try:
                            await warmup
                        except Exception as exc:
                            logger.error("[%s] model unavailable: %s", stream_sid, exc)
                            break
                        logger.info("[%s] gemini session ready", stream_sid)
                        spoken_by_fallback = False
                        await engine.open_with(engine.greeting or
                                               build_greeting(lead_name))
                        pump = asyncio.create_task(pump_from_gemini())
                        continue
                    logger.warning("[%s] model still connecting - covering with "
                                   "the greeting", stream_sid)
                    asyncio.create_task(_speak_text_now(
                        engine.greeting or build_greeting(lead_name),
                        sample_rate, send_audio_to_exotel, flush_audio_to_exotel))
                    spoken_by_fallback = True
                    try:
                        await warmup
                    except Exception as exc:
                        logger.error("[%s] model unavailable: %s", stream_sid, exc)
                        await asyncio.sleep(6)   # let the greeting finish playing
                        break
                except Exception as exc:
                    logger.error("[%s] warm session failed (%s), reconnecting",
                                 stream_sid, exc)
                    try:
                        await engine.connect()
                    except Exception as exc2:
                        logger.error("[%s] model unavailable: %s", stream_sid, exc2)
                        await _speak_text_now(
                            engine.greeting or build_greeting(lead_name),
                            sample_rate, send_audio_to_exotel,
                            flush_audio_to_exotel)
                        break
                logger.info("[%s] gemini session ready", stream_sid)
                pump = asyncio.create_task(pump_from_gemini())
                if spoken_by_fallback:
                    # Sarvam already said it aloud; tell the model what it said so
                    # it picks up the conversation instead of greeting again.
                    await engine.seed_said(engine.greeting or
                                           build_greeting(lead_name))
                else:
                    await engine.open_with(engine.greeting or
                                           build_greeting(lead_name))

            elif kind == "media" and engine is not None:
                chunk = base64.b64decode(event["media"]["payload"])
                stats["in_frames"] += 1
                stats["in_bytes"] += len(chunk)
                level = _rms(chunk)
                stats["rms_max"] = max(stats["rms_max"], level)
                if recorder:
                    recorder.write_in(chunk)

                # A phone line has no echo cancellation: our own audio returns on
                # the inbound stream and the model cannot tell it from the caller.
                # Blanket-discarding everything while we speak fixed the agent
                # talking to itself, but threw away 62% of the caller's audio on a
                # real call -- their overlapping speech reached the model shredded
                # and came back as gibberish.
                #
                # So learn the echo's own loudness while we are speaking, and let
                # through anything clearly louder than it: that is the caller
                # actually talking over us, not our voice coming back.
                if time.monotonic() < playout_until + ECHO_TAIL_S:
                    # Gate against what we actually SENT, not against a guess
                    # learned from the inbound signal. Echo is our own audio
                    # attenuated by the handset and the line, so the loudness we
                    # just played is a real upper bound on how loud the echo can
                    # be. Estimating it from inbound instead failed both ways: a
                    # peak-seeded floor locked onto the caller and muted them for
                    # the whole call, and a fast-falling one tracked the gaps
                    # between words and let every echo syllable through.
                    nonlocal_level[0] *= 0.995      # fade once we go quiet
                    expected_echo = nonlocal_level[0] * ECHO_COUPLING
                    echo_seen += 1
                    if level < expected_echo * ECHO_BREAK_RATIO:
                        stats["echo_suppressed"] += 1
                        continue
                    stats["barge_through"] = stats.get("barge_through", 0) + 1

                if stats["in_frames"] in (1, 50):
                    logger.info("[%s] inbound frame #%d: %d bytes rms=%.0f",
                                stream_sid, stats["in_frames"], len(chunk), level)
                if recorder:
                    recorder.write_caller(chunk)
                pcm16, up_state = resample_pcm(chunk, sample_rate,
                                               GEMINI_INPUT_RATE, up_state)
                await engine.send_audio(pcm16)

            elif kind == "stop":
                logger.info("[%s] call ended: %s", stream_sid,
                            event.get("stop", {}).get("reason"))
                break

    except WebSocketDisconnect:
        logger.info("[%s] exotel disconnected", stream_sid)
    except Exception as exc:
        logger.exception("[%s] gemini telephony error: %s", stream_sid, exc)
    finally:
        logger.info("[%s] STREAM SUMMARY events=%s | inbound %d frames/%d bytes "
                    "(peak rms %.0f, %d echo-suppressed, %d spoke through) | "
                    "outbound %d frames/%d bytes",
                    stream_sid, stats["events"], stats["in_frames"],
                    stats["in_bytes"], stats["rms_max"], stats["echo_suppressed"],
                    stats.get("barge_through", 0),
                    stats["out_frames"], stats["out_bytes"])
        if recorder:
            recorder.close(stats)
            logger.info("[%s] recording -> logs/calls/%s_{in,out}.wav",
                        stream_sid, recorder.call_id)
        if stats["in_frames"] == 0:
            logger.warning("[%s] Exotel sent NO media frames -- it is not "
                           "streaming caller audio to us.", stream_sid)
        if stats["out_frames"] == 0:
            logger.warning("[%s] we sent NO audio to Exotel -- Gemini produced "
                           "nothing.", stream_sid)
        if db_call_id:
            duration = round(time.monotonic() - t_start, 1)
            try:
                _db.add_turns(db_call_id, turns_log)
                _db.finish_call(db_call_id, status="completed", duration_s=duration,
                                tools_used=stats.get("tools", 0),
                                echo_suppressed=stats.get("echo_suppressed", 0))
                asyncio.create_task(_classify_call(db_call_id, turns_log, duration))
            except Exception as exc:
                logger.warning("[%s] could not finalise call: %s", stream_sid, exc)
        if pump:
            pump.cancel()
        if not warmup.done():
            warmup.cancel()
        if engine:
            await engine.close()


def _rms(pcm: bytes) -> float:
    """Root-mean-square level of a 16-bit LE PCM buffer."""
    if len(pcm) < 2:
        return 0.0
    total = 0
    positions = range(0, len(pcm) - 1, 64)      # sample every 32nd sample
    for i in positions:
        val = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        total += val * val
    return (total / max(1, len(positions))) ** 0.5


def _is_speech(pcm: bytes, threshold: int = None) -> bool:
    """Cheap RMS gate. Good enough for turn-taking on a clean PSTN leg; a real
    deployment should swap this for a proper VAD (webrtcvad / silero)."""
    return _rms(pcm) > (SPEECH_RMS_THRESHOLD if threshold is None else threshold)


async def _handle_turn(audio: bytes, sample_rate: int, session: VoiceSession, speak) -> None:
    from stt import transcribe_pcm
    transcript = await transcribe_pcm(audio, sample_rate)
    if not transcript:
        return
    logger.info("[%s] caller: %s", session.call_id, transcript)
    reply = await session.process_user_turn(transcript)
    logger.info("[%s] agent: %s", session.call_id, reply)
    if reply:
        await speak(reply, "reply")







# -- trigger API: the way calls actually start in production ---------------
class TriggerRequest(BaseModel):
    phone: str
    name: str = ""
    reason: str = ""
    context: dict = __import__("pydantic").Field(default_factory=dict)
    trigger_key: str = ""       # supply this; webhook retries are normal
    campaign_id: int | None = None
    delay_minutes: int = 0


@app.post("/api/v1/trigger")
async def trigger_call(req: TriggerRequest, request: Request):
    """Enqueue one call in response to an event in the tenant's own system.

    This is the production path: a form submitted, a trial expiring, a payment
    failed. Speed matters -- a lead called within five minutes converts far
    better than one called at thirty -- so this only enqueues and returns; the
    dialer picks it up within seconds.

    Auth is a per-tenant API key in X-API-Key.
    """
    tenant = _db.tenant_by_api_key(request.headers.get("x-api-key", ""))
    if not tenant:
        return JSONResponse({"error": "invalid or missing X-API-Key"}, status_code=401)

    gate = run_contact_gate(req.phone, bypass_time_check_for_dev=True)
    if not gate.is_approved:
        # Reject bad numbers at the door rather than queueing a call that can
        # never connect. The example calling window is checked at dial time, not here.
        return JSONResponse({"error": gate.reason, "code": gate.rejection_code},
                            status_code=422)

    not_before = None
    if req.delay_minutes > 0:
        not_before = f"datetime('now', '+{int(req.delay_minutes)} minutes')"

    result = _db.enqueue_call(
        tenant["id"], gate.phone_e164, lead_name=req.name, reason=req.reason,
        context=req.context, trigger_key=req.trigger_key or None,
        campaign_id=req.campaign_id, source="trigger")

    if req.delay_minutes > 0 and result["queued"]:
        with _db.connect() as conn:
            conn.execute("UPDATE call_queue SET not_before = datetime('now', ?) "
                         "WHERE id = ?", (f"+{int(req.delay_minutes)} minutes",
                                          result["id"]))

    if result["duplicate"]:
        logger.info("[tenant %s] duplicate trigger_key %r ignored (queue #%s)",
                    tenant["id"], req.trigger_key, result["id"])
        return {"status": "duplicate", "queue_id": result["id"],
                "message": "This trigger_key was already accepted; no second call."}

    logger.info("[tenant %s] queued call #%s to %s (%s)", tenant["id"],
                result["id"], gate.phone_e164, req.reason or "no reason given")
    return {"status": "queued", "queue_id": result["id"], "phone": gate.phone_e164}


# -- real-time browser conversation ---------------------------------------
@app.get("/api/v1/engines")
async def list_engines():
    """Which engines can actually run right now, and why not if they cannot."""
    from engines import GEMINI_LIVE_MODEL
    gemini = {"available": bool(os.getenv("GEMINI_API_KEY", "").strip()), "reason": "Configured; availability is checked when a call starts."}
    return {
        "sarvam": {"available": voice.is_configured(),
                   "reason": None if voice.is_configured() else "SARVAM_API_KEY not set",
                   "model": os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
                   "kind": "STT + LLM + TTS pipeline"},
        "gemini": {"available": gemini["available"], "reason": gemini["reason"],
                   "model": GEMINI_LIVE_MODEL, "kind": "native speech-to-speech"},
    }


@app.websocket("/api/v1/voice/live")
async def voice_live(websocket: WebSocket):
    """Streams microphone PCM in and agent speech out over one socket."""
    from engines import SarvamEngine, GeminiLiveEngine
    from realtime import RealtimeConversation, RATE

    await websocket.accept()
    engine_name = websocket.query_params.get("engine", "sarvam")
    lead_name = websocket.query_params.get("lead_name", "Prospect")
    session_id = "browser_" + __import__("secrets").token_hex(16)

    await drop_session(session_id)          # each call starts fresh
    session = get_or_create_session(session_id, lead_name, "+910000000000")

    # Which company this browser call belongs to, decided BEFORE the engine is built:
    # the Gemini branch below reads tenant_id and db_call_id, and they used to be set
    # after it, so every Gemini browser session died with UnboundLocalError (2026-09-18).
    # The logged-in session decides the tenant; the query parameter is only a fallback.
    tenant_id = int(os.getenv("VOICE_TENANT_ID", "1"))
    try:
        from dashboard import COOKIE as _VSA_COOKIE
        _user = _db.session_user(websocket.cookies.get(_VSA_COOKIE, ""))
        if _user is not None:
            tenant_id = int(_user["tenant_id"])
    except Exception as exc:
        logger.warning("could not resolve the session tenant: %s", exc)
    # A browser test call is a real call as far as the dashboard is concerned --
    # it is the first thing a company tries, and seeing it appear in the history
    # is what demonstrates the product works.
    db_call_id = None
    try:
        db_call_id = _db.start_call(tenant_id, channel="browser", direction="outbound",
                                    status="in_progress", provider_sid=session_id)
    except Exception as exc:
        logger.warning("could not record call: %s", exc)

    agent_row = _db.agent_row(tenant_id)
    session.conversation_history[0]["content"] = build_system_prompt(agent_row, lead_name=lead_name, lead_phone=session.lead_phone)
    session.tool_context = {"tenant_id": tenant_id, "call_id": db_call_id,
                            "company_name": company_of(agent_row, _db.tenant_row(tenant_id))}

    if engine_name == "gemini":
        access = {"available": bool(os.getenv("GEMINI_API_KEY", "").strip()), "reason": "GEMINI_API_KEY not configured"}
        if not access["available"]:
            await websocket.send_json({"type": "error",
                                       "message": f"Gemini unavailable - {access['reason']}"})
            if db_call_id:
                _db.finish_call(db_call_id, status="failed")
            await websocket.close()
            await drop_session(session_id)
            return
        agent_row = None
        try:
            agent_row = _db.agent_row(tenant_id)
        except Exception:
            pass
        engine = GeminiLiveEngine(
            build_system_prompt(agent_row, lead_name=lead_name,
                                lead_phone=session.lead_phone,
                                reason=websocket.query_params.get("reason", "")),
            lead_name=lead_name, lead_phone=session.lead_phone,
            tool_context={"tenant_id": tenant_id, "call_id": db_call_id,
                          "lead_name": lead_name,
                          "lead_phone": session.lead_phone,
                          "company_name": company_of(agent_row, _db.tenant_row(tenant_id))})
        engine.greeting = build_greeting(
            lead_name, company=company_of(agent_row, _db.tenant_row(tenant_id)),
            reason=websocket.query_params.get("reason", ""))
    else:
        engine = SarvamEngine(session)
        if not engine.available:
            await websocket.send_json({"type": "error", "message": "SARVAM_API_KEY not set"})
            if db_call_id:
                _db.finish_call(db_call_id, status="failed")
            await websocket.close()
            await drop_session(session_id)
            return

    convo = RealtimeConversation(websocket, engine, session)
    logger.info("[%s] live conversation opened (engine=%s)", session_id, engine_name)

    turns_log: list = []
    nonlocal_tools = [0]          # list so the nested driver can mutate it
    t_start = time.monotonic()

    # Gemini is speech-to-speech: it does its own VAD, endpointing and barge-in,
    # so it takes the raw microphone stream and never uses transcribe()/respond().
    if engine_name == "gemini":
        try:
            await _run_gemini_live(websocket, convo, engine, lead_name, session_id,
                                   turns_log, nonlocal_tools, t_start)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.exception("[%s] gemini live error: %s", session_id, exc)
            await convo.send_event("error", message="Voice provider failed. Check your account/model configuration.")
        finally:
            await convo.shutdown()
            await engine.close()
            await drop_session(session_id)
            _finish_browser_call(db_call_id, t_start, turns_log, nonlocal_tools[0])
            logger.info("[%s] live conversation closed", session_id)
        return

    try:
        greeting = build_greeting(lead_name, company=session.tool_context["company_name"])
        session.conversation_history.append({"role": "assistant", "content": greeting})
        await convo.send_event("agent", text=greeting, partial=False)
        logger.info("[%s] synthesizing greeting at %d Hz...", session_id, RATE)
        pcm = await voice.synthesize_pcm(greeting, sample_rate=RATE)
        logger.info("[%s] greeting pcm = %s bytes", session_id,
                    "None" if pcm is None else len(pcm))
        if pcm:
            await convo.send_event("state", value="speaking")
            # Playback runs as a task so the receive loop stays live and the
            # caller can talk over the greeting.
            convo.speaking_task = asyncio.create_task(_play_greeting(convo, pcm))
        else:
            logger.error("[%s] greeting synthesis returned no audio", session_id)
            await convo.send_event("error", message="greeting synthesis failed")
            await convo.send_event("state", value="listening")

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await convo.on_audio(message["bytes"])
            elif message.get("text"):
                event = json.loads(message["text"])
                if event.get("type") == "bye":
                    break
                if event.get("type") == "interrupt":
                    await convo.stop_speaking()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("[%s] live conversation error: %s", session_id, exc)
    finally:
        await convo.shutdown()
        await engine.close()
        await drop_session(session_id)
        turns_log.extend({"speaker": m["role"], "text": m.get("content", ""), "t": 0}
                         for m in session.conversation_history if m["role"] in ("user", "assistant") and isinstance(m.get("content"), str))
        _finish_browser_call(db_call_id, t_start, turns_log, nonlocal_tools[0])
        logger.info("[%s] live conversation closed", session_id)


def _finish_browser_call(call_id, t_start: float, turns: list, tools: int) -> None:
    """Close out the dashboard row for a browser call."""
    if not call_id:
        return
    duration = round(time.monotonic() - t_start, 1)
    try:
        _db.add_turns(call_id, turns)
        _db.finish_call(call_id, status="completed", duration_s=duration,
                        tools_used=tools)
    except Exception as exc:
        logger.warning("could not finalise call %s: %s", call_id, exc)
        return
    # Classification talks to a model, so it must not hold the websocket close.
    asyncio.create_task(_classify_call(call_id, turns, duration))


async def _classify_call(call_id: int, turns: list, duration: float) -> None:
    """Extract the structured outcome and push it to the tenant's webhook.

    A recording nobody reads is worth nothing; the disposition is the row the
    sales team actually works from.
    """
    from outcomes import extract_outcome, notify_tenant
    try:
        tool_names = [t["text"].split(" ->")[0] for t in turns
                      if t.get("speaker") == "tool"]
        result = await extract_outcome(turns, tool_names, duration)
        _db.save_outcome(call_id, result)
        logger.info("[call %s] outcome=%s confidence=%.2f", call_id,
                    result.get("disposition"), result.get("confidence", 0))

    except Exception as exc:
        logger.exception("[call %s] outcome extraction failed: %s", call_id, exc)


async def _run_gemini_live(websocket, convo, engine, lead_name: str,
                           session_id: str, turns_log=None, nonlocal_tools=None,
                           t_start: float = 0.0) -> None:
    """Relay: browser mic -> Gemini, Gemini audio -> browser.

    Gemini returns 24 kHz while the browser pipeline runs at 16 kHz, so the
    playback rate is announced up front and the browser builds its buffers at
    that rate rather than replaying the audio too slowly.
    """
    from engines import GEMINI_OUTPUT_RATE
    from realtime import FRAME_BYTES

    logger.info("[%s] connecting to Gemini Live...", session_id)
    await engine.connect()
    logger.info("[%s] Gemini connected; announcing %d Hz", session_id, GEMINI_OUTPUT_RATE)
    await convo.send_event("format", sample_rate=GEMINI_OUTPUT_RATE)
    await convo.send_event("state", value="listening")

    async def pump_from_gemini() -> None:
        """Gemini decides when to speak, so this just forwards whatever arrives."""
        said = ""
        sent_audio = 0
        try:
            async for chunk in engine.receive():
                if convo.closed:
                    return
                if chunk.audio:
                    sent_audio += len(chunk.audio)
                    if sent_audio == len(chunk.audio):
                        logger.info("[%s] first Gemini audio forwarded", session_id)
                    for off in range(0, len(chunk.audio), FRAME_BYTES):
                        await websocket.send_bytes(chunk.audio[off:off + FRAME_BYTES])
                        await asyncio.sleep(0)
                if chunk.tool:
                    nonlocal_tools[0] += 1
                    logger.info("[%s] TOOL %s -> %s", session_id, chunk.tool,
                                (chunk.tool_result or {}).get("status"))
                    turns_log.append({"t": time.monotonic() - t_start,
                                      "speaker": "tool",
                                      "text": f"{chunk.tool} -> "
                                              f"{(chunk.tool_result or {}).get('status')}"})
                    await convo.send_event("tool", name=chunk.tool)
                if chunk.text and chunk.speaker == "user":
                    turns_log.append({"t": time.monotonic() - t_start,
                                      "speaker": "caller", "text": chunk.text})
                    await convo.send_event("user", text=chunk.text, partial=True)
                elif chunk.text:
                    said += chunk.text
                    turns_log.append({"t": time.monotonic() - t_start,
                                      "speaker": "agent", "text": chunk.text})
                    await convo.send_event("agent", text=chunk.text, partial=True)
                if chunk.final:
                    if said.strip():
                        convo.session.conversation_history.append(
                            {"role": "assistant", "content": said.strip()})
                        said = ""
                    await convo.send_event("state", value="listening")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[%s] gemini receive failed: %s", session_id, exc)
            await convo.send_event("error", message=f"Gemini stream ended: {exc}"[:200])

    pump = asyncio.create_task(pump_from_gemini())

    # Ask Gemini to open the call rather than waiting for the prospect to speak.
    await engine.open_with(engine.greeting or build_greeting(lead_name))
    logger.info("[%s] greeting turn sent to Gemini", session_id)

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await engine.send_audio(message["bytes"])
            elif message.get("text"):
                event = json.loads(message["text"])
                if event.get("type") == "bye":
                    break
    finally:
        pump.cancel()


async def _play_greeting(convo, pcm: bytes) -> None:
    from realtime import FRAME_BYTES
    sent = 0
    try:
        for off in range(0, len(pcm), FRAME_BYTES):
            if convo.closed:
                logger.warning("greeting playback aborted after %d frames "
                               "(conversation marked closed)", sent)
                return
            await convo.ws.send_bytes(pcm[off:off + FRAME_BYTES])
            sent += 1
            await asyncio.sleep(0)
        logger.info("greeting playback sent %d frames", sent)
        # Only now is the agent actually done talking.
        await convo.send_event("state", value="listening")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("greeting playback failed: %s", exc)
        await convo.send_event("state", value="listening")


# -- browser demo ---------------------------------------------------------
@app.get("/demo", response_class=HTMLResponse)
async def browser_voice_demo():
    return HTMLResponse(content=DEMO_HTML)








DEMO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Test call &middot; Voice Desk</title>
<style>
  :root{
    --paper:#F6F3EC; --surface:#FFFDF8; --raised:#FCF9F2;
    --rule:#E2DCCF; --rule-soft:#EFE9DC;
    --ink:#23201B; --body:#4A4439; --muted:#6E6659; --faint:#8C8478; --ghost:#B0A796;
    --accent:#A8442A; --accent-dark:#7C2E1B; --accent-wash:#F7EAE4;
    --good:#2F6B4F; --good-wash:#EEF3EF;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--paper);color:var(--ink);min-height:100vh;display:flex;
       justify-content:center;align-items:flex-start;padding:44px 20px;
       font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:14px;line-height:1.5}
  a{color:var(--accent);text-decoration:none}
  a:hover{color:var(--accent-dark)}
  .card{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
        width:100%;max-width:560px;padding:32px;text-align:center}
  .eyebrow{font-size:11px;letter-spacing:0.09em;text-transform:uppercase;
           color:var(--faint);margin-bottom:18px}
  h1{font-family:'Newsreader',Georgia,serif;font-size:30px;font-weight:500;
     letter-spacing:-0.015em;margin-bottom:4px}
  p.desc{color:var(--muted);font-size:14px;margin-bottom:24px}
  .avatar{width:88px;height:88px;border-radius:50%;background:var(--accent-wash);
          border:1px solid #E8C4B6;display:flex;align-items:center;
          justify-content:center;margin:0 auto 18px}
  .avatar svg{color:var(--accent)}
  .avatar.speaking{animation:pulse 1.2s infinite}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(168,68,42,.28)}
                   50%{box-shadow:0 0 0 16px rgba(168,68,42,0)}}
  .engines{display:flex;gap:10px;margin-bottom:18px;text-align:left}
  .engine{flex:1;background:var(--paper);border:1px solid var(--rule);
          border-radius:8px;padding:11px 12px;cursor:pointer}
  .engine b{display:block;font-size:13px;font-weight:600;margin-bottom:2px}
  .engine span{display:block;font-size:11px;color:var(--muted);line-height:1.35}
  .engine .model{font-family:'IBM Plex Mono',monospace;font-size:10px;
                 color:var(--ghost);margin-top:3px;word-break:break-all}
  .engine .why{color:var(--accent);margin-top:4px}
  .engine.active{border-color:var(--accent);background:var(--accent-wash)}
  .engine.disabled{opacity:.55;cursor:not-allowed}
  .btn{width:100%;background:var(--accent);color:#FFFDF8;border:none;padding:13px;
       border-radius:6px;font-size:15px;font-weight:500;cursor:pointer;
       margin-bottom:10px;font-family:'IBM Plex Sans',sans-serif}
  .btn:hover{background:var(--accent-dark)}
  .btn.stop{background:var(--ink)}
  .btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--ink)}
  .btn.ghost:hover{background:var(--ink);color:var(--surface)}
  .status{font-size:13px;color:var(--muted);min-height:20px;margin-bottom:8px}
  .status.err{color:var(--accent)}
  .meter{height:3px;background:var(--rule-soft);border-radius:2px;overflow:hidden;
         margin:8px 0 4px}
  .meter-fill{height:100%;width:0%;background:var(--accent);transition:width .06s linear}
  .latency{font-size:11px;color:var(--ghost);min-height:14px;
           font-family:'IBM Plex Mono',monospace}
  .chat-box{background:var(--paper);border:1px solid var(--rule);border-radius:8px;
            padding:16px;text-align:left;max-height:300px;overflow-y:auto;
            margin-top:16px;font-size:14px}
  .msg{margin-bottom:9px;padding:9px 12px;border-radius:8px;line-height:1.5}
  .msg.agent{background:var(--surface);border-left:2px solid var(--accent)}
  .msg.user{background:var(--rule-soft)}
  .msg.sys{background:transparent;color:var(--good);font-size:12px;padding:3px 4px;
           font-family:'IBM Plex Mono',monospace}
  .devanagari{display:block;color:var(--ghost);font-size:12px;margin-top:4px}
  .back{display:block;margin-top:16px;font-size:13px}
</style>
</head>
<body>
<div class="card">
  <div class="eyebrow">Test call &mdash; in your browser, no phone credit used</div>
  <div class="avatar" id="avatar">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
      <path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>
    </svg>
  </div>
  <h1>Talk to Aarav</h1>
  <p class="desc">Speak in Hindi or Hinglish, exactly as a prospect would.
  Interrupt any time.</p>

  <div class="engines" id="engines"></div>

  <button id="callBtn" class="btn" onclick="toggleCall()">Start call</button>
  <div class="status" id="status">Choose an engine and press start. Chrome or Edge.</div>
  <div class="meter"><div class="meter-fill" id="meter"></div></div>
  <div class="latency" id="latency"></div>

  <div class="chat-box" id="chatBox"></div>
  <a class="back" href="/app">&larr; Back to the dashboard</a>
</div>

<script>
const RATE = 16000;
let ws = null, audioCtx = null, micNode = null, micStream = null;
let isCalling = false, engine = 'gemini';   // best quality; falls back if unavailable
let playHead = 0, sources = [], speaking = false;
// Engines return different rates: Sarvam is synthesized at 16 kHz, Gemini Live
// returns 24 kHz. The server announces it; AudioBuffer resamples to the context.
let playbackRate = RATE;
let turnStart = 0;

const callBtn = document.getElementById('callBtn');
const chatBox = document.getElementById('chatBox');
const avatar  = document.getElementById('avatar');
const statusEl= document.getElementById('status');
const meter   = document.getElementById('meter');
const latencyEl = document.getElementById('latency');

// -- engine picker --------------------------------------------------------
async function loadEngines() {
  const res = await fetch('/api/v1/engines');
  const data = await res.json();
  const box = document.getElementById('engines');
  box.innerHTML = '';
  for (const [key, info] of Object.entries(data)) {
    const div = document.createElement('div');
    div.className = 'engine' + (info.available ? '' : ' disabled') + (key === engine ? ' active' : '');
    div.innerHTML = '<b>' + (key === 'sarvam' ? 'Sarvam AI' : 'Gemini Live') + '</b>'
      + '<span>' + info.kind + '</span>'
      + '<span class="model">' + info.model + '</span>'
      + (info.available ? '' : '<span class="why">' + (info.reason || 'unavailable') + '</span>');
    if (info.available) {
      div.onclick = () => { if (isCalling) return; engine = key; loadEngines(); };
    }
    box.appendChild(div);
  }
  // Never leave an unavailable engine selected.
  if (!data[engine] || !data[engine].available) {
    const fallback = Object.keys(data).find(k => data[k].available);
    if (fallback && fallback !== engine) { engine = fallback; loadEngines(); }
  }
}
loadEngines();

// Transcription arrives in fragments from both sides. Append to the bubble that
// belongs to whoever is currently talking, and start a new one when the speaker
// changes -- otherwise the whole call renders as one unbroken wall of text.
let openBubble = null, openSpeaker = null;

function addMessage(sender, text, streaming) {
  const label = sender === 'agent' ? 'Aarav: ' : sender === 'user' ? 'You: ' : '';

  if (streaming && openBubble && openSpeaker === sender) {
    openBubble.dataset.raw = ((openBubble.dataset.raw || '') + text).replace(/\s+/g, ' ');
    openBubble.textContent = label + openBubble.dataset.raw.trim();
  } else {
    const div = document.createElement('div');
    div.className = 'msg ' + sender;
    div.dataset.raw = text;
    div.textContent = label + text;
    chatBox.appendChild(div);
    openBubble = streaming ? div : null;
    openSpeaker = streaming ? sender : null;
  }
  chatBox.scrollTop = chatBox.scrollHeight;
}

function endTurn() { openBubble = null; openSpeaker = null; }

function setState(s) {
  const labels = { listening: 'Listening…', thinking: 'Aarav is thinking…',
                   transcribing: 'Understanding…', speaking: 'Aarav is speaking' };
  statusEl.textContent = labels[s] || s;
  speaking = (s === 'speaking');
  avatar.classList.toggle('speaking', speaking);
  if (s === 'speaking' && turnStart) {
    latencyEl.textContent = 'first audio in ' + ((performance.now() - turnStart) / 1000).toFixed(1) + 's';
    turnStart = 0;
  }
}

// -- gapless playback of streamed PCM ------------------------------------
function playPcm(arrayBuffer) {
  const pcm = new Int16Array(arrayBuffer);
  if (!pcm.length) return;
  const buf = audioCtx.createBuffer(1, pcm.length, playbackRate);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  // Schedule back-to-back so consecutive chunks play without a seam.
  const now = audioCtx.currentTime;
  if (playHead < now) playHead = now + 0.05;
  src.start(playHead);
  playHead += buf.duration;
  sources.push(src);
  src.onended = () => { sources = sources.filter(s => s !== src); };
}

function clearPlayback() {
  sources.forEach(s => { try { s.stop(); } catch (e) {} });
  sources = [];
  playHead = 0;
}

// -- call control ---------------------------------------------------------
async function toggleCall() { isCalling ? endCall() : startCall(); }

async function startCall() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
  } catch (e) {
    statusEl.textContent = 'Microphone blocked: ' + e.message;
    return;
  }

  audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: RATE });
  await audioCtx.resume();

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/v1/voice/live`
        + `?engine=${engine}&lead_name=Prospect&session_id=live_${Math.random().toString(36).slice(2)}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    playbackRate = RATE;
    isCalling = true;
    callBtn.textContent = 'Hang Up';
    callBtn.classList.add('stop');
    chatBox.innerHTML = '';
    endTurn();
    startMic();
  };

  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) { playPcm(ev.data); return; }
    const msg = JSON.parse(ev.data);
    if (msg.type === 'state') { if (msg.value === 'listening') endTurn(); setState(msg.value); }
    else if (msg.type === 'user') {
      addMessage('user', msg.text, msg.partial === true);
      turnStart = performance.now();
    }
    else if (msg.type === 'agent') addMessage('agent', msg.text, msg.partial === true);
    else if (msg.type === 'tool') { endTurn(); addMessage('sys', 'tool called: ' + msg.name); }
    else if (msg.type === 'format') { playbackRate = msg.sample_rate || RATE; }
    else if (msg.type === 'clear') clearPlayback();
    else if (msg.type === 'error') { addMessage('sys', 'error: ' + msg.message); statusEl.textContent = msg.message; }
  };

  ws.onclose = () => { if (isCalling) endCall(); };
  ws.onerror = () => { statusEl.textContent = 'Connection error.'; };
}

function startMic() {
  const source = audioCtx.createMediaStreamSource(micStream);
  // ScriptProcessor is deprecated but needs no separate worklet file, which
  // keeps this demo a single self-contained page.
  micNode = audioCtx.createScriptProcessor(1024, 1, 1);
  source.connect(micNode);
  micNode.connect(audioCtx.destination);

  micNode.onaudioprocess = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const input = e.inputBuffer.getChannelData(0);
    const pcm = new Int16Array(input.length);
    let peak = 0;
    for (let i = 0; i < input.length; i++) {
      const v = Math.max(-1, Math.min(1, input[i]));
      pcm[i] = v * 32767;
      peak = Math.max(peak, Math.abs(v));
    }
    meter.style.width = Math.min(100, peak * 220) + '%';
    ws.send(pcm.buffer);
  };
}

function endCall() {
  isCalling = false;
  callBtn.textContent = 'Start Live Call';
  callBtn.classList.remove('stop');
  setState('');
  statusEl.textContent = 'Call ended.';
  meter.style.width = '0%';
  clearPlayback();
  if (micNode) { micNode.onaudioprocess = null; micNode.disconnect(); micNode = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify({type: 'bye'})); ws.close(); }
  ws = null;
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
