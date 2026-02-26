"""Meridian Bridge — exposes memory methods to browser Claude via executor events.

Injects window.meridian and window.bridge into the browser, then runs
an event handler loop that proxies memory operations to the Meridian REST API.

Usage:
    python -m claudehopper.meridian_bridge              # start bridge daemon
    python -m claudehopper.meridian_bridge --inject      # just inject JS, exit
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys

import aiohttp

from .executor import ExecutorClient
from .comms import BrowserComms

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("claudehopper.meridian_bridge")

MERIDIAN_URL = os.environ.get("MERIDIAN_URL", "http://localhost:7891")

# JavaScript injected into browser context
INIT_JS = r"""
window.meridian = {
    _pending: {},
    _nextId: 1,

    _request(action, params) {
        return new Promise((resolve, reject) => {
            const id = 'mreq_' + this._nextId++;
            this._pending[id] = { resolve, reject };
            window._remote.emit('meridian', { id, action, ...params });
            setTimeout(() => {
                if (this._pending[id]) {
                    delete this._pending[id];
                    reject(new Error('Meridian request timed out (30s)'));
                }
            }, 30000);
        });
    },

    _handleResponse(id, result, error) {
        const p = this._pending[id];
        if (p) {
            delete this._pending[id];
            if (error) p.reject(new Error(error));
            else p.resolve(result);
        }
    },

    async recall(query, scope = 'all', maxTokens = 800) {
        return this._request('recall', { query, scope, max_tokens: maxTokens });
    },

    async remember(content, type = 'note', tags = [], importance = 3) {
        return this._request('remember', { content, type, tags, importance, source: 'webbie' });
    },

    async briefing(projectId = 'default') {
        return this._request('briefing', { project_id: projectId });
    },

    async forget(id, reason = '') {
        return this._request('forget', { id, reason });
    },
};

window.bridge = {
    _inbox: [],
    _listeners: [],

    sendToGiga(message, type = 'text') {
        window._remote.emit('bridge_msg', {
            from: 'webbie',
            to: 'giga',
            content: message,
            type: type,
        });
        console.log('[bridge] Sent to Giga:', message.slice(0, 60));
    },

    onMessage(callback) {
        this._listeners.push(callback);
    },

    _receive(msg) {
        this._inbox.push(msg);
        for (const cb of this._listeners) {
            try { cb(msg); } catch(e) { console.error('[bridge] listener error:', e); }
        }
    },

    getInbox() {
        return [...this._inbox];
    },
};

console.log('[meridian_bridge] Memory and bridge methods injected.');
console.log('[meridian_bridge] Available: window.meridian.recall(), .remember(), .briefing(), .forget()');
console.log('[meridian_bridge] Available: window.bridge.sendToGiga(), .onMessage(), .getInbox()');
"""

# Regex for meridian_cmd JSON blocks in chat text
_CMD_RE = re.compile(r'\{[^{}]*"meridian_cmd"\s*:\s*"[^"]+?"[^{}]*\}', re.DOTALL)

# JS to read chat messages
_READ_CHAT_JS = r"""
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


async def handle_meridian_event(client: ExecutorClient, http: aiohttp.ClientSession,
                                 meridian_url: str, event_data: dict):
    """Handle a meridian request from browser, proxy to Meridian REST API."""
    req_id = event_data.get("id", "unknown")
    action = event_data.get("action", "")
    logger.info(f"Meridian request: {action} (id={req_id})")

    try:
        result = None

        if action == "recall":
            async with http.post(f"{meridian_url}/api/memory/recall", json={
                "query": event_data.get("query", ""),
                "scope": event_data.get("scope", "all"),
                "max_tokens": event_data.get("max_tokens", 800),
            }) as resp:
                result = await resp.json()

        elif action == "remember":
            async with http.post(f"{meridian_url}/api/memory/remember", json={
                "content": event_data.get("content", ""),
                "type": event_data.get("type", "note"),
                "tags": event_data.get("tags", []),
                "importance": event_data.get("importance", 3),
            }) as resp:
                result = await resp.json()

        elif action == "briefing":
            project_id = event_data.get("project_id", "default")
            async with http.get(f"{meridian_url}/api/memory/briefing",
                               params={"project_id": project_id}) as resp:
                result = await resp.json()

        elif action == "forget":
            result = {"error": "forget not yet exposed via REST API"}

        else:
            result = {"error": f"Unknown action: {action}"}

        result_json = json.dumps(result, default=str)
        result_escaped = result_json.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        await client.exec(
            f"window.meridian._handleResponse('{req_id}', JSON.parse('{result_escaped}'), null);",
            timeout=5,
        )
        logger.info(f"Meridian response sent: {action} (id={req_id})")

    except Exception as e:
        logger.error(f"Meridian handler error: {e}")
        err_msg = str(e).replace("'", "\\'")
        try:
            await client.exec(
                f"window.meridian._handleResponse('{req_id}', null, '{err_msg}');",
                timeout=5,
            )
        except Exception:
            pass


_giga_inbox: list[dict] = []


def handle_bridge_msg(event_data: dict):
    """Handle a bridge message from browser Claude."""
    msg = {
        "from": event_data.get("from", "unknown"),
        "to": event_data.get("to", "unknown"),
        "content": event_data.get("content", ""),
        "type": event_data.get("type", "text"),
    }
    _giga_inbox.append(msg)
    logger.info(f"[BRIDGE] {msg['from']} -> {msg['to']}: {msg['content'][:80]}")


async def send_to_browser(client: ExecutorClient, message: str, msg_type: str = "text"):
    """Send a message to browser Claude via bridge (no ProseMirror)."""
    msg_escaped = json.dumps({
        "from": "giga",
        "to": "webbie",
        "content": message,
        "type": msg_type,
    }, default=str).replace("\\", "\\\\").replace("'", "\\'")
    await client.exec(
        f"window.bridge._receive(JSON.parse('{msg_escaped}'));",
        timeout=5,
    )
    logger.info(f"[BRIDGE] giga -> webbie: {message[:80]}")


async def _execute_meridian_cmd(http: aiohttp.ClientSession, meridian_url: str,
                                 cmd: dict) -> str:
    """Execute a meridian_cmd against the REST API."""
    action = cmd.get("meridian_cmd", "")
    try:
        if action == "recall":
            async with http.post(f"{meridian_url}/api/memory/recall", json={
                "query": cmd.get("query", ""),
                "scope": cmd.get("scope", "all"),
                "max_tokens": cmd.get("max_tokens", 800),
            }) as resp:
                data = await resp.json()
                return data.get("results", str(data))[:2000]

        elif action == "remember":
            async with http.post(f"{meridian_url}/api/memory/remember", json={
                "content": cmd.get("content", ""),
                "type": cmd.get("type", "note"),
                "tags": cmd.get("tags", []),
                "importance": cmd.get("importance", 3),
                "source": cmd.get("source", "webbie"),
            }) as resp:
                data = await resp.json()
                mem_id = data.get("id", "unknown")
                return f"Stored as {mem_id}" if data.get("stored") else f"Failed: {data}"

        elif action == "briefing":
            project_id = cmd.get("project_id", "default")
            async with http.get(f"{meridian_url}/api/memory/briefing",
                               params={"project_id": project_id}) as resp:
                data = await resp.json()
                return str(data.get("briefing", data))[:2000]

        elif action == "checkpoint":
            async with http.post(f"{meridian_url}/api/memory/checkpoint", json={
                "task_state": cmd.get("task_state", ""),
                "decisions": cmd.get("decisions", []),
                "warnings": cmd.get("warnings", []),
                "next_steps": cmd.get("next_steps", []),
                "working_set": cmd.get("working_set", {}),
            }) as resp:
                data = await resp.json()
                return f"Checkpoint: {data.get('checkpoint_id', 'unknown')}"

        else:
            return f"Unknown command: {action}"
    except Exception as e:
        return f"Error: {e}"


async def dom_command_watcher(client: ExecutorClient, http: aiohttp.ClientSession,
                               meridian_url: str, poll_interval: float = 15.0):
    """Poll chat DOM for meridian_cmd JSON blocks and auto-execute."""
    processed_cmds: set[str] = set()
    send_lock = asyncio.Lock()
    logger.info(f"DOM command watcher started (polling every {poll_interval}s)")

    # Snapshot existing DOM — mark all current commands as processed
    try:
        raw = await client.exec(_READ_CHAT_JS, timeout=10)
        if raw:
            messages = json.loads(raw)
            for msg in messages:
                if msg.get("role") != "assistant":
                    continue
                for match in _CMD_RE.finditer(msg.get("text", "")):
                    cmd_hash = hashlib.md5(match.group().encode()).hexdigest()
                    processed_cmds.add(cmd_hash)
            logger.info(f"[DOM-CMD] Baseline: {len(processed_cmds)} existing commands marked processed")
    except Exception as e:
        logger.warning(f"[DOM-CMD] Failed to snapshot baseline: {e}")

    # Poll for new commands
    while True:
        try:
            await asyncio.sleep(poll_interval)

            raw = await client.exec(_READ_CHAT_JS, timeout=10)
            if not raw:
                continue

            try:
                messages = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            for msg in messages:
                if msg.get("role") != "assistant":
                    continue

                text = msg.get("text", "")
                for match in _CMD_RE.finditer(text):
                    cmd_str = match.group()
                    cmd_hash = hashlib.md5(cmd_str.encode()).hexdigest()

                    if cmd_hash in processed_cmds:
                        continue

                    try:
                        cmd = json.loads(cmd_str)
                    except json.JSONDecodeError:
                        processed_cmds.add(cmd_hash)
                        continue

                    if "meridian_cmd" not in cmd:
                        processed_cmds.add(cmd_hash)
                        continue

                    action = cmd["meridian_cmd"]
                    logger.info(f"[DOM-CMD] Auto-executing: {action} (hash={cmd_hash[:8]})")

                    result = await _execute_meridian_cmd(http, meridian_url, cmd)
                    logger.info(f"[DOM-CMD] Result: {result[:200]}")

                    # Send result back via ProseMirror
                    from .comms import BrowserComms
                    reply = f"[AUTO-RELAY] {action} -> {result[:1500]}"
                    async with send_lock:
                        try:
                            async with BrowserComms() as comms:
                                await comms.send(reply)
                            logger.info(f"[DOM-CMD] Result sent back to chat")
                        except Exception as e:
                            logger.warning(f"[DOM-CMD] Failed to send result: {e}")

                    processed_cmds.add(cmd_hash)

        except Exception as e:
            logger.error(f"DOM watcher error: {e}")


async def main():
    inject_only = "--inject" in sys.argv
    meridian_url = os.environ.get("MERIDIAN_URL", MERIDIAN_URL)

    client = ExecutorClient(ssl_verify=False)
    await client.connect()

    logger.info("Injecting meridian bridge JS...")
    await client.exec(INIT_JS, timeout=5)
    logger.info("Bridge JS injected")

    if inject_only:
        logger.info("--inject mode: JS injected, exiting")
        await client.close()
        return

    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def on_meridian(event_data):
        await handle_meridian_event(client, http, meridian_url, event_data)

    client.on("meridian", on_meridian)
    client.on("bridge_msg", handle_bridge_msg)

    watcher_task = asyncio.create_task(dom_command_watcher(client, http, meridian_url))

    logger.info("Event handlers registered. DOM watcher started. Listening...")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        watcher_task.cancel()
        await http.close()
        await client.close()
        logger.info("Bridge shutdown")


if __name__ == "__main__":
    asyncio.run(main())
