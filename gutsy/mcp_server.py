"""`python -m gutsy.mcp_server`: Gutsy's tools for whichever brain runs.

The daemon passes GUTSY_TOKEN (the running turn, or Chotu's standing
token) and, when it differs from the default, GUTSY_HOME.
"""

from __future__ import annotations

import os
import sys

from .config import GutsyConfig
from .mcp_stdio import serve
from .store import GutsyStore
from .tools import GutsyTools

SERVER_NAME = "gutsy"
SERVER_VERSION = "0.2.0"


def main() -> None:
    token = os.getenv("GUTSY_TOKEN", "")
    if not token:
        sys.exit("GUTSY_TOKEN is not set; the gutsy daemon starts this server")
    config = GutsyConfig.load()
    tools = GutsyTools(config, GutsyStore(config.db_path), token)
    serve(SERVER_NAME, SERVER_VERSION, tools.tools())


if __name__ == "__main__":
    main()
