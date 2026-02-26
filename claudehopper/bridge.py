"""ClaudeHopper Bridge — connects CLI Claude and browser Claude
via the executor WebSocket and Meridian REST API.

Injects window._meridian into the browser with recall/remember/briefing/send/poll.
Polls Meridian for messages to push to the browser.

Usage:
    python -m claudehopper.bridge
    python -m claudehopper.bridge --meridian-url http://localhost:7891
"""

import asyncio
import json
import logging
import os

from .executor import ExecutorClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("claudehopper.bridge")

MERIDIAN_URL = os.environ.get("MERIDIAN_URL", "http://localhost:7891")

# JavaScript injected into browser tab.
# Creates window._meridian with recall(), remember(), send(), poll().
BRIDGE_INIT_JS = r"""
(function() {
    if (window._meridian) {
        console.log('[Meridian] Bridge already loaded, skipping');
        return;
    }

    const MERIDIAN = '__MERIDIAN_URL__';

    async function apiCall(endpoint, body) {
        const resp = await fetch(MERIDIAN + endpoint, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        return await resp.json();
    }

    async function apiGet(endpoint) {
        const resp = await fetch(MERIDIAN + endpoint);
        return await resp.json();
    }

    window._meridian = {
        recall: (query, opts = {}) => apiCall('/api/memory/recall', {
            query,
            scope: opts.scope || 'all',
            max_tokens: opts.max_tokens || 800,
        }),

        remember: (content, opts = {}) => apiCall('/api/memory/remember', {
            content,
            type: opts.type || 'note',
            tags: opts.tags || [],
            importance: opts.importance || 3,
        }),

        briefing: (project_id = 'default') =>
            apiGet('/api/memory/briefing?project_id=' + project_id),

        send: (content, type = 'text') => apiCall('/api/bridge/send', {
            from_id: 'webbie',
            to_id: 'giga',
            content,
            msg_type: type,
        }),

        poll: (since = 0) =>
            apiGet('/api/bridge/poll?recipient=webbie&since=' + since),

        ack: (before) => apiCall('/api/bridge/ack', {
            recipient: 'webbie',
            before,
        }),

        _onMessage: null,
        version: '0.2.0',
        ready: true,
    };

    if (window._remote) {
        window._remote.emit('meridian_bridge_ready', {
            version: window._meridian.version,
            url: MERIDIAN,
        });
    }

    console.log('[Meridian] Bridge loaded. Use window._meridian.recall("query") to search memory.');
})();
"""


class ClaudeHopperBridge:
    """Manages the executor connection and injects the bridge into the browser."""

    def __init__(self, meridian_url: str | None = None,
                 executor_url: str | None = None):
        self.meridian_url = meridian_url or MERIDIAN_URL
        self.executor_url = executor_url
        self.client: ExecutorClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._last_poll_ts: float = 0

    async def start(self):
        """Connect to executor and inject the bridge JS."""
        self.client = ExecutorClient(url=self.executor_url, ssl_verify=False)
        await self.client.connect()
        logger.info("Connected to executor")

        init_js = BRIDGE_INIT_JS.replace("__MERIDIAN_URL__", self.meridian_url)
        await self.client.add_init(init_js)
        logger.info("Bridge init code registered")

        self.client.on("meridian_bridge_ready", self._on_bridge_ready)
        self.client.on("meridian_msg_to_giga", self._on_msg_from_browser)

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"ClaudeHopper bridge running (Meridian: {self.meridian_url})")

    async def _on_bridge_ready(self, data):
        logger.info(f"Browser bridge ready: {data}")

    async def _on_msg_from_browser(self, data):
        logger.info(f"Message from browser: {data}")

    async def _poll_loop(self):
        """Poll for CLI→browser messages and push via exec."""
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            while True:
                try:
                    async with http.get(
                        f"{self.meridian_url}/api/bridge/poll",
                        params={"recipient": "webbie", "since": str(self._last_poll_ts)},
                    ) as resp:
                        data = await resp.json()
                    for msg in data.get("messages", []):
                        push_js = f"""
                        if (window._meridian && window._meridian._onMessage) {{
                            window._meridian._onMessage({json.dumps(msg)});
                        }} else {{
                            console.log('[Meridian] Incoming:', {json.dumps(msg['content'][:100])});
                        }}
                        """
                        await self.client.exec(push_js)
                        self._last_poll_ts = msg["ts"]
                        logger.info(f"Pushed message to browser: {msg['content'][:60]}")

                    if data.get("messages"):
                        async with http.post(
                            f"{self.meridian_url}/api/bridge/ack",
                            json={"recipient": "webbie", "before": self._last_poll_ts},
                        ) as _:
                            pass
                except Exception as e:
                    logger.debug(f"Poll error (retrying): {e}")

                await asyncio.sleep(2)

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self.client:
            await self.client.close()


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="ClaudeHopper Bridge")
    parser.add_argument("--meridian-url", default=MERIDIAN_URL)
    parser.add_argument("--executor-url", default=None)
    args = parser.parse_args()

    bridge = ClaudeHopperBridge(
        meridian_url=args.meridian_url,
        executor_url=args.executor_url,
    )
    await bridge.start()

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down")
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
