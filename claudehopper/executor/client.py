"""WebSocket client for the executor server.

Executes JavaScript in a remote browser via a WebSocket bridge.
The executor server runs a browser extension that receives JS,
executes it, and returns results.
"""

import json
import uuid
import logging
import asyncio
from urllib.parse import urlparse

import websockets

import typing as tp

from .config import ExecutorConfig

logger = logging.getLogger(__name__)

_CONSOLE_TAP_JS: str = r"""
(function consoleTap(options) {
  if (window.__consoleTapInstalled) return;
  window.__consoleTapInstalled = true;
  var opts = options || {};
  var forwardTo = opts.forwardTo || null;
  var levels = ["log", "info", "warn", "error", "debug", "trace"];
  var original = Object.create(null);

  var safeRepr = function(v) {
    try {
      if (v instanceof Error)
        return { __type: "Error", name: v.name, message: v.message, stack: v.stack };
      if (typeof Node !== "undefined" && v instanceof Node)
        return { __type: "Node", text: (v.outerHTML || "").slice(0, 200) || v.nodeName };
      if (v === window) return { __type: "Window" };
      if (v === document) return { __type: "Document" };
      return JSON.parse(JSON.stringify(v));
    } catch(e) {
      try { return String(v); } catch(e2) { return "[unserializable]"; }
    }
  };

  var emit = function(payload) {
    if (typeof forwardTo === "function") {
      try { forwardTo(payload); } catch(e) {}
    }
  };

  for (var i = 0; i < levels.length; i++) {
    (function(level) {
      var fn = console[level];
      if (typeof fn !== "function") return;
      original[level] = fn.bind(console);
      console[level] = function() {
        var args = Array.prototype.slice.call(arguments);
        emit({
          level: level,
          ts: Date.now(),
          argsSafe: args.map(safeRepr)
        });
        return original[level].apply(console, args);
      };
    })(levels[i]);
  }

  window.addEventListener("error", function(e) {
    emit({
      level: "error", ts: Date.now(),
      argsSafe: [safeRepr(e.message)],
      source: "window.error",
      filename: e.filename, lineno: e.lineno, colno: e.colno,
      errorSafe: safeRepr(e.error)
    });
  });

  window.addEventListener("unhandledrejection", function(e) {
    emit({
      level: "error", ts: Date.now(),
      argsSafe: ["Unhandled rejection"],
      source: "unhandledrejection",
      reasonSafe: safeRepr(e.reason)
    });
  });
})({
  forwardTo: function(p) { window._remote.emit("console", p); }
});
""".strip()


class ExecutorClient:
    """WebSocket client for executing JavaScript in a remote browser."""

    def __init__(
        self,
        url: tp.Optional[str] = None,
        ssl_verify: bool = True,
        cert_path: tp.Optional[str] = None,
        key_path: tp.Optional[str] = None,
    ):
        """
        Args:
            url: Executor URL (https://host:port). Defaults to EXECUTOR_URL env var.
            ssl_verify: Verify SSL certs. False for self-signed.
            cert_path: Client certificate PEM path.
            key_path: Client key PEM path.
        """
        config = ExecutorConfig(
            url=url, cert_path=cert_path, key_path=key_path, ssl_verify=ssl_verify,
        )
        self._cert_path = config.cert_path
        self._key_path = config.key_path
        self.ssl_verify = config.ssl_verify

        # Convert HTTP(S) URL to WebSocket URL
        parsed = urlparse(config.url)
        if parsed.scheme in ("https", "wss"):
            ws_scheme = "wss"
        else:
            ws_scheme = "ws"
        path = parsed.path.rstrip("/") + "/client"
        self.ws_url = f"{ws_scheme}://{parsed.netloc}{path}"

        self.ws: tp.Optional[tp.Any] = None
        self.pending_calls: dict = {}
        self.connected = False
        self.event_handlers: tp.Dict[str, tp.List[tp.Callable]] = {}

    async def connect(self):
        """Connect to the executor server."""
        import ssl as sslmod

        ssl_ctx = None
        if self.ws_url.startswith("wss://"):
            ssl_ctx = sslmod.create_default_context()
            if not self.ssl_verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = sslmod.CERT_NONE
            if self._cert_path and self._key_path:
                try:
                    ssl_ctx.load_cert_chain(certfile=self._cert_path, keyfile=self._key_path)
                except Exception as e:
                    logger.warning(f"Failed to load client cert/key: {e}")
            if self._cert_path:
                try:
                    ssl_ctx.load_verify_locations(cafile=self._cert_path)
                except Exception as e:
                    logger.warning(f"Failed to load CA cert: {e}")

        self.ws = await websockets.connect(self.ws_url, ssl=ssl_ctx)
        asyncio.create_task(self._handle_messages())
        await asyncio.sleep(0.2)
        await self.ws.ping()
        self.connected = True
        logger.info(f"Connected to {self.ws_url}")

    async def _handle_messages(self):
        """Handle incoming messages from server."""
        assert self.ws is not None
        try:
            async for message in self.ws:
                data = json.loads(message)

                if data["type"] == "result":
                    call_id = data["id"]
                    if call_id in self.pending_calls:
                        future = self.pending_calls.pop(call_id)
                        if data["success"]:
                            future.set_result(data.get("result"))
                        else:
                            future.set_exception(Exception(data.get("error", "Unknown error")))

                elif data["type"] == "event":
                    event_type = data.get("eventType")
                    event_data = data.get("data", {})
                    if event_type:
                        asyncio.create_task(self._dispatch_event(event_type, event_data))

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}")
            self.connected = False
        except Exception as e:
            logger.error(f"Message handler error: {e}")
            self.connected = False

    async def exec(self, code: str, timeout: float = 30.0) -> tp.Any:
        """Execute JavaScript in the remote browser.

        Args:
            code: JavaScript code to execute.
            timeout: Timeout in seconds.

        Returns:
            Result from JavaScript execution.
        """
        assert self.ws is not None, "Not connected"

        call_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.Future()
        self.pending_calls[call_id] = future

        await self.ws.send(json.dumps({
            "type": "exec",
            "id": call_id,
            "code": code,
            "timeout": timeout,
        }))

        try:
            return await asyncio.wait_for(future, timeout=timeout + 5)
        except asyncio.TimeoutError:
            self.pending_calls.pop(call_id, None)
            raise Exception(f"Execution timed out after {timeout}s")

    async def is_browser_connected(self) -> bool:
        """Check if browser extension is connected to the executor."""
        try:
            await self.exec("true", timeout=2.0)
            return True
        except Exception as e:
            if "No browser connected" in str(e) or "timed out" in str(e).lower():
                return False
            return True

    async def navigate(self, url: str):
        """Navigate browser to URL."""
        assert self.ws is not None, "Not connected"
        await self.ws.send(json.dumps({"type": "navigate", "url": url}))

    def on(self, event_type: str, handler: tp.Callable) -> None:
        """Register an event handler for browser events."""
        if event_type not in self.event_handlers:
            self.event_handlers[event_type] = []
        self.event_handlers[event_type].append(handler)

    def off(self, event_type: str, handler: tp.Optional[tp.Callable] = None) -> bool:
        """Unregister event handler(s)."""
        if event_type not in self.event_handlers:
            return False
        if handler is None:
            del self.event_handlers[event_type]
            return True
        try:
            self.event_handlers[event_type].remove(handler)
            if not self.event_handlers[event_type]:
                del self.event_handlers[event_type]
            return True
        except ValueError:
            return False

    async def _dispatch_event(self, event_type: str, event_data: dict) -> None:
        """Dispatch event to registered handlers."""
        for handler in self.event_handlers.get(event_type, []):
            try:
                result = handler(event_data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Event handler for '{event_type}' failed: {e}")

    async def add_init(self, code: str) -> None:
        """Register initialization code (runs on browser connect/reconnect)."""
        assert self.ws is not None, "Not connected"
        await self.ws.send(json.dumps({"type": "add_init", "code": code}))

    async def enable_console_tap(self) -> None:
        """Install console log interception in the browser.

        Forwards console.log/warn/error/etc. as 'console' events.
        Register a handler with: client.on('console', handler)
        """
        await self.add_init(_CONSOLE_TAP_JS)
        await self.exec(_CONSOLE_TAP_JS)

    async def close(self):
        """Close the connection."""
        if self.ws:
            await self.ws.close()
            self.connected = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
