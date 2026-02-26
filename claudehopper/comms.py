"""Reliable bidirectional comms with browser Claude via executor.

Provides send/receive with dedup, state tracking, and streaming stabilization.

Usage:
    from claudehopper.comms import BrowserComms
    async with BrowserComms() as comms:
        await comms.send("hello")
        messages = await comms.read_new()

CLI:
    python -m claudehopper.comms send "message here"
    python -m claudehopper.comms read
    python -m claudehopper.comms new
"""

import asyncio
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime

from .executor import ExecutorClient

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".claudehopper"
STATE_FILE = STATE_DIR / "comms_state.json"
LOG_FILE = STATE_DIR / "comms.log"


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"sent_hashes": [], "last_read_count": 0, "last_msg_hash": ""}


def _save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _hash(text: str) -> str:
    return hashlib.md5(text.strip()[:200].encode()).hexdigest()[:12]


# JS to read all visible messages from claude.ai chat
READ_JS = r"""
const allMsgs = document.querySelectorAll('[data-is-streaming]');
const containers = allMsgs.length > 0
    ? allMsgs
    : document.querySelectorAll('div.grid.grid-cols-1');

const messages = [];
for (const el of containers) {
    const text = el.textContent.trim();
    if (text.length < 2) continue;
    const isUser = el.className.includes('font-user-message')
        || el.className.includes('!font-user')
        || el.getAttribute('data-is-streaming') === null && el.querySelector('[class*="font-user"]');
    messages.push({
        role: isUser ? 'human' : 'assistant',
        text: text.slice(0, 8000),
        len: text.length
    });
}
return JSON.stringify(messages.slice(-15));
"""


def _make_send_js(message: str) -> str:
    """JS to insert text into ProseMirror editor via execCommand."""
    safe = json.dumps(message)
    return f"""
const e = document.querySelector("div.ProseMirror");
if (!e) return JSON.stringify({{ok: false, error: "no editor"}});
e.focus();
document.execCommand("selectAll");
document.execCommand("delete");
document.execCommand("insertText", false, {safe});
return JSON.stringify({{ok: true, preview: e.textContent.slice(0, 80)}});
"""


def _make_insert_and_send_js(message: str) -> str:
    """JS to insert text AND click send in one shot with retry.

    Combines insert + send into a single exec call to avoid the race
    condition where the send button isn't ready between two separate calls.
    """
    safe = json.dumps(message)
    return f"""
const e = document.querySelector("div.ProseMirror");
if (!e) return JSON.stringify({{ok: false, error: "no editor"}});
e.focus();
document.execCommand("selectAll");
document.execCommand("delete");
document.execCommand("insertText", false, {safe});

// Wait for ProseMirror to process and enable the send button
const findBtn = () => document.querySelector('button[aria-label="Send message"]')
    || document.querySelector('button[aria-label="Send Message"]');

let attempts = 0;
while (attempts < 20) {{
    await new Promise(r => setTimeout(r, 150));
    const b = findBtn();
    if (b && !b.disabled) {{
        b.click();
        return JSON.stringify({{ok: true, sent: true, preview: e.textContent.slice(0, 80)}});
    }}
    attempts++;
}}

// Button click failed — try Enter key on the editor as fallback
// Many chat UIs submit on Enter, bypassing the button entirely
e.focus();
e.dispatchEvent(new KeyboardEvent("keydown", {{
    key: "Enter", code: "Enter", keyCode: 13, which: 13,
    bubbles: true, cancelable: true
}}));
await new Promise(r => setTimeout(r, 300));

// Check if the text was cleared (indicating submit worked)
if (!e.textContent || e.textContent.trim().length === 0) {{
    return JSON.stringify({{ok: true, sent: true, method: "enter_key", preview: ""}});
}}

// Neither button nor Enter worked
const b = findBtn();
return JSON.stringify({{
    ok: true, sent: false,
    error: b ? (b.disabled ? "button stayed disabled" : "unknown") : "no button found",
    preview: e.textContent.slice(0, 80)
}});
"""


CLICK_SEND_JS = """
const b = document.querySelector('button[aria-label="Send message"]')
       || document.querySelector('button[aria-label="Send Message"]');
if (b && !b.disabled) {
    b.click();
    return JSON.stringify({ok: true, method: "button"});
}

// Fallback: try Enter key on editor
const e = document.querySelector("div.ProseMirror");
if (e) {
    e.focus();
    e.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter", code: "Enter", keyCode: 13, which: 13,
        bubbles: true, cancelable: true
    }));
    await new Promise(r => setTimeout(r, 300));
    if (!e.textContent || e.textContent.trim().length === 0) {
        return JSON.stringify({ok: true, method: "enter_key"});
    }
}

return JSON.stringify({ok: false, error: b ? (b.disabled ? "disabled" : "click failed") : "no button"});
"""


class BrowserComms:
    """Bidirectional communication with browser-based Claude."""

    def __init__(self, executor_url: str | None = None):
        """
        Args:
            executor_url: Executor server URL. Defaults to EXECUTOR_URL env var.
        """
        self._executor_url = executor_url
        self.client: ExecutorClient | None = None
        self.state = _load_state()

    async def connect(self):
        self.client = ExecutorClient(url=self._executor_url, ssl_verify=False)
        await self.client.connect()

    async def close(self):
        if self.client:
            await self.client.close()
        _save_state(self.state)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def read_all(self) -> list[dict]:
        """Read all visible messages from the chat."""
        raw = await self.client.exec(READ_JS, timeout=10)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _log(f"Failed to parse read result: {raw!r}")
            return []

    async def read_new(self) -> list[dict]:
        """Read only messages we haven't seen before."""
        all_msgs = await self.read_all()
        if not all_msgs:
            return []

        last_count = self.state.get("last_read_count", 0)
        last_hash = self.state.get("last_msg_hash", "")

        if len(all_msgs) == last_count and _hash(all_msgs[-1]["text"]) == last_hash:
            return []

        new_msgs = []
        if last_count > 0 and last_hash:
            found_idx = -1
            for i, m in enumerate(all_msgs):
                if _hash(m["text"]) == last_hash:
                    found_idx = i
            if found_idx >= 0:
                new_msgs = all_msgs[found_idx + 1:]
            else:
                new_msgs = all_msgs[max(0, last_count):]
        else:
            new_msgs = all_msgs

        self.state["last_read_count"] = len(all_msgs)
        if all_msgs:
            self.state["last_msg_hash"] = _hash(all_msgs[-1]["text"])
        _save_state(self.state)

        return new_msgs

    async def send(self, message: str) -> bool:
        """Send a message. Returns True on success.

        Uses a combined insert+send JS to avoid the race condition where
        the send button isn't ready between two separate exec calls.
        Falls back to two-step if the combined approach fails.
        """
        msg_hash = _hash(message)

        recent_hashes = self.state.get("sent_hashes", [])
        if msg_hash in recent_hashes[-5:]:
            _log(f"Dedup: message already sent (hash={msg_hash})")
            return False

        # Try combined insert+send (avoids race condition)
        result_raw = await self.client.exec(
            _make_insert_and_send_js(message), timeout=10,
        )
        try:
            result = json.loads(result_raw) if result_raw else {"ok": False, "error": "no response"}
        except (json.JSONDecodeError, TypeError):
            result = {"ok": False, "error": f"parse error: {result_raw!r}"}

        if not result.get("ok"):
            _log(f"Insert failed: {result.get('error')}")
            return False

        if result.get("sent"):
            _log(f"Sent (combined): {result.get('preview', '')}")
            recent_hashes.append(msg_hash)
            self.state["sent_hashes"] = recent_hashes[-20:]
            _save_state(self.state)
            return True

        # Combined insert worked but send didn't fire — fallback to separate click
        _log(f"Text inserted, send pending: {result.get('error', 'retrying click')}")
        await asyncio.sleep(0.5)

        send_raw = await self.client.exec(CLICK_SEND_JS, timeout=5)
        try:
            send_result = json.loads(send_raw) if send_raw else {"ok": False, "error": "no response"}
        except (json.JSONDecodeError, TypeError):
            send_result = {"ok": False, "error": f"parse error: {send_raw!r}"}

        if not send_result.get("ok"):
            _log(f"Send click failed: {send_result.get('error')}")
            _log("Text is in editor — may need manual send")
            return False

        recent_hashes.append(msg_hash)
        self.state["sent_hashes"] = recent_hashes[-20:]
        _save_state(self.state)

        _log(f"Sent (fallback): {message[:60]}...")
        return True

    async def wait_for_response(self, timeout: int = 120, poll_interval: float = 3.0) -> str | None:
        """Wait for a new assistant message. Stabilizes streaming before returning."""
        baseline = await self.read_all()
        baseline_count = len(baseline)
        baseline_hash = _hash(baseline[-1]["text"]) if baseline else ""

        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            current = await self.read_all()
            if not current:
                continue

            has_new = False
            if len(current) > baseline_count:
                new_msgs = current[baseline_count:]
                if any(m["role"] == "assistant" for m in new_msgs):
                    has_new = True
            elif current[-1]["role"] == "assistant" and _hash(current[-1]["text"]) != baseline_hash:
                has_new = True

            if not has_new:
                continue

            stable = await self._wait_for_stable(timeout=max(10, timeout - elapsed))
            if stable:
                self.state["last_read_count"] = len(stable)
                self.state["last_msg_hash"] = _hash(stable[-1]["text"])
                _save_state(self.state)
                for m in reversed(stable):
                    if m["role"] == "assistant":
                        return m["text"]

        return None

    async def _wait_for_stable(self, timeout: int = 60, settle_interval: float = 3.0) -> list[dict] | None:
        """Poll until content stops changing (streaming done)."""
        prev_hash = ""
        stable_count = 0
        elapsed = 0

        while elapsed < timeout:
            await asyncio.sleep(settle_interval)
            elapsed += settle_interval

            current = await self.read_all()
            if not current:
                continue

            curr_hash = _hash(current[-1]["text"])
            if curr_hash == prev_hash:
                stable_count += 1
                if stable_count >= 2:
                    return current
            else:
                stable_count = 0
            prev_hash = curr_hash

        return await self.read_all()


# ── CLI ──

async def _cli_send(msg: str):
    async with BrowserComms() as comms:
        ok = await comms.send(msg)
        if ok:
            print("Waiting for response...")
            resp = await comms.wait_for_response(timeout=180)
            if resp:
                print(f"\n[RESPONSE]: {resp}")
            else:
                print("(no response within timeout)")


async def _cli_read():
    async with BrowserComms() as comms:
        msgs = await comms.read_all()
        for m in msgs:
            role = m["role"].upper()
            print(f"\n[{role}]: {m['text'][:500]}")


async def _cli_new():
    async with BrowserComms() as comms:
        msgs = await comms.read_new()
        if not msgs:
            print("(no new messages)")
        for m in msgs:
            role = m["role"].upper()
            print(f"\n[{role}]: {m['text'][:500]}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m claudehopper.comms [send|read|new] [message]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "send":
        msg = " ".join(sys.argv[2:]) or "ping"
        asyncio.run(_cli_send(msg))
    elif cmd == "read":
        asyncio.run(_cli_read())
    elif cmd == "new":
        asyncio.run(_cli_new())
    else:
        print(f"Unknown command: {cmd}")
