"""Codex as the brain, on the owner's own ChatGPT login.

`codex exec --json` prints thread/turn/item events. Codex reads Gutsy's
instructions from AGENTS.md in the workspace (the daemon writes it), runs
shell commands in its workspace-write sandbox, and reaches Gutsy's tools
over MCP. Exec mode never pauses for approval, so consequential actions rely
on `request_approval`, as the instructions require.
"""

from __future__ import annotations

import json
import shutil
from typing import Any

from ..config import GutsyConfig
from .base import TurnRequest, TurnResult, mcp_servers, run_jsonl


def _toml(value: Any) -> str:
    """A TOML literal for `-c key=value` (JSON strings and arrays are valid TOML)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {_toml(item)}" for key, item in value.items()) + "}"
    return json.dumps(value)


def build_argv(codex: str, config: GutsyConfig, request: TurnRequest) -> list[str]:
    argv = [
        codex,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(config.workspace),
        "-s",
        "workspace-write",
    ]
    if config.codex_model:
        argv += ["-m", config.codex_model]
    for name, server in mcp_servers(config, request.token).items():
        for key, value in server.items():
            argv += ["-c", f"mcp_servers.{name}.{key}={_toml(value)}"]
    if request.session_id:
        argv += ["resume", request.session_id]
    argv.append(request.prompt)
    return argv


class EventReader:
    """Folds `codex exec --json` events into one result.

    `error` events are transient (reconnect attempts); only `turn.failed`
    ends a turn as a failure.
    """

    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.messages: list[str] = []
        self.failure: str | None = None
        self.completed = False
        self.last_error: str | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "thread.started":
            self.thread_id = str(event.get("thread_id") or "") or None
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                self.messages.append(str(item["text"]))
        elif kind == "turn.completed":
            self.completed = True
        elif kind == "turn.failed":
            self.failure = str((event.get("error") or {}).get("message") or "turn failed")
        elif kind == "error":
            self.last_error = str(event.get("message") or "")

    def finish(self, code: int, stderr: str) -> TurnResult:
        text = self.messages[-1] if self.messages else ""
        if self.failure:
            return TurnResult(text, self.thread_id, error=self.failure)
        if not self.completed:
            detail = self.last_error or stderr.strip()[-500:]
            return TurnResult(text, self.thread_id, error=f"codex exited {code}: {detail}")
        return TurnResult(text, self.thread_id)


class CodexBrain:
    name = "codex"

    def __init__(self, config: GutsyConfig) -> None:
        self.config = config

    @staticmethod
    def available() -> bool:
        return shutil.which("codex") is not None

    async def run(self, request: TurnRequest) -> TurnResult:
        codex = shutil.which("codex")
        if codex is None:
            return TurnResult("", None, error="the codex CLI is not installed")
        reader = EventReader()
        code, stderr = await run_jsonl(build_argv(codex, self.config, request), cwd=self.config.workspace, on_event=reader)
        return reader.finish(code, stderr)
