from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .. import browser
from ..config import LineConfig

STDERR_TAIL_CHARS = 2_000
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@latest"


@dataclass(frozen=True)
class TurnRequest:
    chat_id: str
    prompt: str
    session_id: str | None
    token: str


@dataclass(frozen=True)
class TurnResult:
    text: str
    session_id: str | None
    error: str | None = None


class Brain(Protocol):
    name: str

    async def run(self, request: TurnRequest) -> TurnResult: ...


def line_mcp_server(config: LineConfig, token: str) -> dict[str, Any]:
    """How a brain starts the line's MCP server (stdio)."""
    package_root = str(Path(__file__).resolve().parents[2])
    return {
        "command": sys.executable,
        "args": ["-m", "chotu_line.mcp_server"],
        "env": {
            "CHOTU_LINE_TOKEN": token,
            "CHOTU_LINE_HOME": str(config.home),
            "PYTHONPATH": package_root,
        },
    }


def playwright_mcp_server(config: LineConfig) -> dict[str, Any] | None:
    """Playwright MCP attached to the line's Chrome, when the owner has Node."""
    npx = shutil.which("npx")
    if not config.browser or npx is None:
        return None
    return {
        "command": npx,
        "args": ["-y", PLAYWRIGHT_MCP_PACKAGE, "--cdp-endpoint", browser.endpoint(config.chrome_port)],
    }


def mcp_servers(config: LineConfig, token: str) -> dict[str, dict[str, Any]]:
    servers = {"chotu_line": line_mcp_server(config, token)}
    playwright = playwright_mcp_server(config)
    if playwright is not None:
        servers["playwright"] = playwright
    return servers


async def run_jsonl(
    argv: list[str],
    *,
    cwd: Path,
    on_event: Callable[[dict[str, Any]], None],
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a CLI that prints JSON lines; returns (exit code, stderr tail).

    Cancelling the awaiting task kills the process, so /stop on the phone
    really stops the brain.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env={**os.environ, **(env or {})},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )
    stderr_chunks: list[bytes] = []

    async def drain_stderr() -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(65536):
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("{"):
                continue
            on_event(json.loads(line))
        code = await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    finally:
        await stderr_task
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return code, stderr[-STDERR_TAIL_CHARS:]
