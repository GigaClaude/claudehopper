# ClaudeHopper

**Make two Claudes talk to each other.**

ClaudeHopper bridges CLI-based Claude (Claude Code) and browser-based Claude (claude.ai) through a WebSocket executor. Send messages, read responses, relay memory commands — all programmatically.

## How It Works

```
CLI Claude ──→ ClaudeHopper ──→ Executor Server ──→ Browser Extension ──→ claude.ai
                                                                          ↓
CLI Claude ←── ClaudeHopper ←── Executor Server ←── Browser Extension ←── claude.ai
```

1. **Executor Server** runs on your machine with a browser extension connected
2. **ClaudeHopper** sends JavaScript to the executor, which runs it in the browser tab
3. Messages are injected into claude.ai's ProseMirror editor and sent via DOM manipulation
4. Responses are read back by querying the chat DOM

## Install

```bash
pip install -e .
```

## Configuration

Set environment variables for your executor:

```bash
export EXECUTOR_URL=https://localhost:18111   # Executor server URL
export EXECUTOR_CERT_PATH=./certs/cert.pem    # Client certificate (optional)
export EXECUTOR_KEY_PATH=./certs/key.pem      # Client key (optional)
```

## Usage

### Chat — send a message and get a response

```bash
python -m claudehopper.chat "What are you working on?"
python -m claudehopper.chat --read     # read latest messages
python -m claudehopper.chat --watch    # real-time message watcher
```

### Comms — reliable send/receive with dedup

```python
from claudehopper.comms import BrowserComms

async with BrowserComms() as comms:
    await comms.send("Hello from CLI Claude!")
    response = await comms.wait_for_response(timeout=60)
    print(response)
```

```bash
python -m claudehopper.comms send "message here"
python -m claudehopper.comms read
python -m claudehopper.comms new    # only unread messages
```

### Relay — bridge Meridian memory commands

If browser Claude emits `meridian_cmd` JSON blocks in its messages, the relay picks them up and executes them against a local [Meridian](https://github.com/GigaClaude/meridian) instance.

```bash
python -m claudehopper.relay           # one-shot scan
python -m claudehopper.relay --watch   # continuous polling
```

Requires `pip install meridian` separately.

## Executor

ClaudeHopper needs an executor server with a browser extension. The executor:

- Runs a WebSocket server that accepts JS execution requests
- Has a browser extension that connects to it and runs JS in the active tab
- Returns results back through the WebSocket

The executor is part of the [SNAPP](https://github.com/apresence/snapp) project. See its docs for setup.

## Architecture

```
claudehopper/
├── executor/         # WebSocket client for browser automation
│   ├── client.py     # ExecutorClient — exec JS, navigate, events
│   └── config.py     # Connection config from env vars
├── comms.py          # BrowserComms — reliable send/receive with dedup
├── chat.py           # Interactive chat CLI
└── relay.py          # Meridian command relay
```

Built by [GigaClaude](https://github.com/GigaClaude) with [apresence](https://github.com/apresence).
