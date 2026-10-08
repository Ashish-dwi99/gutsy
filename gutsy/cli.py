"""`gutsy`: set up, run, and manage Gutsy."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from . import browser, context, doctor, service
from .brains.base import gutsy_mcp_server
from .brains.chotu import ChotuBrain
from .brains.claude import ClaudeBrain
from .brains.codex import CodexBrain
from .config import BRAINS, CHOTU_HUB_DATA_DIR, DEFAULT_BRAIN, GutsyConfig, ensure_home
from .control import ASK_TIMEOUT_SECONDS
from .daemon import COMMANDS, PRIVACY, Gutsy, poll_telegram, serve_control, tick_schedules
from .store import GutsyStore
from .telegram import TelegramBot, TelegramError
from .vault import delete_secret, normalize_origin, put_secret

CHOTU_MCP_EXCLUDED_TOOLS = ["approve"]


def system_timezone() -> str:
    link = Path("/etc/localtime")
    if link.is_symlink():
        target = os.readlink(link)
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    return "UTC"


def detect_brains(config: GutsyConfig) -> dict[str, bool]:
    return {
        "chotu": ChotuBrain(config).available(),
        "claude": ClaudeBrain.available(),
        "codex": CodexBrain.available(),
    }


def register_with_chotu(config: GutsyConfig, hub_data_dir: Path) -> Path:
    """Mount Gutsy's tools in Chotu, carrying the standing token."""
    path = hub_data_dir / "mcp_servers.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    servers = existing.setdefault("mcpServers", {})
    servers["gutsy"] = {
        **gutsy_mcp_server(config, config.chotu_token),
        "timeout": ASK_TIMEOUT_SECONDS + 60,
        "risk": "low",
        "tools": {"exclude": CHOTU_MCP_EXCLUDED_TOOLS},
    }
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_workspace_instructions(config: GutsyConfig) -> None:
    """Codex reads AGENTS.md from its working root; Claude gets the same text as a system prompt."""
    (config.workspace / "AGENTS.md").write_text(context.instructions("assistant"), encoding="utf-8")


def cmd_setup(args: argparse.Namespace) -> None:
    config = GutsyConfig.load()
    available = detect_brains(config)
    print("Brains on this machine:")
    for name in BRAINS:
        print(f"  {name:6} {'ready' if available[name] else 'not found'}")
    default = config.brain if available.get(config.brain) else next((n for n in BRAINS if available[n]), DEFAULT_BRAIN)
    choice = input(f"Which brain? [{default}] ").strip() or default
    if choice not in BRAINS:
        sys.exit(f"pick one of {', '.join(BRAINS)}")
    config.brain = choice

    if not config.telegram_token or input("Replace the Telegram bot token? [y/N] ").lower().startswith("y"):
        print("\nIn Telegram, open @BotFather, send /newbot, and paste the token it gives you.")
        config.telegram_token = getpass.getpass("Bot token: ").strip()
    bot_name = asyncio.run(_bot_username(config.telegram_token))

    config.timezone = config.timezone or system_timezone()
    config.chotu_token = config.chotu_token or secrets.token_urlsafe(24)
    if not config.paired:
        config.pairing_code = secrets.token_hex(4)
    config.save()
    write_workspace_instructions(config)
    GutsyStore(config.db_path)

    if CHOTU_HUB_DATA_DIR.exists():
        print(f"\nChotu found; its tools are registered in {register_with_chotu(config, CHOTU_HUB_DATA_DIR)}")
        print("(restart Chotu once so it picks them up)")

    print(f"\nTimezone: {config.timezone}")
    print(f"\n{PRIVACY}")
    if config.paired:
        print("\nAlready paired with your Telegram account.")
    else:
        print("\nNext:")
        print("  gutsy service install   # keeps Gutsy running in the background")
        print(f"  then open this on your phone to pair: https://t.me/{bot_name}?start={config.pairing_code}")
    print("\nCheck everything any time with `gutsy doctor`.")


async def _bot_username(token: str) -> str:
    bot = TelegramBot(token)
    try:
        return str((await bot.me())["username"])
    except TelegramError as exc:
        sys.exit(f"Telegram rejected that token: {exc}")
    finally:
        await bot.close()


def cmd_run(args: argparse.Namespace) -> None:
    config = GutsyConfig.load()
    if not config.telegram_token:
        sys.exit("run `gutsy setup` first")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(config.home / "gutsy.log")],
    )
    # httpx logs every request URL at INFO, and Telegram's URLs carry the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    write_workspace_instructions(config)
    if config.browser:
        browser.launch(config.chrome_port, config.chrome_profile)
    asyncio.run(_serve(config))


async def _serve(config: GutsyConfig) -> None:
    store = GutsyStore(config.db_path)
    bot = TelegramBot(config.telegram_token)
    gutsy = Gutsy(config, store, bot)
    server = await serve_control(gutsy, config.socket_path)
    await bot.set_commands(COMMANDS)
    logging.getLogger("gutsy").info("gutsy is up with brain %s", config.brain)
    if not config.paired:
        print(f"waiting to pair: https://t.me/{(await bot.me())['username']}?start={config.pairing_code}")
    try:
        async with server:
            await asyncio.gather(poll_telegram(gutsy, bot, store), tick_schedules(gutsy))
    finally:
        await bot.close()


def cmd_status(args: argparse.Namespace) -> None:
    config = GutsyConfig.load()
    store = GutsyStore(config.db_path)
    available = detect_brains(config)
    print(f"brain:     {config.brain} ({'ready' if available[config.brain] else 'NOT available'})")
    print(f"telegram:  {'paired' if config.paired else 'not paired'}")
    if not config.paired and config.telegram_token and config.pairing_code:
        print(f"           pair by opening https://t.me/{asyncio.run(_bot_username(config.telegram_token))}?start={config.pairing_code}")
    print(f"timezone:  {config.timezone or 'unset'}")
    print(f"browser:   {'running' if browser.is_running(config.chrome_port) else 'stopped'} (port {config.chrome_port})")
    print(f"vault:     {len(store.vault_items())} items")
    print(f"schedules: {len([s for s in store.schedules() if s['status'] == 'active'])} active")


def cmd_brain(args: argparse.Namespace) -> None:
    config = GutsyConfig.load()
    config.brain = args.name
    config.save()
    print(f"brain set to {args.name}")


def cmd_browser(args: argparse.Namespace) -> None:
    config = GutsyConfig.load()
    browser.launch(config.chrome_port, config.chrome_profile)
    print("Gutsy's Chrome is open. Sign in to the sites you want it to use; the sessions persist.")


def cmd_vault_add(args: argparse.Namespace) -> None:
    config = GutsyConfig.load()
    store = GutsyStore(config.db_path)
    origins = [normalize_origin(origin) for origin in args.origin]
    username = input("Username / email / phone: ").strip()
    secret = {"username": username, "password": getpass.getpass("Password: ")}
    item = store.add_vault_item(kind="login", label=args.label, origins=origins, hint=_mask(username))
    put_secret(item["handle"], secret)
    print(f"saved {item['handle']} ({args.label}) for {', '.join(origins)}")


def _mask(value: str) -> str:
    if "@" in value:
        name, _, domain = value.partition("@")
        return f"{name[:2]}…@{domain}"
    return f"{value[:2]}…{value[-2:]}" if len(value) > 4 else "…"


def cmd_vault_list(args: argparse.Namespace) -> None:
    for item in GutsyStore(GutsyConfig.load().db_path).vault_items():
        print(f"{item['handle']}  {item['kind']:5}  {item['label']}  {item['hint']}  {', '.join(item['origins'])}")


def cmd_vault_rm(args: argparse.Namespace) -> None:
    store = GutsyStore(GutsyConfig.load().db_path)
    if not store.remove_vault_item(args.handle):
        sys.exit(f"no vault item {args.handle}")
    delete_secret(args.handle)
    print(f"removed {args.handle}")


def cmd_doctor(args: argparse.Namespace) -> None:
    text, healthy = doctor.render(doctor.checks(GutsyConfig.load()))
    print(text)
    if not healthy:
        sys.exit(1)


def cmd_service(args: argparse.Namespace) -> None:
    config = GutsyConfig.load()
    if args.action == "install":
        if not config.telegram_token:
            sys.exit("run `gutsy setup` first")
        print(f"installed {service.install(config)}; logs in {config.home / 'service.log'}")
    elif args.action == "uninstall":
        service.uninstall()
        print("background service removed")
    else:
        print(f"installed: {service.is_installed()}  running: {service.is_running()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gutsy", description="Text your own agent from your phone.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="choose a brain and connect Telegram").set_defaults(run=cmd_setup)
    sub.add_parser("run", help="run Gutsy").set_defaults(run=cmd_run)
    sub.add_parser("status", help="show Gutsy's state").set_defaults(run=cmd_status)
    brain = sub.add_parser("brain", help="switch the brain")
    brain.add_argument("name", choices=BRAINS)
    brain.set_defaults(run=cmd_brain)
    sub.add_parser("browser", help="open Gutsy's Chrome to sign in to sites").set_defaults(run=cmd_browser)
    sub.add_parser("doctor", help="check the setup and say how to fix problems").set_defaults(run=cmd_doctor)
    svc = sub.add_parser("service", help="run Gutsy in the background, starting at login")
    svc.add_argument("action", choices=["install", "uninstall", "status"])
    svc.set_defaults(run=cmd_service)

    vault = sub.add_parser("vault", help="saved logins").add_subparsers(dest="vault_command", required=True)
    add = vault.add_parser("add", help="save a login in the keychain")
    add.add_argument("--label", required=True)
    add.add_argument("--origin", action="append", required=True, help="https origin it may be filled on; repeatable")
    add.set_defaults(run=cmd_vault_add)
    vault.add_parser("list").set_defaults(run=cmd_vault_list)
    remove = vault.add_parser("rm")
    remove.add_argument("handle")
    remove.set_defaults(run=cmd_vault_rm)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    ensure_home()
    args.run(args)


if __name__ == "__main__":
    main()
