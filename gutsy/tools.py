"""The personal-assistant toolset every brain gets over MCP."""

from __future__ import annotations

import json
import time
from functools import wraps
from typing import Any, Callable

from . import browser, permissions, schedule
from .config import GutsyConfig
from .control import APPROVAL_TIMEOUT_SECONDS, ASK_TIMEOUT_SECONDS, ControlError, call
from .mcp_stdio import Tool, ToolFailure
from .store import PERSONAL_FIELDS, WORKSTREAM_STATUSES, GutsyStore, StoreError
from .vault import VaultError, normalize_origin

_DOMAIN_ERRORS = (StoreError, schedule.RuleError, VaultError, ControlError)
WORKSTREAM_FIELDS = ("objective", "constraints", "decisions", "progress", "next_step", "notes", "sources")


def _tool(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()):
    def register(handler: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(handler)
        def run(self: "GutsyTools", arguments: dict[str, Any]) -> Any:
            try:
                return handler(self, arguments)
            except _DOMAIN_ERRORS as exc:
                raise ToolFailure(str(exc)) from exc

        run.tool_spec = (name, description, {"type": "object", "properties": properties, "required": list(required)})  # type: ignore[attr-defined]
        return run

    return register


_STRING = {"type": "string"}


class GutsyTools:
    """Tool handlers bound to one owner's store, config, and turn token."""

    def __init__(self, config: GutsyConfig, store: GutsyStore, token: str) -> None:
        self.config = config
        self.store = store
        self.token = token

    def tools(self) -> list[Tool]:
        found: list[Tool] = []
        for attribute in dir(type(self)):
            method = getattr(self, attribute)
            spec = getattr(method, "tool_spec", None)
            if spec:
                name, description, schema = spec
                found.append(Tool(name, description, schema, method))
        return found

    def _control(self, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        return call(self.config.socket_path, {**request, "token": self.token}, timeout=timeout)

    # talking to the owner -------------------------------------------------
    @_tool(
        "send_message",
        "Send the owner a short interim text message now, such as an acknowledgement before long work or a "
        "result followed by a separate question. Plain text, no Markdown. Your final reply is delivered "
        "automatically; do not repeat it here.",
        {"text": _STRING},
        ("text",),
    )
    def send_message(self, arguments: dict[str, Any]) -> str:
        self._control({"op": "send", "text": str(arguments["text"])}, timeout=60)
        return "sent"

    @_tool(
        "ask_user",
        "Ask the owner a question and wait for their reply (up to 30 minutes). Use it for a one-time code, "
        "a choice only they can make, or missing information you cannot find yourself. Never ask for a "
        "password or card number; use the vault.",
        {"question": _STRING, "choices": {"type": "array", "items": _STRING, "maxItems": 6}},
        ("question",),
    )
    def ask_user(self, arguments: dict[str, Any]) -> str:
        reply = self._control(
            {"op": "ask", "question": str(arguments["question"]), "choices": list(arguments.get("choices") or [])},
            timeout=ASK_TIMEOUT_SECONDS + 30,
        )
        return str(reply["answer"])

    @_tool(
        "request_approval",
        "Get the owner's explicit approval before a consequential action: a purchase or payment, a message "
        "or email to anyone else, a booking or cancellation, or anything destructive. State the exact "
        "merchant, item, amount, recipient, or change. Returns approved or declined. Do not ask again for "
        "an action already approved unless the total or a material term changed.",
        {"action": _STRING, "details": _STRING},
        ("action", "details"),
    )
    def request_approval(self, arguments: dict[str, Any]) -> str:
        action, details = str(arguments["action"]), str(arguments["details"])
        reply = self._control(
            {"op": "approve", "title": action, "detail": details, "can_always": False},
            timeout=APPROVAL_TIMEOUT_SECONDS + 30,
        )
        if reply["decision"] not in ("allow", "always"):
            return "declined"
        self.store.add_grant(action, details, time.time())
        return "approved; payment pages are open to you for the next 20 minutes for this exact action"

    @_tool(
        "approve",
        "Internal: the brain's own tool-permission prompt. Do not call this yourself.",
        {"tool_name": _STRING, "input": {"type": "object"}, "tool_use_id": _STRING},
        ("tool_name", "input"),
    )
    def approve(self, arguments: dict[str, Any]) -> str:
        tool_name = str(arguments["tool_name"])
        tool_input = dict(arguments.get("input") or {})
        verdict = permissions.judge(
            tool_name,
            tool_input,
            workspace=self.config.workspace,
            trusted=self.store.is_trusted_tool(tool_name),
            payment_page=lambda: browser.payment_page_url(self.config.chrome_port) if self.config.browser else None,
            purchase_approved=lambda: self.store.has_active_grant(time.time()),
        )
        decision = "allow"
        if verdict.decision == "ask":
            reason = f"\nwhy I'm asking: {verdict.reason}" if verdict.reason else ""
            reply = self._control(
                {
                    "op": "approve",
                    "title": f"Allow {tool_name.removeprefix('mcp__playwright__')}?",
                    "detail": permissions.summarize(tool_name, tool_input) + reason,
                    "can_always": verdict.can_always,
                },
                timeout=APPROVAL_TIMEOUT_SECONDS + 30,
            )
            decision = reply["decision"]
            if decision == "always":
                self.store.trust_tool(tool_name)
        if decision == "deny":
            return json.dumps({"behavior": "deny", "message": "The owner declined this from their phone."})
        return json.dumps({"behavior": "allow", "updatedInput": tool_input})

    # what Gutsy knows about the owner ----------------------------------
    @_tool(
        "recall",
        "Read everything saved about the owner: personal info, memories, and open workstreams.",
        {},
    )
    def recall(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "personal_info": self.store.personal_info(),
            "memories": self.store.memories(),
            "workstreams": self.store.workstreams(),
        }

    @_tool(
        "personal_info_update",
        "Save or correct the owner's own reusable form details when they state them. Pass null to forget a "
        "field. Never store credentials, card details, or one-time codes here.",
        {"fields": {"type": "object", "properties": {name: {"type": ["string", "null"]} for name in PERSONAL_FIELDS}}},
        ("fields",),
    )
    def personal_info_update(self, arguments: dict[str, Any]) -> dict[str, str]:
        return self.store.update_personal_info(dict(arguments["fields"]))

    @_tool(
        "remember",
        "Save one stable fact or preference the owner states about themselves (a favourite cinema, a diet, "
        "a seat preference). Not task details, not third-party claims, never secrets.",
        {"text": _STRING},
        ("text",),
    )
    def remember(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.store.remember(str(arguments["text"]))

    @_tool("forget_memory", "Forget one saved memory by id.", {"id": _STRING}, ("id",))
    def forget_memory(self, arguments: dict[str, Any]) -> str:
        return "forgotten" if self.store.forget_memory(str(arguments["id"])) else "no such memory"

    @_tool(
        "workstream_save",
        "Create or update an ongoing undertaking that spans conversations (a trip, a move, a renewal). Save "
        "the objective, constraints, decisions including rejected options, verified progress, the next "
        "unresolved step, and sources. To update, pass id and the revision you last read.",
        {
            "id": _STRING,
            "revision": {"type": "integer"},
            "title": _STRING,
            "status": {"type": "string", "enum": list(WORKSTREAM_STATUSES)},
            **{name: _STRING for name in WORKSTREAM_FIELDS},
        },
        ("title", "status"),
    )
    def workstream_save(self, arguments: dict[str, Any]) -> dict[str, Any]:
        body = {name: str(arguments[name]) for name in WORKSTREAM_FIELDS if arguments.get(name)}
        return self.store.save_workstream(
            title=str(arguments["title"]),
            status=str(arguments["status"]),
            body=body,
            workstream_id=arguments.get("id"),
            revision=arguments.get("revision"),
        )

    @_tool(
        "workstream_list",
        "List workstreams, open ones by default.",
        {"include_closed": {"type": "boolean"}},
    )
    def workstream_list(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.workstreams(include_closed=bool(arguments.get("include_closed")))

    @_tool("workstream_forget", "Forget a workstream when the owner asks.", {"id": _STRING}, ("id",))
    def workstream_forget(self, arguments: dict[str, Any]) -> str:
        return "forgotten" if self.store.forget_workstream(str(arguments["id"])) else "no such workstream"

    # schedules -------------------------------------------------------------
    @_tool(
        "schedule_create",
        "Create a reminder, recurring job, or monitor the owner asked for. kind once needs `at` (local "
        "ISO date-time); interval needs every_minutes (at least 5); daily needs time HH:MM and optional "
        "weekdays (0=Monday). `prompt` is the full task to run then; the result is texted to the owner.",
        {
            "title": _STRING,
            "prompt": _STRING,
            "kind": {"type": "string", "enum": ["once", "interval", "daily"]},
            "at": _STRING,
            "every_minutes": {"type": "integer"},
            "time": _STRING,
            "weekdays": {"type": "array", "items": {"type": "integer"}},
        },
        ("title", "prompt", "kind"),
    )
    def schedule_create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rule = schedule.build_rule(
            kind=str(arguments["kind"]),
            timezone=self.config.timezone or "UTC",
            at=arguments.get("at"),
            every_minutes=arguments.get("every_minutes"),
            time=arguments.get("time"),
            weekdays=arguments.get("weekdays"),
        )
        created = self.store.add_schedule(
            chat_id=self.config.owner_chat_id,
            title=str(arguments["title"]),
            prompt=str(arguments["prompt"]),
            rule=rule,
            next_run_at=schedule.first_run(rule, time.time()),
        )
        return {**created, "when": schedule.describe(rule)}

    @_tool("schedule_list", "List the owner's schedules.", {})
    def schedule_list(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        return [{**item, "when": schedule.describe(item["rule"])} for item in self.store.schedules()]

    @_tool(
        "schedule_update",
        "Pause, resume, or delete a schedule.",
        {"id": _STRING, "action": {"type": "string", "enum": ["pause", "resume", "delete"]}},
        ("id", "action"),
    )
    def schedule_update(self, arguments: dict[str, Any]) -> Any:
        schedule_id, action = str(arguments["id"]), str(arguments["action"])
        if action == "delete":
            return "deleted" if self.store.delete_schedule(schedule_id) else "no such schedule"
        if action == "pause":
            return self.store.update_schedule(schedule_id, status="paused")
        item = self.store.schedule(schedule_id)
        return self.store.update_schedule(
            schedule_id, status="active", next_run_at=schedule.first_run(item["rule"], time.time())
        )

    # vault -------------------------------------------------------------------
    @_tool(
        "vault_list",
        "List saved logins: handle, label, allowed sites, and a masked username. Passwords are never shown.",
        {},
    )
    def vault_list(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.vault_items()

    @_tool(
        "vault_fill",
        "Fill a saved login into the open browser tab on that login's site. The password goes from the "
        "keychain straight into the page; you only learn which fields were filled. Submit the form yourself "
        "afterwards. Card details are never stored: pay with the merchant's saved method after request_approval.",
        {"handle": _STRING},
        ("handle",),
    )
    def vault_fill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = self.store.vault_item(str(arguments["handle"]))
        if not browser.is_running(self.config.chrome_port):
            raise ToolFailure("Gutsy's Chrome is not running; start it with `gutsy browser`")
        return browser.fill(self.config.chrome_port, item)

    @_tool(
        "vault_request_setup",
        "When a sign-in needs a login that is not saved, send the owner the one command to save it on their "
        "Mac. Never ask for a password in chat.",
        {"label": _STRING, "origin": _STRING},
        ("label", "origin"),
    )
    def vault_request_setup(self, arguments: dict[str, Any]) -> str:
        origin = normalize_origin(str(arguments["origin"]))
        label = str(arguments["label"]).replace('"', "")
        command = f'gutsy vault add --label "{label}" --origin {origin}'
        text = f"I need your {label} login saved first. On your Mac, run:\n{command}\nthen tell me when it's done."
        self._control({"op": "send", "text": text}, timeout=60)
        return "setup instructions sent; wait for the owner to say it is saved"
