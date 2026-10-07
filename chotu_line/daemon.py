"""The line's daemon: Telegram in, a brain's turn, Telegram out.

One turn runs per chat at a time; messages that arrive meanwhile wait and go
together as the next turn. A question or approval the brain is waiting on is
answered by the owner's next message or button tap. /stop cancels the turn
and kills the brain's process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import browser, context, schedule
from .brains import TurnRequest, TurnResult, make_brain
from .config import BRAINS, LineConfig
from .control import APPROVAL_TIMEOUT_SECONDS, ASK_TIMEOUT_SECONDS
from .store import LineStore
from .telegram import Inbound, TelegramBot, TelegramError, parse_update

log = logging.getLogger("chotu_line")

TYPING_INTERVAL_SECONDS = 4.5
SCHEDULER_TICK_SECONDS = 30
POLL_RETRY_SECONDS = 5
NO_UPDATE = "NO_UPDATE"
HELP = (
    "Text me what you need done.\n"
    "/stop stops the current task\n"
    "/new starts a fresh conversation\n"
    "/brain shows or switches the brain (claude, codex, chotu)\n"
    "/status shows what's running\n"
    "/forget erases what I know about you"
)
PRIVACY = (
    "Privacy: I run on your own computer, and what I learn about you stays there. "
    "Telegram chats with bots are not end-to-end encrypted, so Telegram can read them. "
    "Never send passwords or card numbers here."
)


class Stopped(Exception):
    """The owner sent /stop while the brain was waiting on them."""


class Channel(Protocol):
    async def send(self, chat_id: str, text: str, *, buttons: list[tuple[str, str]] | None = None) -> str: ...
    async def typing(self, chat_id: str) -> None: ...
    async def answer_button(self, callback_id: str, text: str = "") -> None: ...
    async def settle_buttons(self, chat_id: str, message_id: str, outcome: str) -> None: ...


@dataclass
class Pending:
    """A question or approval the brain is blocked on."""

    chat_id: str
    future: asyncio.Future[str]
    message_id: str = ""
    choices: list[str] = field(default_factory=list)


@dataclass
class ChatState:
    task: asyncio.Task[None] | None = None
    queued: list[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()


class Line:
    def __init__(self, config: LineConfig, store: LineStore, channel: Channel) -> None:
        self.config = config
        self.store = store
        self.channel = channel
        self.chats: dict[str, ChatState] = {}
        self.tokens: dict[str, str] = {}
        self.asks: list[tuple[str, Pending]] = []
        self.approvals: dict[str, Pending] = {}
        self.background: set[asyncio.Task[Any]] = set()
        if config.chotu_token and config.owner_chat_id:
            self.tokens[config.chotu_token] = config.owner_chat_id

    # inbound ------------------------------------------------------------
    async def handle(self, inbound: Inbound) -> None:
        if not self.config.paired:
            await self._try_pair(inbound)
            return
        if inbound.user_id != self.config.owner_user_id or inbound.chat_id != self.config.owner_chat_id:
            log.info("ignored message from non-owner user %s", inbound.user_id)
            return
        if inbound.is_button:
            await self._button(inbound)
            return
        text = inbound.text.strip()
        if not text:
            return
        if text.startswith("/"):
            await self._command(inbound.chat_id, text)
            return
        if self._answer_pending(inbound.chat_id, text):
            return
        state = self.chats.setdefault(inbound.chat_id, ChatState())
        if state.busy:
            state.queued.append(text)
            await self.channel.send(inbound.chat_id, "got it, I'll pick that up right after this one")
            return
        self._start_turn(inbound.chat_id, text)

    async def _try_pair(self, inbound: Inbound) -> None:
        code = self.config.pairing_code
        if not code or inbound.is_button or inbound.text.strip() != f"/start {code}":
            return
        self.config.owner_user_id = inbound.user_id
        self.config.owner_chat_id = inbound.chat_id
        self.config.pairing_code = ""
        self.config.save()
        if self.config.chotu_token:
            self.tokens[self.config.chotu_token] = inbound.chat_id
        log.info("paired with Telegram user %s", inbound.user_id)
        await self.channel.send(inbound.chat_id, f"paired. you're talking to {self.config.brain}.\n\n{HELP}\n\n{PRIVACY}")

    def _answer_pending(self, chat_id: str, text: str) -> bool:
        for index, (token, pending) in enumerate(self.asks):
            if pending.chat_id == chat_id and not pending.future.done():
                pending.future.set_result(text)
                del self.asks[index]
                return True
        return False

    async def _button(self, inbound: Inbound) -> None:
        kind, _, rest = inbound.callback_data.partition(":")
        pending_id, _, value = rest.partition(":")
        if kind == "apv" and pending_id in self.approvals:
            pending = self.approvals[pending_id]
            if not pending.future.done():
                pending.future.set_result(value)
            await self.channel.answer_button(inbound.callback_id)
            return
        if kind == "ans":
            for index, (token, pending) in enumerate(self.asks):
                if token == pending_id and not pending.future.done():
                    pending.future.set_result(pending.choices[int(value)])
                    del self.asks[index]
                    break
        await self.channel.answer_button(inbound.callback_id)

    async def _command(self, chat_id: str, text: str) -> None:
        command, _, argument = text.partition(" ")
        state = self.chats.setdefault(chat_id, ChatState())
        if command == "/stop":
            state.queued.clear()
            self._cancel_waits(chat_id)
            if state.busy:
                assert state.task is not None
                state.task.cancel()
                await self.channel.send(chat_id, "stopped.")
            else:
                await self.channel.send(chat_id, "nothing was running.")
        elif command == "/new":
            self.store.clear_sessions(chat_id)
            await self.channel.send(chat_id, "fresh start. what's next?")
        elif command == "/brain":
            await self._switch_brain(chat_id, argument.strip())
        elif command == "/forget":
            self.store.forget_everything()
            await self.channel.send(
                chat_id,
                "done: your saved details, memories, workstreams, and conversation history are erased. "
                "saved logins and schedules stay; manage them with chotu-line on your computer.",
            )
        elif command == "/status":
            await self.channel.send(chat_id, self._status_text(state))
        else:
            await self.channel.send(chat_id, HELP)

    async def _switch_brain(self, chat_id: str, name: str) -> None:
        if not name:
            await self.channel.send(chat_id, f"brain: {self.config.brain}\nswitch with /brain chotu, /brain claude, or /brain codex")
            return
        if name not in BRAINS:
            await self.channel.send(chat_id, f"I only know {', '.join(BRAINS)}.")
            return
        self.config.brain = name
        self.config.save()
        await self.channel.send(chat_id, f"switched to {name}. your saved info, workstreams, and schedules carry over.")

    def _status_text(self, state: ChatState) -> str:
        active = [item for item in self.store.schedules() if item["status"] == "active"]
        chrome = "on" if self.config.browser and browser.is_running(self.config.chrome_port) else "off"
        return (
            f"brain: {self.config.brain}\n"
            f"working: {'yes' if state.busy else 'no'}"
            + (f" ({len(state.queued)} waiting)" if state.queued else "")
            + f"\nschedules: {len(active)} active\nbrowser: {chrome}"
        )

    def _cancel_waits(self, chat_id: str) -> None:
        waiting = [pending for _, pending in self.asks] + list(self.approvals.values())
        for pending in waiting:
            if pending.chat_id == chat_id and not pending.future.done():
                pending.future.set_exception(Stopped())
        self.asks = [(token, pending) for token, pending in self.asks if pending.chat_id != chat_id]

    # turns --------------------------------------------------------------
    def _start_turn(self, chat_id: str, text: str) -> None:
        state = self.chats.setdefault(chat_id, ChatState())
        state.task = asyncio.create_task(self._interactive_turn(chat_id, text))
        state.task.add_done_callback(_log_failure)

    async def _interactive_turn(self, chat_id: str, text: str) -> None:
        brain_name = self.config.brain
        prompt = context.interactive_prompt(self.store, self.config.timezone, text, brain=brain_name)
        session_id = self.store.session_id(chat_id, brain_name)
        result = await self._run_brain(chat_id, prompt, session_id=session_id)
        if result.session_id:
            self.store.save_session(chat_id, brain_name, result.session_id)
        if result.error:
            log.warning("turn failed on %s: %s", brain_name, result.error)
            hint = "\n(send /new if this keeps happening)" if session_id else ""
            await self.channel.send(chat_id, f"that didn't work: {result.error[:600]}{hint}")
        elif result.text.strip():
            await self.channel.send(chat_id, result.text)
        state = self.chats[chat_id]
        if state.queued:
            queued, state.queued = "\n\n".join(state.queued), []
            self._start_turn(chat_id, queued)

    async def _run_brain(self, chat_id: str, prompt: str, *, session_id: str | None) -> TurnResult:
        token = secrets.token_urlsafe(18)
        self.tokens[token] = chat_id
        typing = asyncio.create_task(self._keep_typing(chat_id))
        brain = make_brain(self.config.brain, self.config, context.instructions("assistant"))
        try:
            return await brain.run(TurnRequest(chat_id=chat_id, prompt=prompt, session_id=session_id, token=token))
        finally:
            typing.cancel()
            self.tokens.pop(token, None)

    async def _keep_typing(self, chat_id: str) -> None:
        while True:
            try:
                await self.channel.typing(chat_id)
            except (TelegramError, httpx.HTTPError) as exc:
                log.debug("typing indicator failed: %s", exc)
            await asyncio.sleep(TYPING_INTERVAL_SECONDS)

    # schedules ----------------------------------------------------------
    async def run_due_schedules(self, now: float) -> None:
        for item in self.store.due_schedules(now):
            upcoming = schedule.next_run(item["rule"], now)
            self.store.update_schedule(
                item["id"],
                last_run_at=now,
                next_run_at=upcoming,
                status="active" if upcoming is not None else "done",
            )
            task = asyncio.create_task(self._scheduled_turn(item))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
            task.add_done_callback(_log_failure)

    async def _scheduled_turn(self, item: dict[str, Any]) -> None:
        prompt = context.scheduled_prompt(
            self.store, self.config.timezone, item["title"], item["prompt"], brain=self.config.brain
        )
        result = await self._run_brain(item["chat_id"], prompt, session_id=None)
        if result.error:
            await self.channel.send(item["chat_id"], f"scheduled task \"{item['title']}\" failed: {result.error[:600]}")
        elif result.text.strip() and result.text.strip() != NO_UPDATE:
            await self.channel.send(item["chat_id"], result.text)

    # control socket (requests from the MCP server) ----------------------
    async def control(self, request: dict[str, Any]) -> dict[str, Any]:
        chat_id = self.tokens.get(str(request.get("token") or ""))
        if chat_id is None:
            return {"ok": False, "error": "this turn has ended; the owner can no longer be reached from it"}
        op = request.get("op")
        if op == "send":
            await self.channel.send(chat_id, str(request["text"]))
            return {"ok": True}
        if op == "ask":
            return await self._ask(chat_id, str(request["token"]), str(request["question"]), list(request.get("choices") or []))
        if op == "approve":
            return await self._approve(chat_id, str(request["title"]), str(request["detail"]), bool(request.get("can_always")))
        return {"ok": False, "error": f"unknown op {op!r}"}

    async def _ask(self, chat_id: str, token: str, question: str, choices: list[str]) -> dict[str, Any]:
        pending = Pending(chat_id, asyncio.get_running_loop().create_future(), choices=choices)
        self.asks.append((token, pending))
        buttons = [(choice[:40], f"ans:{token}:{index}") for index, choice in enumerate(choices)]
        await self.channel.send(chat_id, question, buttons=buttons or None)
        try:
            answer = await asyncio.wait_for(pending.future, ASK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "the owner did not answer within 30 minutes"}
        except Stopped:
            return {"ok": False, "error": "the owner stopped this task"}
        finally:
            self.asks = [(key, value) for key, value in self.asks if value is not pending]
        return {"ok": True, "answer": answer}

    async def _approve(self, chat_id: str, title: str, detail: str, can_always: bool) -> dict[str, Any]:
        pending_id = secrets.token_hex(4)
        buttons = [("Allow", f"apv:{pending_id}:allow"), ("Deny", f"apv:{pending_id}:deny")]
        if can_always:
            buttons.append(("Always allow", f"apv:{pending_id}:always"))
        pending = Pending(chat_id, asyncio.get_running_loop().create_future())
        self.approvals[pending_id] = pending
        pending.message_id = await self.channel.send(chat_id, f"{title}\n{detail[:1200]}", buttons=buttons)
        try:
            decision = await asyncio.wait_for(pending.future, APPROVAL_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, Stopped):
            decision = "deny"
        finally:
            del self.approvals[pending_id]
        outcome = {"allow": "Allowed", "always": "Always allowed", "deny": "Denied"}[decision]
        await self.channel.settle_buttons(chat_id, pending.message_id, outcome)
        return {"ok": True, "decision": decision}


def _log_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("turn failed", exc_info=task.exception())


async def serve_control(line: Line, socket_path: Path) -> asyncio.AbstractServer:
    socket_path.unlink(missing_ok=True)

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            reply = await line.control(request)
        except (json.JSONDecodeError, KeyError) as exc:
            reply = {"ok": False, "error": f"bad control request: {exc}"}
        writer.write(json.dumps(reply).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(on_connect, path=str(socket_path))
    socket_path.chmod(0o600)
    return server


async def poll_telegram(line: Line, bot: TelegramBot, store: LineStore) -> None:
    offset = int(store.get_kv("telegram_offset", "0") or 0)
    while True:
        try:
            updates = await bot.updates(offset)
        except (TelegramError, httpx.HTTPError) as exc:
            log.warning("telegram poll failed, retrying: %s", exc)
            await asyncio.sleep(POLL_RETRY_SECONDS)
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            store.set_kv("telegram_offset", str(offset))
            inbound = parse_update(update)
            if inbound is None:
                continue
            try:
                await line.handle(inbound)
            except (TelegramError, httpx.HTTPError) as exc:
                log.warning("could not answer update %s: %s", update["update_id"], exc)


async def tick_schedules(line: Line) -> None:
    while True:
        await line.run_due_schedules(time.time())
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)
