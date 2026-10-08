"""Gutsy: the daemon's conversation flow with a fake Telegram and a fake brain."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from gutsy import daemon as daemon_module
from gutsy import schedule
from gutsy.brains import TurnRequest, TurnResult
from gutsy.config import GutsyConfig
from gutsy.daemon import Gutsy, serve_control
from gutsy.store import GutsyStore
from gutsy.telegram import Inbound
from gutsy.tools import GutsyTools

OWNER = "100"


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list | None]] = []
        self.settled: list[str] = []

    async def send(self, chat_id: str, text: str, *, buttons=None) -> str:
        self.sent.append((chat_id, text, buttons))
        return str(len(self.sent))

    async def typing(self, chat_id: str) -> None:
        return None

    async def answer_button(self, callback_id: str, text: str = "") -> None:
        return None

    async def settle_buttons(self, chat_id: str, message_id: str, outcome: str) -> None:
        self.settled.append(outcome)

    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


class FakeBrain:
    """Records each turn; `gate` lets a test hold a turn open."""

    name = "claude"

    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []
        self.gate: asyncio.Event | None = None
        self.reply = "done"

    async def run(self, request: TurnRequest) -> TurnResult:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        return TurnResult(self.reply, f"session-{len(self.requests)}")


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch):
    # AF_UNIX paths are capped near 104 bytes on macOS; pytest's tmp_path is longer.
    path = Path(tempfile.mkdtemp(prefix="cl", dir="/tmp"))
    monkeypatch.setenv("GUTSY_HOME", str(path))
    (path / "workspace").mkdir()
    yield path
    shutil.rmtree(path)


@pytest.fixture
def brain(monkeypatch: pytest.MonkeyPatch) -> FakeBrain:
    fake = FakeBrain()
    monkeypatch.setattr(daemon_module, "make_brain", lambda name, config, instructions: fake)
    return fake


def make_gutsy(home: Path, *, paired: bool = True) -> tuple[Gutsy, FakeChannel]:
    config = GutsyConfig(brain="claude", timezone="Asia/Kolkata", browser=False, pairing_code="abcd")
    if paired:
        config.owner_user_id = config.owner_chat_id = OWNER
    config.save()
    channel = FakeChannel()
    return Gutsy(config, GutsyStore(config.db_path), channel), channel


def text(body: str, user: str = OWNER) -> Inbound:
    return Inbound(update_id=1, chat_id=user, user_id=user, text=body)


async def settle(gutsy: Gutsy) -> None:
    for state in gutsy.chats.values():
        while state.task is not None and not state.task.done():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_only_the_pairing_code_pairs_and_strangers_are_ignored(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home, paired=False)
    await gutsy.handle(text("/start wrong", user="7"))
    assert channel.sent == [] and not gutsy.config.paired
    await gutsy.handle(text("/start abcd", user="7"))
    assert GutsyConfig.load().owner_user_id == "7" and GutsyConfig.load().pairing_code == ""
    await gutsy.handle(text("book a table", user="8"))
    await settle(gutsy)
    assert brain.requests == []


@pytest.mark.asyncio
async def test_a_turn_carries_context_and_its_session_continues(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home)
    gutsy.store.update_personal_info({"city": "Pune"})
    await gutsy.handle(text("movie tonight?"))
    await settle(gutsy)
    await gutsy.handle(text("the 9pm one"))
    await settle(gutsy)
    first, second = brain.requests
    assert "owner info: city=Pune" in first.prompt and first.prompt.endswith("movie tonight?")
    assert first.session_id is None and second.session_id == "session-1"
    assert channel.texts() == ["done", "done"]


@pytest.mark.asyncio
async def test_messages_during_a_turn_wait_and_go_together(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home)
    brain.gate = asyncio.Event()
    await gutsy.handle(text("find flights to Goa"))
    await asyncio.sleep(0)
    await gutsy.handle(text("window seat"))
    await gutsy.handle(text("under 6k"))
    brain.gate.set()
    await settle(gutsy)
    await settle(gutsy)
    assert len(brain.requests) == 2
    assert brain.requests[1].prompt.endswith("window seat\n\nunder 6k")


@pytest.mark.asyncio
async def test_stop_cancels_the_turn(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home)
    brain.gate = asyncio.Event()
    await gutsy.handle(text("cancel netflix"))
    await asyncio.sleep(0)
    await gutsy.handle(text("/stop"))
    await asyncio.sleep(0.05)
    assert gutsy.chats[OWNER].task.cancelled()
    assert channel.texts()[-1] == "stopped."
    assert gutsy.tokens == {}


@pytest.mark.asyncio
async def test_the_brain_reaches_the_owner_through_the_socket(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home)
    server = await serve_control(gutsy, gutsy.config.socket_path)
    token = "turn-token"
    gutsy.tokens[token] = OWNER
    tools = GutsyTools(gutsy.config, gutsy.store, token)
    async with server:
        await asyncio.to_thread(tools.send_message, {"text": "on it"})
        assert channel.texts() == ["on it"]

        asking = asyncio.create_task(asyncio.to_thread(tools.ask_user, {"question": "the OTP?"}))
        while len(channel.sent) < 2:
            await asyncio.sleep(0.01)
        await gutsy.handle(text("482913"))
        assert await asking == "482913"
        assert brain.requests == []  # the answer did not start a new turn

        approving = asyncio.create_task(
            asyncio.to_thread(tools.request_approval, {"action": "Pay ₹740", "details": "PVR Phoenix, 2 seats"})
        )
        while len(channel.sent) < 3:
            await asyncio.sleep(0.01)
        _, card, buttons = channel.sent[2]
        assert card.startswith("Pay ₹740") and [label for label, _ in buttons] == ["Allow", "Deny"]
        allow = buttons[0][1]
        await gutsy.handle(Inbound(update_id=2, chat_id=OWNER, user_id=OWNER, callback_id="cb", callback_data=allow))
        assert (await approving).startswith("approved")
        assert gutsy.store.has_active_grant(time.time())
        assert channel.settled == ["Allowed"]

        stale = GutsyTools(gutsy.config, gutsy.store, "ended-turn")
        with pytest.raises(Exception, match="turn has ended"):
            await asyncio.to_thread(stale.send_message, {"text": "late"})


@pytest.mark.asyncio
async def test_always_allow_trusts_the_tool_from_then_on(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home)
    server = await serve_control(gutsy, gutsy.config.socket_path)
    gutsy.tokens["t"] = OWNER
    tools = GutsyTools(gutsy.config, gutsy.store, "t")
    async with server:
        asking = asyncio.create_task(asyncio.to_thread(tools.approve, {"tool_name": "mcp__gmail__archive", "input": {"id": "m1"}}))
        while not channel.sent:
            await asyncio.sleep(0.01)
        always = channel.sent[0][2][2][1]
        await gutsy.handle(Inbound(update_id=3, chat_id=OWNER, user_id=OWNER, callback_id="cb", callback_data=always))
        assert '"behavior": "allow"' in await asking
        again = await asyncio.to_thread(tools.approve, {"tool_name": "mcp__gmail__archive", "input": {"id": "m2"}})
        assert '"m2"' in again and len(channel.sent) == 1


@pytest.mark.asyncio
async def test_due_schedule_runs_once_and_quiet_results_stay_quiet(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home)
    now = time.time()
    rule = {"kind": "once", "at": now - 1}
    item = gutsy.store.add_schedule(chat_id=OWNER, title="price check", prompt="check the price", rule=rule, next_run_at=now - 1)
    brain.reply = "NO_UPDATE"
    await gutsy.run_due_schedules(now)
    await asyncio.gather(*gutsy.background)
    assert gutsy.store.schedule(item["id"])["status"] == "done"
    assert "Scheduled task: price check" in brain.requests[0].prompt and brain.requests[0].session_id is None
    assert channel.sent == []
    await gutsy.run_due_schedules(now + 60)
    assert len(brain.requests) == 1


def test_interval_schedule_advances_from_when_it_ran() -> None:
    rule = schedule.build_rule(kind="interval", timezone="UTC", every_minutes=30)
    assert schedule.next_run(rule, 1_000) == 1_000 + 1_800


@pytest.mark.asyncio
async def test_forget_erases_what_the_line_knows_but_keeps_logins_and_schedules(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home)
    gutsy.store.update_personal_info({"city": "Pune"})
    gutsy.store.remember("prefers aisle seats")
    gutsy.store.save_session(OWNER, "claude", "s1")
    gutsy.store.add_vault_item(kind="login", label="GitHub", origins=["https://github.com"], hint="oc…")
    gutsy.store.add_schedule(chat_id=OWNER, title="t", prompt="p", rule={"kind": "interval", "every_minutes": 30}, next_run_at=1.0)
    await gutsy.handle(text("/forget"))
    assert gutsy.store.personal_info() == {} and gutsy.store.memories() == []
    assert gutsy.store.session_id(OWNER, "claude") is None
    assert len(gutsy.store.vault_items()) == 1 and len(gutsy.store.schedules()) == 1
    assert channel.texts()[-1].startswith("done")


@pytest.mark.asyncio
async def test_pairing_message_discloses_that_telegram_can_read_chats(home: Path, brain: FakeBrain) -> None:
    gutsy, channel = make_gutsy(home, paired=False)
    await gutsy.handle(text("/start abcd", user="7"))
    assert "not end-to-end encrypted" in channel.texts()[-1]


def test_every_command_in_the_menu_is_handled() -> None:
    from gutsy.daemon import COMMANDS, HELP

    for name, _ in COMMANDS:
        assert f"/{name}" in HELP
