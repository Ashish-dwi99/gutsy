"""`python -m chotu_line.mcp_server`: the line's tools for whichever brain runs.

The daemon passes CHOTU_LINE_TOKEN (the running turn, or Chotu's standing
token) and, when it differs from the default, CHOTU_LINE_HOME.
"""

from __future__ import annotations

import os
import sys

from .config import LineConfig
from .mcp_stdio import serve
from .store import LineStore
from .tools import LineTools

SERVER_NAME = "chotu_line"
SERVER_VERSION = "0.1.0"


def main() -> None:
    token = os.getenv("CHOTU_LINE_TOKEN", "")
    if not token:
        sys.exit("CHOTU_LINE_TOKEN is not set; the chotu-line daemon starts this server")
    config = LineConfig.load()
    tools = LineTools(config, LineStore(config.db_path), token)
    serve(SERVER_NAME, SERVER_VERSION, tools.tools())


if __name__ == "__main__":
    main()
