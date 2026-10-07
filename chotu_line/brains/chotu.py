"""Chotu as the brain: one turn through the local Chotu hub.

Chotu runs its own agent (kimi-agent-rs) with its own browser, memory, and
approvals. It reaches the line's tools through the MCP server `chotu-line
setup` registers in the hub's mcp_servers.json, which carries Chotu's
standing token instead of a per-turn one.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import LineConfig
from .base import TurnRequest, TurnResult

TURN_TIMEOUT_SECONDS = 15 * 60


def turn_payload(request: TurnRequest) -> dict[str, Any]:
    return {
        "transcript": request.prompt,
        "source": "text",
        "session_id": request.session_id or f"line_{request.chat_id}",
        "client_request_id": f"line:{request.token}"[:120],
        "persist_conversation": True,
        "remember_context": True,
        "response_mode": "text_text",
        "display_surface": "panel",
        "interaction_context": {"kind": "messaging_gateway", "platform": "telegram", "chat_id": request.chat_id},
    }


def answer_text(response: dict[str, Any]) -> str:
    return str(
        response.get("answer")
        or response.get("spoken_answer")
        or (response.get("chat") or {}).get("answer")
        or ""
    )


class ChotuBrain:
    name = "chotu"

    def __init__(self, config: LineConfig) -> None:
        self.config = config

    def available(self) -> bool:
        try:
            return httpx.get(f"{self.config.chotu_hub_url}/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    async def run(self, request: TurnRequest) -> TurnResult:
        payload = turn_payload(request)
        try:
            async with httpx.AsyncClient(timeout=TURN_TIMEOUT_SECONDS) as client:
                response = await client.post(f"{self.config.chotu_hub_url}/v1/assistant/turn", json=payload)
        except httpx.HTTPError as exc:
            return TurnResult("", None, error=f"Chotu is not reachable ({type(exc).__name__}); is the app open?")
        if response.status_code != 200:
            return TurnResult("", None, error=f"Chotu answered {response.status_code}: {response.text[:300]}")
        return TurnResult(answer_text(response.json()), payload["session_id"])
