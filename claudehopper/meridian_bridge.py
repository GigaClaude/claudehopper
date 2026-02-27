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
import time

import aiohttp

from .executor import ExecutorClient
from .comms import BrowserComms

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("claudehopper.meridian_bridge")

MERIDIAN_URL = os.environ.get("MERIDIAN_URL", "http://localhost:18101")

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

    sendToChannel(message) {
        window._remote.emit('bridge_msg', {
            from: 'webbie',
            to: 'channel',
            content: message,
            type: 'channel',
        });
        console.log('[bridge] Sent to channel:', message.slice(0, 60));
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

// ── Self-Exec: watch for { "js_cmd": "..." } in assistant messages, execute directly ──
(function() {
  if (window._selfExec) return;
  window._selfExec = { processed: new Set(), results: {} };

  const CMD_RE = /\{\s*"js_cmd"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}/g;

  function getNonce() {
    const s = document.querySelector('script[nonce]');
    return s ? s.nonce || s.getAttribute('nonce') : '';
  }

  function execJS(code) {
    return new Promise((resolve) => {
      const id = '_se_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
      const wrapped = `(async()=>{try{const __r=await(async function(){${code}})();window._selfExec.results['${id}']={ok:true,value:__r}}catch(e){window._selfExec.results['${id}']={ok:false,error:e.message}}})();`;
      const el = document.createElement('script');
      el.nonce = getNonce();
      el.textContent = wrapped;
      document.head.appendChild(el);
      el.remove();
      let tries = 0;
      const poll = setInterval(() => {
        const r = window._selfExec.results[id];
        if (r || tries++ > 100) {
          clearInterval(poll);
          delete window._selfExec.results[id];
          resolve(r || { ok: false, error: 'timeout' });
        }
      }, 50);
    });
  }

  function sendToChat(text) {
    const pm = document.querySelector('div.ProseMirror');
    if (!pm) return;
    const ed = pm.editor;
    if (ed && ed.commands) {
      ed.commands.clearContent();
      ed.commands.insertContent(text);
    } else {
      pm.focus();
      document.execCommand('selectAll');
      document.execCommand('delete');
      document.execCommand('insertText', false, text);
    }
    setTimeout(() => {
      const btn = document.querySelector('button[aria-label="Send message"]')
              || document.querySelector('button[aria-label="Send Message"]');
      if (btn && !btn.disabled) { btn.click(); return; }
      pm.dispatchEvent(new KeyboardEvent('keydown', {
        key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true
      }));
    }, 200);
  }

  function getLastAssistantText() {
    const msgs = document.querySelectorAll('[data-is-streaming]');
    const containers = msgs.length > 0 ? Array.from(msgs) : Array.from(document.querySelectorAll('div.grid.grid-cols-1'));
    for (let i = containers.length - 1; i >= 0; i--) {
      const el = containers[i];
      const isUser = el.className.includes('font-user-message') || el.className.includes('!font-user');
      const isStreaming = el.getAttribute('data-is-streaming') === 'true';
      if (!isUser && !isStreaming) return el.textContent.trim();
    }
    return '';
  }

  async function scan() {
    const text = getLastAssistantText();
    if (!text) return;
    CMD_RE.lastIndex = 0;
    let match;
    while ((match = CMD_RE.exec(text)) !== null) {
      const raw = match[0];
      const hash = raw.length + '_' + raw.slice(0, 40);
      if (window._selfExec.processed.has(hash)) continue;
      window._selfExec.processed.add(hash);
      const code = match[1].replace(/\\"/g, '"').replace(/\\n/g, '\n').replace(/\\\\/g, '\\');
      console.log('[self-exec] Run:', code.slice(0, 80));
      const result = await execJS(code);
      const output = result.ok ? String(result.value === undefined ? '(undefined)' : result.value) : 'ERROR: ' + result.error;
      console.log('[self-exec] Result:', output.slice(0, 200));
      sendToChat('[js_cmd result] ' + output.slice(0, 3000));
      break;
    }
  }

  const observer = new MutationObserver(() => {
    clearTimeout(window._selfExec._debounce);
    window._selfExec._debounce = setTimeout(scan, 2000);
  });
  observer.observe(document.querySelector('main') || document.body, { childList: true, subtree: true, characterData: true });
  setInterval(scan, 10000);
  console.log('[self-exec] Active. Watching for { "js_cmd": "..." } in assistant messages.');
})();

console.log('[meridian_bridge] Memory, bridge, and self-exec injected.');
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
    const isStreaming = el.getAttribute('data-is-streaming') === 'true';
    messages.push({
        role: isUser ? 'human' : 'assistant',
        text: text.slice(0, 8000),
        len: text.length,
        streaming: isStreaming
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


def make_bridge_msg_handler(http: "aiohttp.ClientSession", meridian_url: str):
    """Create bridge message handler with HTTP session for channel forwarding."""

    async def handle_bridge_msg(event_data: dict):
        """Handle a bridge message from browser Claude."""
        msg = {
            "from": event_data.get("from", "unknown"),
            "to": event_data.get("to", "unknown"),
            "content": event_data.get("content", ""),
            "type": event_data.get("type", "text"),
        }
        _giga_inbox.append(msg)
        logger.info(f"[BRIDGE] {msg['from']} -> {msg['to']}: {msg['content'][:80]}")

        # Forward channel-type messages to Meridian channel API
        if msg["to"] == "channel" or msg["type"] == "channel":
            try:
                async with http.post(
                    f"{meridian_url}/api/channel/send",
                    json={"sender": msg["from"], "content": msg["content"]},
                ) as resp:
                    data = await resp.json()
                    logger.info(f"[BRIDGE->CHANNEL] {msg['from']}: {msg['content'][:60]} (msg #{data.get('count', '?')})")
            except Exception as e:
                logger.warning(f"[BRIDGE->CHANNEL] Forward failed: {e}")

    return handle_bridge_msg


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
                               params={"project_id": project_id},
                               timeout=aiohttp.ClientTimeout(total=60)) as resp:
                data = await resp.json()
                briefing = str(data.get("briefing", data))
                # Briefing can be 6k+ chars — too large for DOM injection.
                # Extract high-signal lines only: task state, warnings, next steps.
                lines = briefing.split("\n")
                summary_lines = []
                for line in lines:
                    s = line.strip().strip('"').strip(",").strip()
                    if any(k in s.lower() for k in [
                        '"task":', '"warnings":', '"next_steps":', '"active_warnings":',
                        "[high]", "[med]",
                    ]) or (s.startswith('"') and ":" in s and len(s) < 120):
                        summary_lines.append(s)
                if summary_lines:
                    return f"BRIEFING ({len(briefing)} chars):\n" + "\n".join(summary_lines[:20])
                return f"BRIEFING ({len(briefing)} chars):\n{briefing[:800]}"

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

        elif action == "channel_send":
            content = cmd.get("content", "")
            sender = cmd.get("sender", "webbie")
            async with http.post(f"{meridian_url}/api/channel/send", json={
                "sender": sender,
                "content": content,
            }) as resp:
                data = await resp.json()
                return f"Posted to channel (msg #{data.get('count', '?')})" if data.get("ok") else f"Failed: {data}"

        else:
            return f"Unknown command: {action}"
    except Exception as e:
        return f"Error: {e}"


async def handle_cmd_event(client: ExecutorClient, http: aiohttp.ClientSession,
                            meridian_url: str, cmd_data: dict):
    """Handle 'cmd' events from TTB browser bridge.

    Routes by key present in cmd_data:
      meridian_cmd → Meridian REST API
      bash_cmd     → shell execution
      js_cmd       → inject JS back to browser
    """
    try:
        if "meridian_cmd" in cmd_data:
            logger.info(f"[CMD] meridian_cmd: {cmd_data['meridian_cmd']}")
            result = await _execute_meridian_cmd(http, meridian_url, cmd_data)
            # Send result back via TTB if available, else bridge
            result_json = json.dumps({"type": "meridian_result", "result": result}, default=str)
            result_escaped = result_json.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
            await client.exec(
                f"console.log('[cmd] meridian result:', {json.dumps(result[:1500])})",
                timeout=5,
            )

        elif "bash_cmd" in cmd_data:
            cmd_str = cmd_data["bash_cmd"]
            timeout_s = min(cmd_data.get("timeout", 30), 120)  # Cap at 2 min
            logger.info(f"[CMD] bash_cmd: {cmd_str[:80]} (timeout={timeout_s}s)")

            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.expanduser("~"),
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
                output = stdout.decode("utf-8", errors="replace")[:4000]
                exit_code = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                output = "(timed out)"
                exit_code = -1

            logger.info(f"[CMD] bash_cmd exit={exit_code}, {len(output)} chars")
            result_msg = f"[bash exit={exit_code}] {output}"
            await client.exec(
                f"console.log('[cmd] bash result:', {json.dumps(result_msg[:2000])})",
                timeout=5,
            )

        elif "ping" in cmd_data:
            # Webbie pings Giga's side — return latest channel msg# as sanity check
            try:
                async with http.get(
                    f"{meridian_url}/api/channel/history",
                    params={"limit": "1"},
                ) as resp:
                    data = await resp.json()
                msgs = data.get("messages", [])
                latest = msgs[-1] if msgs else {}
                count = data.get("count", len(msgs))
                result = json.dumps({
                    "pong": True,
                    "channel_msgs": count,
                    "latest_sender": latest.get("sender", ""),
                    "latest_preview": latest.get("content", "")[:80],
                }, default=str)
            except Exception as e:
                result = json.dumps({"pong": True, "error": str(e)})
            logger.info(f"[CMD] ping -> {result[:120]}")
            await client.exec(
                f"console.log('[cmd] ping result:', {json.dumps(result)})",
                timeout=5,
            )

        elif "js_cmd" in cmd_data:
            code = cmd_data["js_cmd"]
            logger.info(f"[CMD] js_cmd: {code[:80]}")
            try:
                result = await client.exec(code, timeout=cmd_data.get("timeout", 10))
                logger.info(f"[CMD] js_cmd result: {str(result)[:200]}")
            except Exception as e:
                logger.error(f"[CMD] js_cmd error: {e}")

        else:
            logger.warning(f"[CMD] Unknown cmd keys: {list(cmd_data.keys())}")

    except Exception as e:
        logger.error(f"[CMD] Handler error: {e}")


async def dom_command_watcher(client: ExecutorClient, http: aiohttp.ClientSession,
                               meridian_url: str, poll_interval: float = 15.0):
    """Poll chat DOM for new assistant messages containing meridian_cmd JSON blocks."""
    processed_cmds: set[str] = set()
    send_lock = asyncio.Lock()
    logger.info(f"DOM command watcher started (polling every {poll_interval}s)")

    # Snapshot existing DOM — mark all current commands as already processed
    try:
        raw = await client.exec(_READ_CHAT_JS, timeout=10)
        if raw:
            messages = json.loads(raw)
            for msg in messages:
                if msg.get("role") != "assistant":
                    continue
                text = msg.get("text", "")
                for match in _CMD_RE.finditer(text):
                    cmd_hash = hashlib.md5(match.group().encode()).hexdigest()
                    processed_cmds.add(cmd_hash)
            logger.info(f"[DOM-CMD] Baseline: {len(processed_cmds)} cmds marked seen")
    except Exception as e:
        logger.warning(f"[DOM-CMD] Failed to snapshot baseline: {e}")

    # Poll for new messages
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
            if not client.connected:
                logger.info("[DOM-CMD] Connection dead, forcing reconnect...")
                try:
                    await client.close()
                except Exception:
                    pass
                try:
                    await client.connect()
                    await client.exec(INIT_JS, timeout=5)
                    logger.info("[DOM-CMD] Reconnected and re-injected bridge JS")
                except Exception as re:
                    logger.warning(f"[DOM-CMD] Reconnect failed: {re}, will retry next cycle")


async def channel_watcher(client: ExecutorClient, http: aiohttp.ClientSession,
                           meridian_url: str, poll_interval: float = 3.0):
    """Poll the IRC channel for new messages and push them to Webbie's browser.

    This bridges the gap between the IRC channel (Meridian web server) and
    Webbie's browser tab. Without this, channel messages only go to WebSocket
    subscribers on the channel.html page — Webbie never sees them.

    Uses a persistent BrowserComms connection to avoid reconnecting per message.
    """
    last_ts: float = time.time()  # Only forward messages after we start
    send_lock = asyncio.Lock()
    comms: BrowserComms | None = None
    logger.info(f"Channel watcher started (polling every {poll_interval}s, since ts={last_ts:.0f})")

    async def get_comms() -> BrowserComms:
        """Get or create a persistent BrowserComms connection."""
        nonlocal comms
        if comms is None or comms.client is None or not comms.client.connected:
            if comms:
                try:
                    await comms.close()
                except Exception:
                    pass
            comms = BrowserComms()
            await comms.connect()
        return comms

    while True:
        try:
            await asyncio.sleep(poll_interval)

            async with http.get(
                f"{meridian_url}/api/channel/history",
                params={"since": str(last_ts), "limit": "50"},
            ) as resp:
                data = await resp.json()

            messages = data.get("messages", [])
            if not messages:
                continue

            for msg in messages:
                sender = msg.get("sender", "unknown")
                content = msg.get("content", "")
                ts = msg.get("ts", 0)

                # Skip messages from webbie to avoid echo loops
                if sender.lower() == "webbie":
                    if ts > last_ts:
                        last_ts = ts
                    continue

                # Deliver to Webbie's chat via ProseMirror (actually visible)
                relay_msg = f"[#{sender}] {content}"
                try:
                    async with send_lock:
                        c = await get_comms()
                        ok = await c.send(relay_msg)
                    if ok:
                        logger.info(f"[CHANNEL->CHAT] {sender}: {content[:60]}")
                    else:
                        logger.warning(f"[CHANNEL->CHAT] Send returned False for: {content[:60]}")
                except Exception as e:
                    logger.warning(f"[CHANNEL->CHAT] Failed: {e}")
                    # Force reconnect on next attempt
                    comms = None

                if ts > last_ts:
                    last_ts = ts

        except Exception as e:
            logger.debug(f"Channel watcher error (retrying): {e}")
            if not client.connected:
                logger.info("[CHANNEL] Executor disconnected, attempting reconnect...")
                try:
                    await client.close()
                except Exception:
                    pass
                try:
                    await client.connect()
                    await client.exec(INIT_JS, timeout=5)
                    logger.info("[CHANNEL] Reconnected and re-injected bridge JS")
                except Exception as re:
                    logger.warning(f"[CHANNEL] Reconnect failed: {re}")
                comms = None  # Force BrowserComms reconnect too


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
    client.on("bridge_msg", make_bridge_msg_handler(http, meridian_url))
    client.on("cmd", lambda data: handle_cmd_event(client, http, meridian_url, data))

    watcher_task = asyncio.create_task(dom_command_watcher(client, http, meridian_url))
    channel_task = asyncio.create_task(channel_watcher(client, http, meridian_url))

    logger.info("Event handlers registered. DOM watcher + channel watcher started. Listening...")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        watcher_task.cancel()
        channel_task.cancel()
        await http.close()
        await client.close()
        logger.info("Bridge shutdown")


if __name__ == "__main__":
    asyncio.run(main())
