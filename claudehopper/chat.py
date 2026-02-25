"""Interactive chat with browser-based Claude.

Send messages, read responses, or watch for new messages in real-time.

Usage:
    python -m claudehopper.chat "message to send"
    python -m claudehopper.chat --read
    python -m claudehopper.chat --watch
"""

import asyncio
import json
import sys
import time

from .executor import ExecutorClient

SEND_JS_TEMPLATE = r"""
const editor = document.querySelector('div.ProseMirror[contenteditable="true"]')
    || document.querySelector('div[contenteditable="true"]');
if (!editor) return 'no editor found';
editor.focus();
await new Promise(r => setTimeout(r, 100));
document.execCommand('insertText', false, __MSG__);
await new Promise(r => setTimeout(r, 300));
const content = editor.textContent || '';
if (!content.trim()) return 'text did not land';
const form = editor.closest('form') || editor.closest('[class*="composer"]') || editor.parentElement?.parentElement?.parentElement;
if (form) {
    const btns = form.querySelectorAll('button');
    for (let i = btns.length - 1; i >= 0; i--) {
        if (!btns[i].disabled && btns[i].querySelector('svg')) {
            btns[i].click();
            return 'sent';
        }
    }
}
editor.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true}));
return 'sent-enter';
"""

READ_JS = r"""
const grids = document.querySelectorAll('div.grid.grid-cols-1');
const msgs = [];
for (const g of grids) {
    const text = g.textContent.trim();
    if (text.length < 2) continue;
    const isUser = g.className.includes('font-user-message') || g.className.includes('!font-user');
    msgs.push({ role: isUser ? 'H' : 'A', text: text.slice(0, 1000) });
}
return JSON.stringify(msgs.slice(-8));
"""


async def send_message(client: ExecutorClient, message: str) -> str:
    js = SEND_JS_TEMPLATE.replace("__MSG__", json.dumps(message))
    return await client.exec(js, timeout=10)


async def read_messages(client: ExecutorClient, last_n: int = 8) -> list[dict]:
    raw = await client.exec(READ_JS, timeout=10)
    if not raw:
        return []
    return json.loads(raw)


async def wait_for_response(client: ExecutorClient, known_count: int, timeout: float = 30) -> list[dict]:
    """Wait until a new assistant message appears."""
    start = time.time()
    while time.time() - start < timeout:
        msgs = await read_messages(client)
        if len(msgs) > known_count and msgs[-1]["role"] == "A":
            return msgs
        await asyncio.sleep(2)
    return await read_messages(client)


async def cmd_send(message: str):
    async with ExecutorClient(ssl_verify=False) as client:
        msgs_before = await read_messages(client)
        count_before = len(msgs_before)

        result = await send_message(client, message)
        if "sent" not in result:
            print(f"Failed to send: {result}")
            return

        print(f"[SENT]: {message}")
        print("Waiting for response...")

        msgs_after = await wait_for_response(client, count_before, timeout=45)

        for m in msgs_after[count_before:]:
            role = "BROWSER" if m["role"] == "A" else "HUMAN"
            print(f"\n[{role}]: {m['text']}")


async def cmd_read():
    async with ExecutorClient(ssl_verify=False) as client:
        msgs = await read_messages(client)
        for m in msgs:
            role = "BROWSER" if m["role"] == "A" else "HUMAN"
            print(f"\n[{role}]: {m['text'][:500]}")


async def cmd_watch():
    async with ExecutorClient(ssl_verify=False) as client:
        last_count = 0
        print("Watching for new messages... (Ctrl+C to stop)")
        try:
            while True:
                msgs = await read_messages(client)
                if len(msgs) > last_count:
                    for m in msgs[last_count:]:
                        role = "BROWSER" if m["role"] == "A" else "HUMAN"
                        ts = time.strftime("%H:%M:%S")
                        print(f"[{ts}] [{role}]: {m['text'][:300]}")
                    last_count = len(msgs)
                await asyncio.sleep(3)
        except KeyboardInterrupt:
            pass


async def main():
    args = sys.argv[1:]
    if not args or args[0] == "--help":
        print(__doc__)
        return
    if args[0] == "--read":
        await cmd_read()
    elif args[0] == "--watch":
        await cmd_watch()
    else:
        message = " ".join(args)
        await cmd_send(message)


if __name__ == "__main__":
    asyncio.run(main())
