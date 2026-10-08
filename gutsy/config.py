from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# In order of preference when several are available: the owner's own
# subscriptions first, because they cost nothing extra.
BRAINS = ("claude", "codex", "chotu")
DEFAULT_BRAIN = "claude"
CHROME_DEBUG_PORT = 9333
CHOTU_HUB_URL = "http://127.0.0.1:7777"
CHOTU_HUB_DATA_DIR = Path.home() / "Library" / "Application Support" / "Chotu" / "hub"


def gutsy_home() -> Path:
    return Path(os.getenv("GUTSY_HOME", Path.home() / ".gutsy")).expanduser()


@dataclass
class GutsyConfig:
    """The owner's settings, one JSON file readable only by the owner.

    The Telegram token is the only credential here; vault secrets live in the
    OS keychain, never in this file.
    """

    brain: str = DEFAULT_BRAIN
    telegram_token: str = ""
    owner_user_id: str = ""
    owner_chat_id: str = ""
    pairing_code: str = ""
    timezone: str = ""
    browser: bool = True
    chrome_port: int = CHROME_DEBUG_PORT
    claude_model: str = ""
    codex_model: str = ""
    chotu_hub_url: str = CHOTU_HUB_URL
    chotu_token: str = ""

    @property
    def home(self) -> Path:
        return gutsy_home()

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def db_path(self) -> Path:
        return self.home / "gutsy.db"

    @property
    def socket_path(self) -> Path:
        return self.home / "control.sock"

    @property
    def chrome_profile(self) -> Path:
        return self.home / "chrome"

    @property
    def paired(self) -> bool:
        return bool(self.owner_user_id and self.owner_chat_id)

    def validate(self) -> None:
        if self.brain not in BRAINS:
            raise ValueError(f"brain must be one of {', '.join(BRAINS)}, not {self.brain!r}")

    @classmethod
    def load(cls) -> "GutsyConfig":
        path = config_path()
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        config = cls(**{key: value for key, value in raw.items() if key in known})
        config.validate()
        return config

    def save(self) -> None:
        self.validate()
        ensure_home()
        path = config_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)


def config_path() -> Path:
    return gutsy_home() / "config.json"


def ensure_home() -> Path:
    home = gutsy_home()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    (home / "workspace").mkdir(exist_ok=True)
    return home
