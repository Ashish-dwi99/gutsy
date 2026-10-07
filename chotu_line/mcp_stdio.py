"""A minimal MCP server over stdio: initialize, tools/list, tools/call.

Hand-written instead of the MCP SDK so the line installs with no heavy
dependency and does not break when the SDK reshapes its API (2.x renamed
FastMCP). The wire format is JSON-RPC 2.0, one message per line.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, TextIO

PROTOCOL_VERSION = "2025-06-18"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def listing(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


class ToolFailure(Exception):
    """A tool refused or failed; the message goes back to the model as an error result."""


def serve(name: str, version: str, tools: list[Tool], *, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    by_name = {tool.name: tool for tool in tools}

    def reply(message_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
        payload["error" if error else "result"] = error or result
        stdout.write(json.dumps(payload) + "\n")
        stdout.flush()

    for line in stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, message_id = message.get("method"), message.get("id")
        if message_id is None:
            continue  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
            reply(
                message_id,
                result={
                    "protocolVersion": requested,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": name, "version": version},
                },
            )
        elif method == "ping":
            reply(message_id, result={})
        elif method == "tools/list":
            reply(message_id, result={"tools": [tool.listing() for tool in tools]})
        elif method == "tools/call":
            params = message.get("params") or {}
            tool = by_name.get(params.get("name"))
            if tool is None:
                reply(message_id, error={"code": -32602, "message": f"unknown tool {params.get('name')}"})
                continue
            reply(message_id, result=_call(tool, params.get("arguments") or {}))
        else:
            reply(message_id, error={"code": -32601, "message": f"method not found: {method}"})


def _call(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        value = tool.handler(arguments)
    except ToolFailure as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}
