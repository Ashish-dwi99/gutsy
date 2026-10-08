"""Gutsy: how each brain is invoked and how its output is read."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from gutsy.brains import TurnRequest
from gutsy.brains import chotu as chotu_brain
from gutsy.brains import claude as claude_brain
from gutsy.brains import codex as codex_brain
from gutsy.config import GutsyConfig
from gutsy.mcp_stdio import Tool, ToolFailure, serve
from gutsy.telegram import parse_update, split_message


@pytest.fixture(autouse=True)
def gutsy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GUTSY_HOME", str(tmp_path))
    return tmp_path


def test_claude_resumes_and_routes_permission_prompts_to_the_line(tmp_path: Path) -> None:
    request = TurnRequest(chat_id="1", prompt="book it", session_id="sess-1", token="tok")
    argv = claude_brain.build_argv("claude", GutsyConfig(), request, mcp_config=tmp_path / "m.json", instructions="be kind")
    assert argv[:3] == ["claude", "-p", "book it"]
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__gutsy__approve"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--resume") + 1] == "sess-1"
    assert argv[argv.index("--append-system-prompt") + 1] == "be kind"


def test_claude_stream_reader_returns_the_result_or_the_failure() -> None:
    reader = claude_brain.StreamReader()
    reader({"type": "system", "subtype": "init", "session_id": "s1"})
    reader({"type": "result", "subtype": "success", "is_error": False, "result": "booked", "session_id": "s1"})
    assert reader.finish(0, "") == claude_brain.TurnResult("booked", "s1")

    failed = claude_brain.StreamReader()
    failed({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "", "session_id": "s2"})
    assert failed.finish(1, "").error == "error_max_turns"
    assert "boom" in claude_brain.StreamReader().finish(1, "boom").error


def test_codex_puts_exec_flags_before_resume_and_mcp_config_as_toml() -> None:
    request = TurnRequest(chat_id="1", prompt="hi", session_id="th-1", token="tok")
    argv = codex_brain.build_argv("codex", GutsyConfig(browser=False), request)
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[-3:] == ["resume", "th-1", "hi"]
    configs = [argv[i + 1] for i, word in enumerate(argv) if word == "-c"]
    assert 'mcp_servers.gutsy.args=["-m", "gutsy.mcp_server"]' in configs
    env = next(item for item in configs if item.startswith("mcp_servers.gutsy.env="))
    assert 'GUTSY_TOKEN = "tok"' in env and env.endswith("}")


def test_codex_reconnect_errors_are_transient_and_turn_failed_is_not() -> None:
    reader = codex_brain.EventReader()
    for event in (
        {"type": "thread.started", "thread_id": "th-9"},
        {"type": "error", "message": "Reconnecting... 2/5"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done: 2 tickets"}},
        {"type": "turn.completed", "usage": {}},
    ):
        reader(event)
    assert reader.finish(0, "") == codex_brain.TurnResult("done: 2 tickets", "th-9")

    failed = codex_brain.EventReader()
    failed({"type": "turn.failed", "error": {"message": "usage limit"}})
    assert failed.finish(1, "").error == "usage limit"
    assert "401" in codex_brain.EventReader().finish(1, "401 Unauthorized").error


def test_chotu_turn_keeps_one_session_per_chat() -> None:
    payload = chotu_brain.turn_payload(TurnRequest(chat_id="42", prompt="hi", session_id=None, token="t"))
    assert payload["session_id"] == "line_42" and payload["response_mode"] == "text_text"
    assert chotu_brain.answer_text({"chat": {"answer": "hey"}}) == "hey"


def test_telegram_messages_and_button_taps_parse() -> None:
    message = parse_update(
        {"update_id": 5, "message": {"message_id": 9, "text": "hi", "chat": {"id": 7}, "from": {"id": 7, "username": "a"}}}
    )
    assert (message.chat_id, message.user_id, message.text, message.is_button) == ("7", "7", "hi", False)
    tap = parse_update(
        {"update_id": 6, "callback_query": {"id": "cb", "data": "apv:x:allow", "from": {"id": 7}, "message": {"message_id": 3, "chat": {"id": 7}}}}
    )
    assert (tap.is_button, tap.callback_data, tap.message_id) == (True, "apv:x:allow", "3")
    assert parse_update({"update_id": 7, "edited_message": {}}) is None
    assert all(len(chunk) <= 10 for chunk in split_message("word " * 20, limit=10))


def test_mcp_server_lists_calls_and_reports_tool_failures() -> None:
    def refuse(arguments: dict) -> str:
        raise ToolFailure("no vault item x")

    tools = [
        Tool("echo", "Echo.", {"type": "object", "properties": {}}, lambda arguments: {"got": arguments}),
        Tool("refuse", "Refuse.", {"type": "object", "properties": {}}, refuse),
    ]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"a": 1}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "refuse", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    ]
    out = io.StringIO()
    serve("t", "0", tools, stdin=io.StringIO("\n".join(json.dumps(r) for r in requests)), stdout=out)
    replies = {reply["id"]: reply for reply in map(json.loads, out.getvalue().splitlines())}
    assert replies[1]["result"]["protocolVersion"] == "2025-03-26"
    assert [tool["name"] for tool in replies[2]["result"]["tools"]] == ["echo", "refuse"]
    assert json.loads(replies[3]["result"]["content"][0]["text"]) == {"got": {"a": 1}}
    assert replies[4]["result"]["isError"] is True
    assert replies[5]["error"]["code"] == -32601
