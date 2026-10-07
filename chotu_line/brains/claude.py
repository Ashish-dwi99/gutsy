"""Claude Code as the brain, on the owner's own subscription.

`claude -p` streams JSON events. Its tool-permission prompts go to the line's
`approve` MCP tool, which asks the owner on Telegram when a call is not safe
to allow outright.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..config import LineConfig
from .base import TurnRequest, TurnResult, mcp_servers, run_jsonl

PERMISSION_TOOL = "mcp__chotu_line__approve"


def build_argv(
    claude: str, config: LineConfig, request: TurnRequest, *, mcp_config: Path, instructions: str
) -> list[str]:
    argv = [
        claude,
        "-p",
        request.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(mcp_config),
        "--permission-prompt-tool",
        PERMISSION_TOOL,
        "--append-system-prompt",
        instructions,
    ]
    if request.session_id:
        argv += ["--resume", request.session_id]
    if config.claude_model:
        argv += ["--model", config.claude_model]
    return argv


class StreamReader:
    """Folds `claude -p --output-format stream-json` events into one result."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.result: dict[str, Any] | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        if event.get("session_id"):
            self.session_id = str(event["session_id"])
        if event.get("type") == "result":
            self.result = event

    def finish(self, code: int, stderr: str) -> TurnResult:
        result = self.result
        if result is None:
            return TurnResult("", self.session_id, error=f"claude exited {code}: {stderr.strip()[-500:]}")
        text = str(result.get("result") or "")
        if result.get("is_error") or result.get("subtype") != "success":
            return TurnResult(text, self.session_id, error=text or str(result.get("subtype")))
        return TurnResult(text, self.session_id)


class ClaudeBrain:
    name = "claude"

    def __init__(self, config: LineConfig, instructions: str) -> None:
        self.config = config
        self.instructions = instructions

    @staticmethod
    def available() -> bool:
        return shutil.which("claude") is not None

    async def run(self, request: TurnRequest) -> TurnResult:
        claude = shutil.which("claude")
        if claude is None:
            return TurnResult("", None, error="the claude CLI is not installed")
        run_dir = self.config.home / "run"
        run_dir.mkdir(exist_ok=True)
        mcp_config = run_dir / f"{request.token}.mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": mcp_servers(self.config, request.token)}), encoding="utf-8")
        mcp_config.chmod(0o600)
        reader = StreamReader()
        try:
            argv = build_argv(claude, self.config, request, mcp_config=mcp_config, instructions=self.instructions)
            code, stderr = await run_jsonl(argv, cwd=self.config.workspace, on_event=reader)
        finally:
            mcp_config.unlink(missing_ok=True)
        return reader.finish(code, stderr)
