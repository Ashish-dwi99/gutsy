"""What each turn's prompt carries besides the owner's words.

The brain's session already holds the conversation; this adds what changes
between messages: the time, the owner's saved details, and open workstreams.
"""

from __future__ import annotations

from datetime import datetime
from importlib import resources
from zoneinfo import ZoneInfo

from .store import WORKSTREAMS_RECALLED, GutsyStore

MEMORIES_RECALLED = 30
GUTSY_NOTE = (
    "The owner is texting you on Telegram from their phone. Reply in short plain text, no Markdown. "
    "Use the gutsy tools for approvals, questions, schedules, workstreams, and the vault."
)


def instructions(name: str) -> str:
    return resources.files("gutsy").joinpath("instructions", f"{name}.md").read_text(encoding="utf-8")


def context_block(store: GutsyStore, timezone: str, now: datetime | None = None) -> str:
    zone = ZoneInfo(timezone or "UTC")
    moment = (now or datetime.now(zone)).astimezone(zone)
    lines = [f"now: {moment.strftime('%A %Y-%m-%d %H:%M')} ({zone.key})"]
    info = store.personal_info()
    if info:
        lines.append("owner info: " + "; ".join(f"{key}={value}" for key, value in info.items()))
    memories = store.memories()[:MEMORIES_RECALLED]
    if memories:
        lines.append("owner memories:")
        lines += [f"• [{item['id']}] {item['text']}" for item in memories]
    workstreams = store.workstreams()[:WORKSTREAMS_RECALLED]
    if workstreams:
        lines.append("open workstreams:")
        for item in workstreams:
            next_step = f" — next: {item['next_step']}" if item.get("next_step") else ""
            lines.append(f"• [{item['id']} rev {item['revision']}] {item['title']} ({item['status']}){next_step}")
    return "<line_context>\n" + "\n".join(lines) + "\n</line_context>"


def interactive_prompt(store: GutsyStore, timezone: str, text: str, *, brain: str) -> str:
    parts = [context_block(store, timezone)]
    if brain == "chotu":
        parts.append(GUTSY_NOTE)
    parts.append(text)
    return "\n\n".join(parts)


def scheduled_prompt(store: GutsyStore, timezone: str, title: str, task: str, *, brain: str) -> str:
    parts = [context_block(store, timezone), instructions("scheduled")]
    if brain == "chotu":
        parts.append(GUTSY_NOTE)
    parts.append(f"Scheduled task: {title}\n{task}")
    return "\n\n".join(parts)
