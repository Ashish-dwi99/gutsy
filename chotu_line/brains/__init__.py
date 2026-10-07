from __future__ import annotations

from ..config import LineConfig
from .base import Brain, TurnRequest, TurnResult
from .chotu import ChotuBrain
from .claude import ClaudeBrain
from .codex import CodexBrain


def make_brain(name: str, config: LineConfig, instructions: str) -> Brain:
    if name == "claude":
        return ClaudeBrain(config, instructions)
    if name == "codex":
        return CodexBrain(config)
    if name == "chotu":
        return ChotuBrain(config)
    raise ValueError(f"unknown brain {name!r}")


__all__ = ["Brain", "TurnRequest", "TurnResult", "make_brain"]
