"""`chotu-line doctor`: every setup check, each with the fix in plain words."""

from __future__ import annotations

import asyncio
import shutil
import sys
from dataclasses import dataclass

import keyring

from . import browser, service
from .brains.chotu import ChotuBrain
from .config import LineConfig, config_path
from .telegram import TelegramBot, TelegramError

BRAIN_INSTALL = {
    "claude": "install Claude Code (https://claude.com/claude-code) and run `claude` once to sign in",
    "codex": "install Codex (`npm i -g @openai/codex`) and run `codex login`",
    "chotu": "open the Chotu app and sign in",
}


@dataclass(frozen=True)
class Check:
    ok: bool
    label: str
    fix: str = ""
    required: bool = True


def brain_available(name: str, config: LineConfig) -> bool:
    if name == "chotu":
        return ChotuBrain(config).available()
    return shutil.which(name) is not None


def checks(config: LineConfig) -> list[Check]:
    results = [
        Check(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "install Python 3.11 or newer"),
        Check(config_path().exists(), "setup has run", "run `chotu-line setup`"),
        Check(
            brain_available(config.brain, config),
            f"brain: {config.brain}",
            BRAIN_INSTALL[config.brain] + ", or switch with `chotu-line brain <name>`",
        ),
    ]
    results.append(_telegram_check(config))
    results.append(Check(config.paired, "paired with your Telegram", "run `chotu-line run` and open the pairing link it prints"))
    results.append(
        Check(
            not config.browser or browser.find_chrome() is not None,
            "Chrome (or Chromium, Brave, Edge) installed",
            "install Google Chrome, or turn the browser off in config.json",
        )
    )
    results.append(
        Check(
            not config.browser or config.brain == "chotu" or shutil.which("npx") is not None,
            "Node.js for browser control",
            "install Node.js 18+ (https://nodejs.org); without it the brain can't use the browser",
        )
    )
    results.append(_keychain_check())
    results.append(Check(service.is_installed(), "background service installed", "run `chotu-line service install`", required=False))
    results.append(Check(service.is_running(), "background service running", "run `chotu-line service install`, then check service.log", required=False))
    return results


def _telegram_check(config: LineConfig) -> Check:
    if not config.telegram_token:
        return Check(False, "Telegram bot token", "run `chotu-line setup` and paste the token from @BotFather")

    async def username() -> str:
        bot = TelegramBot(config.telegram_token)
        try:
            return str((await bot.me())["username"])
        finally:
            await bot.close()

    try:
        return Check(True, f"Telegram bot @{asyncio.run(username())}")
    except TelegramError as exc:
        return Check(False, "Telegram bot token", f"Telegram rejected it ({exc}); run `chotu-line setup` again")


def _keychain_check() -> Check:
    backend = keyring.get_keyring()
    usable = backend.priority > 0
    return Check(usable, f"secure storage ({type(backend).__name__})", "install a system keyring (macOS Keychain is built in)")


def render(results: list[Check]) -> tuple[str, bool]:
    lines = []
    for check in results:
        mark = "✓" if check.ok else ("✗" if check.required else "!")
        lines.append(f"{mark} {check.label}" + ("" if check.ok else f"\n    fix: {check.fix}"))
    healthy = all(check.ok for check in results if check.required)
    return "\n".join(lines), healthy
