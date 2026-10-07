"""Run the line as a login service so it survives closed terminals and reboots.

macOS uses a launchd agent and Linux a systemd user unit. Both restart the
line if it exits. Services start with a minimal PATH, so install captures the
directories of the CLIs the brains need (claude, codex, node/npx) as they are
found right now.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import LineConfig

LABEL = "ai.chotu.line"
SYSTEMD_UNIT = "chotu-line.service"
TOOLS_ON_PATH = ("claude", "codex", "node", "npx", "chotu-line")
BASE_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


class ServiceError(RuntimeError):
    pass


def service_path() -> str:
    found = [str(Path(path).parent) for tool in TOOLS_ON_PATH if (path := shutil.which(tool))]
    ordered: list[str] = []
    for directory in [*found, *BASE_PATH]:
        if directory not in ordered:
            ordered.append(directory)
    return ":".join(ordered)


def command() -> list[str]:
    return [sys.executable, "-m", "chotu_line.cli", "run"]


def launchd_plist(config: LineConfig) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": command(),
        "EnvironmentVariables": {"PATH": service_path(), "CHOTU_LINE_HOME": str(config.home), "HOME": str(Path.home())},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(config.home / "service.log"),
        "StandardErrorPath": str(config.home / "service.log"),
        "WorkingDirectory": str(config.home),
    }


def systemd_unit(config: LineConfig) -> str:
    argv = " ".join(command())
    return (
        "[Unit]\nDescription=Chotu Line\nAfter=network-online.target\n\n"
        f"[Service]\nExecStart={argv}\nRestart=always\nRestartSec=30\n"
        f"Environment=PATH={service_path()}\nEnvironment=CHOTU_LINE_HOME={config.home}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def _launchd_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ServiceError(f"{' '.join(argv)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def install(config: LineConfig) -> Path:
    if sys.platform == "darwin":
        path = _launchd_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
        path.write_bytes(plistlib.dumps(launchd_plist(config)))
        _run(["launchctl", "bootstrap", domain, str(path)])
        return path
    if sys.platform.startswith("linux"):
        path = _systemd_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(config), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        return path
    raise ServiceError("background service install supports macOS and Linux; run `chotu-line run` instead")


def uninstall() -> None:
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
        _launchd_file().unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], check=False)
        _systemd_file().unlink(missing_ok=True)


def is_running() -> bool:
    if sys.platform == "darwin":
        result = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False)
        return result.returncode == 0 and "state = running" in result.stdout
    if sys.platform.startswith("linux"):
        return _run(["systemctl", "--user", "is-active", SYSTEMD_UNIT], check=False).stdout.strip() == "active"
    return False


def is_installed() -> bool:
    return _launchd_file().exists() if sys.platform == "darwin" else _systemd_file().exists()
