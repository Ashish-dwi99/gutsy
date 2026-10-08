"""The owner's dedicated Chrome, and secret fill into it.

Gutsy uses its own Chrome profile with a DevTools port so that:
- the brain's browser tools (Playwright MCP attached with --cdp-endpoint)
  drive real, logged-in sessions that persist between tasks;
- `fill` can type a saved login into the same tab over CDP, so the secret
  goes keychain -> Chrome and never through the model;
- the payment probe tells the permission gate when a checkout is open.

Chrome refuses a DevTools port on the default profile, so a separate
profile is required, not just preferred. The owner sees this window on their
Mac and can solve a CAPTCHA or passkey prompt in it directly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.sync.client import connect

from .vault import VaultError, get_secret

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
CHROME_COMMANDS = ("google-chrome", "chromium", "chromium-browser")


def endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for command in CHROME_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    return None


def is_running(port: int) -> bool:
    try:
        return httpx.get(f"{endpoint(port)}/json/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def launch(port: int, profile: Path) -> None:
    """Start Gutsy's Chrome if it is not already listening."""
    if is_running(port):
        return
    chrome = find_chrome()
    if chrome is None:
        raise RuntimeError("no Chrome, Chromium, Brave, or Edge found")
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def page_targets(port: int) -> list[dict[str, Any]]:
    """Page tabs, most recently active first (Chrome's own /json order)."""
    response = httpx.get(f"{endpoint(port)}/json/list", timeout=5)
    response.raise_for_status()
    return [target for target in response.json() if target.get("type") == "page"]


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    if not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def choose_target(targets: list[dict[str, Any]], origins: list[str]) -> dict[str, Any] | None:
    """The first tab whose exact origin the vault item allows.

    Exact origin matching is the phishing guard: a look-alike domain, a
    different subdomain, or plain http never receives a saved secret.
    """
    allowed = set(origins)
    for target in targets:
        if origin_of(str(target.get("url") or "")) in allowed:
            return target
    return None


PAYMENT_FRAME_HOSTS = (
    "js.stripe.com",
    "checkout.stripe.com",
    "checkout.razorpay.com",
    "api.razorpay.com",
    "www.paypal.com",
    "assets.braintreegateway.com",
    "checkoutshopper-live.adyen.com",
    "secure.payu.in",
    "sdk.cashfree.com",
    "api.juspay.in",
    "checkout.com",
)

# True when the page asks for card details: card inputs in the page or a payment provider's frame.
PAYMENT_PROBE = """(() => {
  const cardField = /cc-number|cc-csc|cc-exp|card.?number|cardnumber|\\bcvv\\b|\\bcvc\\b|security.?code/i;
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const inputs = Array.from(document.querySelectorAll("input")).filter(visible);
  if (inputs.some((el) => cardField.test([el.autocomplete, el.name, el.id, el.placeholder].join(" ")))) return true;
  const hosts = %s;
  return Array.from(document.querySelectorAll("iframe")).some((frame) => {
    try { return hosts.includes(new URL(frame.src).hostname); } catch (error) { return false; }
  });
})()"""


def _evaluate(target: dict[str, Any], expression: str) -> Any:
    with connect(target["webSocketDebuggerUrl"], max_size=4_000_000, open_timeout=10) as ws:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True, "awaitPromise": True}}))
        while True:
            reply = json.loads(ws.recv(timeout=15))
            if reply.get("id") == 1:
                return reply


def payment_page_url(port: int) -> str | None:
    """The URL of the first open tab that asks for card details, if any."""
    if not is_running(port):
        return None
    probe = PAYMENT_PROBE % json.dumps(list(PAYMENT_FRAME_HOSTS))
    for target in page_targets(port):
        if not str(target.get("url") or "").startswith("http"):
            continue
        reply = _evaluate(target, probe)
        if reply.get("result", {}).get("result", {}).get("value") is True:
            return str(target["url"])
    return None


def fill(port: int, item: dict[str, Any]) -> dict[str, Any]:
    """Fill one saved login into the matching tab; returns field names only."""
    target = choose_target(page_targets(port), item["origins"])
    if target is None:
        allowed = ", ".join(item["origins"])
        raise VaultError(f"no open tab is on {allowed}; open the sign-in page there first")
    secret = {"kind": item["kind"], **get_secret(item["handle"])}
    script = resources.files("gutsy").joinpath("browser_fill.js").read_text(encoding="utf-8")
    expected_origin = origin_of(target["url"])
    expression = f"({script.strip()})({json.dumps(secret)}, {json.dumps(expected_origin)})"
    reply = _evaluate(target, expression)
    if "error" in reply:
        raise VaultError(f"Chrome refused the fill: {reply['error'].get('message')}")
    result = reply["result"]
    if "exceptionDetails" in result:
        raise VaultError("the fill script failed on this page")
    value = result["result"].get("value") or {}
    return {"ok": bool(value.get("ok")), "filled": list(value.get("filled") or []), "error": value.get("error")}
