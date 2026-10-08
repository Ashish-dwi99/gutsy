"""Which of the brain's own tool calls need the owner's tap.

Claude Code hands every permission prompt to Gutsy's `approve` tool. Most
calls are safe to allow without bothering the owner on their phone: reading,
searching, Gutsy's own tools, browsing, and writing inside Gutsy's
workspace. Three kinds of call are gated in code, not by instructions:

- browser tools that run arbitrary page code, upload local files, or read raw
  network traffic (they could read a filled password or leak a file);
- interaction while a payment page is open, unless the owner approved a
  purchase through `request_approval` within the grant window;
- anything else not known to be safe.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "WebSearch",
        "WebFetch",
        "TodoWrite",
        "Task",
        "ToolSearch",
        "NotebookRead",
    }
)
GUTSY_PREFIX = "mcp__gutsy__"
BROWSER_PREFIX = "mcp__playwright__"
# Run page code, push local files to a site, or show raw requests (a submitted password lives there).
BROWSER_ALWAYS_ASK = frozenset(
    BROWSER_PREFIX + name for name in ("browser_evaluate", "browser_run_code_unsafe", "browser_file_upload", "browser_network_request")
)
# Change page state; gated while a payment page is open.
BROWSER_INTERACTIONS = frozenset(
    BROWSER_PREFIX + name
    for name in (
        "browser_click",
        "browser_type",
        "browser_fill_form",
        "browser_press_key",
        "browser_select_option",
        "browser_drag",
        "browser_drop",
        "browser_handle_dialog",
        "browser_hover",
    )
)
WORKSPACE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
SAFE_COMMANDS = frozenset(
    {"ls", "pwd", "date", "cal", "echo", "cat", "head", "tail", "wc", "which", "whoami", "uname", "grep", "rg", "sort", "uniq", "cut", "tr"}
)
SHELL_CONTROL = (";", "|", "&", ">", "<", "`", "$(", "\n")
NEVER_ALWAYS = frozenset({"Bash"}) | BROWSER_ALWAYS_ASK | BROWSER_INTERACTIONS


@dataclass(frozen=True)
class Verdict:
    decision: Literal["allow", "ask"]
    can_always: bool = False
    reason: str = ""


def judge(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    workspace: Path,
    trusted: bool,
    payment_page: Callable[[], str | None] = lambda: None,
    purchase_approved: Callable[[], bool] = lambda: False,
) -> Verdict:
    """`payment_page` and `purchase_approved` are only called for browser interactions."""
    if tool_name in READ_ONLY_TOOLS or tool_name.startswith(GUTSY_PREFIX):
        return Verdict("allow")
    if tool_name in BROWSER_ALWAYS_ASK:
        return Verdict("ask", reason="this browser tool can read page secrets or send local files")
    if tool_name in BROWSER_INTERACTIONS:
        url = payment_page()
        if url and not purchase_approved():
            return Verdict("ask", reason=f"a payment page is open ({url}) and no purchase is approved")
        return Verdict("allow")
    if tool_name.startswith(BROWSER_PREFIX):
        return Verdict("allow")
    if tool_name in WORKSPACE_WRITE_TOOLS and _inside(tool_input, workspace):
        return Verdict("allow")
    if tool_name == "Bash" and is_safe_command(str(tool_input.get("command") or "")):
        return Verdict("allow")
    if trusted and tool_name not in NEVER_ALWAYS:
        return Verdict("allow")
    return Verdict("ask", can_always=tool_name not in NEVER_ALWAYS)


def is_safe_command(command: str) -> bool:
    """One read-only program, no pipes, redirects, chaining, or substitution."""
    if not command.strip() or any(token in command for token in SHELL_CONTROL):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return bool(words) and words[0] in SAFE_COMMANDS


def summarize(tool_name: str, tool_input: dict[str, Any]) -> str:
    """What the owner reads on the approval card."""
    if tool_name == "Bash":
        detail = str(tool_input.get("command") or "")
        note = str(tool_input.get("description") or "")
        return f"{detail}\n({note})" if note else detail
    for key in ("file_path", "notebook_path", "url", "path", "element", "function"):
        if tool_input.get(key):
            return str(tool_input[key])[:600]
    return ", ".join(f"{key}={value}" for key, value in list(tool_input.items())[:4])[:600]


def _inside(tool_input: dict[str, Any], workspace: Path) -> bool:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return False
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve().is_relative_to(workspace.resolve())
