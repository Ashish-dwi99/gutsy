"""The control socket between the MCP server and the daemon.

The MCP server is a child of the brain (Claude Code, Codex, or Chotu), so it
cannot reach Telegram itself. It asks the daemon over a Unix socket inside the
owner-only home directory, one newline-terminated JSON request per connection.

Every request carries the turn token the daemon minted for the running turn;
the daemon maps that token to its chat, so a request cannot address a chat of
its own choosing.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

ASK_TIMEOUT_SECONDS = 30 * 60
APPROVAL_TIMEOUT_SECONDS = 30 * 60
MAX_LINE_BYTES = 1_000_000


class ControlError(RuntimeError):
    pass


def call(socket_path: Path, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(str(socket_path))
        except OSError as exc:
            raise ControlError("the chotu-line daemon is not running") from exc
        conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > MAX_LINE_BYTES:
                raise ControlError("control reply too large")
    if not buffer:
        raise ControlError("the daemon closed the connection without a reply")
    reply = json.loads(buffer)
    if not reply.get("ok"):
        raise ControlError(str(reply.get("error") or "control request failed"))
    return reply
