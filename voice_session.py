"""
Conversation state for one call: LLM reasoning, real tool calling, and history.

Notable fixes over the first prototype:
  * Tools are actually offered to the model. Previously TOOL_DEFINITIONS was
    imported but never sent, and side-effects fired on substring matches against
    the caller's words -- so "main demo nahi chahta" booked a demo.
  * API failures are logged and flagged instead of being swallowed by a bare
    `except: pass` that returned a canned pitch indistinguishable from a real answer.
  * One aiohttp session is reused; a new pool per turn was adding handshake
    latency to every reply on a latency-critical path.
"""
import os
import json
import logging
from typing import Optional, List, Dict, Any

import aiohttp
from dotenv import load_dotenv

from sales_tools import TOOL_DEFINITIONS, execute_tool_call

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
logger = logging.getLogger("voice_session")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = os.getenv("VOICE_LLM_MODEL", "meta-llama/llama-3.3-70b-instruct")

SYSTEM_PROMPT = """You are Aarav, a warm, polite and persuasive sales specialist calling from your company.
You are speaking to {lead_name} (phone {lead_phone}), during an operator-authorized test call.

LANGUAGE & TONE
1. Speak natural corporate Hinglish -- Hindi grammar with the English business words Indians actually use.
2. Write Hinglish in Latin script. It is transliterated to Devanagari before synthesis, so spell phonetically
   the way the word is pronounced ("hoon" not "hu", "chahiye" not "chahie").
3. Hard limit: ONE sentence, at most about 25 words. Two only if the second is the question.
   Long turns are the single biggest thing that makes a voice agent feel robotic -- the
   prospect waits through a monologue instead of having a conversation.
4. End almost every turn with a question so the prospect keeps talking.
5. Never use markdown (*, #, -), bullet points, emoji, or URLs. Everything you write is spoken aloud.
6. Write numbers as words where natural. Say "pandrah minute", not "15 min".

SALES FACTS
- No pricing or performance facts are configured. Do not invent them.

TOOLS - USE THEM, DO NOT DESCRIBE THEM
You have real tools. Calling one performs a real action; talking about it does not.
- Prospect wants details in writing -> take their email and book the demo; the
  invite email carries the details.
- Prospect shows interest in a demo -> you MUST call check_calendar_slots.
- Prospect picks a time -> you MUST call book_demo_slot.
Call the tool first, then tell them what happened. Never describe an action you
have not called the tool for.

BEHAVIOUR
- If they ask to be removed or mention DND, apologise once and stop the conversation. Never claim removal unless a suppression tool confirms it.

NEVER CLAIM AN ACTION YOU HAVE NOT TAKEN
You can only book a slot or send a WhatsApp by calling the matching tool. If no
tool ran, you have not done it. Say what will happen instead: "main aapko confirm
karke bhej doonga" -- never "maine slot block kar diya hai" or "aapko details mil
jayengi". A prospect who is promised a WhatsApp that never arrives is worse than
one who was told nothing.

NEVER NARRATE YOUR OWN REASONING
Everything you produce is spoken aloud to a real person. Do not say things like
"The user just asked me to..." or "I should now...". Speak only the words Aarav
would say on the phone.
"""


def build_greeting(lead_name: str = "", company: str = "your company",
                   agent_name: str = "Aarav", reason: str = "") -> str:
    """The opening line.

    Naming the right company is not cosmetic: a prospect greeted by a company they
    never contacted spends the call working out who rang. And when no name is
    known, greet without one -- the placeholder used to land as the literal
    "Namaste ji ji!", which sounds like a machine reading a form.
    """
    name = (lead_name or "").strip()
    if name.lower() in ("", "ji", "unknown", "there", "sir", "madam"):
        opening = "Namaste!"
    else:
        # Avoid repeating honorifics in the generated greeting.
        first = name.split()[0]
        opening = f"Namaste {first} ji!"

    why = (reason or "").strip()
    if why:
        why = why[0].upper() + why[1:]
        why = why.rstrip(".") + "."
        context = f" {why}"
    else:
        context = ""

    return (f"{opening} Main {agent_name} bol raha hoon {company} se."
            f"{context} Kya aapke paas do minute hain baat karne ke liye?")


def company_of(agent, tenant=None) -> str:
    """Whose name the agent gives on the call.

    Falls back to the tenant's own company name rather than a hardcoded one: a
    company that skipped onboarding should still introduce itself as itself.
    """
    if agent:
        try:
            name = (json.loads(agent["profile_json"] or "{}") or {}).get("company_name")
            if name:
                return name
        except (json.JSONDecodeError, TypeError):
            pass
    if tenant is not None:
        try:
            if tenant["company_name"]:
                return tenant["company_name"]
        except (KeyError, TypeError):
            pass
    return "our team"


class VoiceSession:
    def __init__(self, call_id: str, lead_name: str, lead_phone: str, telephony_ws=None):
        self.call_id = call_id
        self.lead_name = lead_name
        self.lead_phone = lead_phone
        self.telephony_ws = telephony_ws
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()

        self.conversation_history: List[Dict[str, Any]] = [
            {"role": "system",
             "content": SYSTEM_PROMPT.format(lead_name=lead_name, lead_phone=lead_phone)}
        ]
        self.tool_context: Dict[str, Any] = {}
        self.tool_events: List[Dict[str, Any]] = []
        self.should_hang_up = False
        self.last_error: Optional[str] = None
        self.llm_degraded = False          # True when a reply came from the fallback
        self._session: Optional[aiohttp.ClientSession] = None

    # -- infrastructure ---------------------------------------------------
    def _llm_configured(self) -> bool:
        return bool(self.openrouter_key) and not self.openrouter_key.startswith("your_")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.openrouter_key}",
                         "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=12),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def get_initial_greeting(self) -> str:
        greeting = build_greeting(self.lead_name)
        self.conversation_history.append({"role": "assistant", "content": greeting})
        return greeting

    # -- LLM --------------------------------------------------------------
    async def _call_llm_api(self, messages: List[Dict[str, Any]],
                            tools: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """Returns the raw assistant message, or None if the call failed.
        None is distinguishable from a real reply so callers can flag degradation."""
        if not self._llm_configured():
            self.last_error = "OPENROUTER_API_KEY missing"
            return None

        payload: Dict[str, Any] = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 90,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            session = await self._get_session()
            async with session.post(OPENROUTER_URL, json=payload) as resp:
                body = await resp.text()
                if resp.status == 200:
                    data = json.loads(body)
                    choices = data.get("choices") or []
                    if choices:
                        self.last_error = None
                        return choices[0].get("message")
                    self.last_error = "LLM returned no choices"
                else:
                    self.last_error = f"LLM HTTP {resp.status}: {body[:200]}"
        except Exception as exc:
            self.last_error = f"LLM request failed: {exc}"

        logger.error("[%s] %s", self.call_id, self.last_error)
        return None

    def _fallback_reply(self) -> str:
        return (
            f"Ji {self.lead_name} ji, hamare Voice Agent ka setup sirf pandrah minute me ho jata hai, "
            f"aur cost lagbhag ek se dedh rupaye per minute padti hai. "
            f"Kya main aapke liye ek live demo book kar doon?"
        )

    # -- turn handling ----------------------------------------------------
    async def process_user_turn(self, user_transcript: str) -> str:
        """Runs one turn, including any tool calls the model requests."""
        if not user_transcript.strip():
            return ""

        self.conversation_history.append({"role": "user", "content": user_transcript})
        self.llm_degraded = False

        # Up to two rounds so the model can call a tool and then speak the result.
        for _ in range(2):
            message = await self._call_llm_api(self.conversation_history, tools=TOOL_DEFINITIONS)

            if message is None:
                reply = self._fallback_reply()
                self.llm_degraded = True
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                reply = (message.get("content") or "").strip()
                if not reply:
                    reply = self._fallback_reply()
                    self.llm_degraded = True
                self.conversation_history.append({"role": "assistant", "content": reply})
                self._check_for_hangup(reply)
                return reply

            # The model asked for tools -- run them and loop so it can respond.
            self.conversation_history.append(message)
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                args = self._with_defaults(name, raw_args)
                result = await execute_tool_call(name, json.dumps(args),
                                                 {**self.tool_context, "lead_name": self.lead_name,
                                                  "lead_phone": self.lead_phone})
                self.tool_events.append({"tool": name, "args": args, "result": result})
                self.conversation_history.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "name": name,
                    "content": json.dumps(result),
                })

        # Both rounds wanted tools; close the turn gracefully.
        reply = self._fallback_reply()
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    async def stream_reply(self, user_transcript: str):
        """Yields reply text deltas as the model writes them.

        Waiting for a complete reply before synthesizing costs a full generation
        of dead air. Streaming lets the caller synthesize sentence by sentence,
        so the agent starts speaking roughly a second in. Tool calls are not
        streamed -- if the model wants one, fall back to the blocking path.
        """
        if not user_transcript.strip():
            return

        self.conversation_history.append({"role": "user", "content": user_transcript})
        self.llm_degraded = False

        if not self._llm_configured():
            self.last_error = "OPENROUTER_API_KEY missing"
            self.llm_degraded = True
            reply = self._fallback_reply()
            self.conversation_history.append({"role": "assistant", "content": reply})
            yield reply
            return

        payload = {
            "model": LLM_MODEL,
            "messages": self.conversation_history,
            "temperature": 0.4,
            "max_tokens": 160,
            "stream": True,
        }

        collected = ""
        try:
            session = await self._get_session()
            async with session.post(OPENROUTER_URL, json=payload) as resp:
                if resp.status != 200:
                    self.last_error = f"LLM HTTP {resp.status}: {(await resp.text())[:200]}"
                    raise RuntimeError(self.last_error)
                async for raw in resp.content:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta", {})
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    piece = delta.get("content") or ""
                    if piece:
                        collected += piece
                        yield piece
        except Exception as exc:
            self.last_error = self.last_error or f"LLM stream failed: {exc}"
            logger.error("[%s] %s", self.call_id, self.last_error)

        if collected.strip():
            self.conversation_history.append({"role": "assistant", "content": collected.strip()})
            self._check_for_hangup(collected)
        else:
            self.llm_degraded = True
            reply = self._fallback_reply()
            self.conversation_history.append({"role": "assistant", "content": reply})
            yield reply

    def _with_defaults(self, name: str, raw_args: str) -> Dict[str, Any]:
        """Normalises tool arguments before execution."""
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except json.JSONDecodeError:
            logger.warning("[%s] bad tool arguments for %s: %r", self.call_id, name, raw_args)
            args = {}
        # Contact details are overwritten, not defaulted. The model echoes the
        # number back from the prompt and can mangle or invent one, and these
        # tools send real messages -- the session's value is the only trusted source.
        if args.get("lead_phone") not in (None, self.lead_phone):
            logger.warning("[%s] %s proposed lead_phone=%r; using session value",
                           self.call_id, name, args.get("lead_phone"))
        args["lead_phone"] = self.lead_phone
        if name == "book_demo_slot":
            args["lead_name"] = self.lead_name
        return args

    def _check_for_hangup(self, reply: str) -> None:
        lowered = reply.lower()
        if any(p in lowered for p in ("list se hata", "remove kar diya", "number hata di",
                                      "aage se call nahi", "dnd")):
            self.should_hang_up = True


# -- session registry ------------------------------------------------------
# The browser demo used to build a fresh VoiceSession per HTTP request, so the
# agent restarted from the system prompt on every turn and could never remember
# what the prospect had just said.
_SESSIONS: Dict[str, VoiceSession] = {}


def get_or_create_session(call_id: str, lead_name: str, lead_phone: str) -> VoiceSession:
    session = _SESSIONS.get(call_id)
    if session is None:
        session = VoiceSession(call_id=call_id, lead_name=lead_name, lead_phone=lead_phone)
        _SESSIONS[call_id] = session
    return session


async def drop_session(call_id: str) -> None:
    session = _SESSIONS.pop(call_id, None)
    if session:
        await session.close()


# Operator-provided company facts override the generic fallback prompt.
TENANT_PROMPT = """You are {agent_name}, a warm, polite sales specialist calling on
behalf of {company}.

WHO YOU ARE CALLING FOR
{company} — {one_liner}
What they sell: {sells}
Who they sell to: {audience}

FACTS YOU MAY STATE (these are the only ones; never invent a price, a number or a
guarantee that is not here)
{facts}

THIS CALL
You are speaking to {lead_name} ({lead_phone}).
Why you are calling: {reason}
Your goal: {goal}

LANGUAGE & TONE
1. Speak natural corporate Hinglish — Hindi grammar with the English business words
   Indians actually use. If the prospect answers in English, follow them.
2. Write Hinglish in Latin script, spelled the way it is pronounced ("hoon" not "hu").
3. Hard limit: ONE sentence, about 25 words. Two only if the second is the question.
4. Open by naming yourself and {company} clearly — the prospect must know who called.
5. End almost every turn with a question so they keep talking.
6. Never use markdown, bullets, emoji or URLs. Everything you write is spoken aloud.
7. Say numbers as words: "pandrah minute", not "15 min".

TOOLS — USE THEM, DO NOT DESCRIBE THEM
Calling a tool performs a real action; talking about it does not.
- Prospect shows interest in a demo -> you MUST call check_calendar_slots.
- Prospect picks a time -> you MUST call book_demo_slot.
Call the tool first, then say what happened. Never claim an action you did not take.

NEVER NARRATE YOUR OWN REASONING
Do not say "The user just asked me to...". Speak only what {agent_name} would say.

IF THEY ASK TO BE REMOVED
Apologise once and stop the conversation. Never claim removal unless a suppression tool confirms it.
"""


def build_system_prompt(agent=None, lead_name: str = "ji",
                        lead_phone: str = "the number on file",
                        reason: str = "") -> str:
    """Compose the call prompt from the tenant's own company profile."""
    if not agent or not (agent["one_liner"] or agent["company_blurb"]):
        return SYSTEM_PROMPT.format(lead_name=lead_name, lead_phone=lead_phone)

    # The profile JSON is the source of truth for the company's own name.
    company_name = ""
    try:
        company_name = (json.loads(agent["profile_json"] or "{}")
                        .get("company_name") or "")
    except (json.JSONDecodeError, TypeError):
        pass
    if not company_name:
        company_name = "our team"

    raw_facts = (agent["key_facts"] or "").strip()
    facts = [f.strip() for f in raw_facts.split(";") if f.strip()]
    fact_lines = chr(10).join(f"- {f}" for f in facts) or "- (none recorded)"

    return TENANT_PROMPT.format(
        agent_name=agent["name"] or "Aarav",
        company=company_name,
        one_liner=agent["one_liner"] or "",
        sells=agent["company_blurb"] or "",
        audience=agent["who_they_sell_to"] or "businesses",
        facts=fact_lines,
        lead_name=lead_name,
        lead_phone=lead_phone,
        reason=reason or "Operator-authorized test call",
        goal=agent["goal"] or "Book a short demo.",
    )
