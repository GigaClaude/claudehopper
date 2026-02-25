"""Meridian command relay — executes JSON commands from browser Claude's chat.

Reads messages via the bridge, parses meridian_cmd JSON blocks,
executes them against the Meridian API, and sends results back.

Usage:
    python -m claudehopper.relay              # scan latest messages
    python -m claudehopper.relay --watch      # continuous watch mode
"""

import asyncio
import json
import re
import sys

from .comms import BrowserComms

# Regex to find meridian_cmd JSON blocks in message text
CMD_PATTERN = re.compile(
    r'\{[^{}]*"meridian_cmd"\s*:\s*"[^"]+?"[^{}]*\}',
    re.DOTALL,
)


async def execute_command(cmd: dict) -> str:
    """Execute a meridian command and return the result as a string.

    Requires the meridian package to be installed (pip install meridian).
    """
    from meridian.storage import StorageLayer
    from meridian.gateway import MemoryGateway
    from meridian.workers import WorkerPool

    action = cmd.get("meridian_cmd")
    storage = StorageLayer()
    await storage.init_db()
    gateway = MemoryGateway()

    try:
        if action == "recall":
            query = cmd.get("query", "")
            scope = cmd.get("scope", "all")
            max_tokens = cmd.get("max_tokens", 400)
            result = await gateway.recall(query, scope, max_tokens, storage)
            return f"RECALL RESULT:\n{result['results'][:2000]}"

        elif action == "remember":
            workers = WorkerPool()
            result = await storage.remember(
                {
                    "content": cmd.get("content", ""),
                    "type": cmd.get("type", "note"),
                    "tags": cmd.get("tags", []),
                    "importance": cmd.get("importance", 3),
                    "source": cmd.get("source", "browser"),
                    "related_to": cmd.get("related_to", []),
                },
                workers,
            )
            await workers.close()
            if result.get("stored"):
                return f"STORED: {result['id']}"
            elif result.get("rejected"):
                return f"REJECTED (quality gate): {result.get('reasons', [])}"
            else:
                return f"STORE FAILED: {json.dumps(result)}"

        elif action == "briefing":
            project_id = cmd.get("project_id", "default")
            briefing = await gateway.assemble_briefing(project_id, storage)
            return f"BRIEFING:\n{briefing[:3000]}"

        elif action == "forget":
            mem_id = cmd.get("id", "")
            reason = cmd.get("reason", "")
            result = await storage.forget({"id": mem_id, "reason": reason})
            return f"FORGOT: {json.dumps(result)}"

        elif action == "checkpoint":
            result = await storage.checkpoint(
                {
                    "task_state": cmd.get("task_state", ""),
                    "decisions": cmd.get("decisions", []),
                    "warnings": cmd.get("warnings", []),
                    "next_steps": cmd.get("next_steps", []),
                    "working_set": cmd.get("working_set", {}),
                }
            )
            return f"CHECKPOINT: {json.dumps(result)}"

        else:
            return f"UNKNOWN COMMAND: {action}"

    finally:
        await storage.close()
        await gateway.close()


def extract_commands(text: str) -> list[dict]:
    """Extract meridian_cmd JSON blocks from message text."""
    commands = []
    for match in CMD_PATTERN.finditer(text):
        try:
            obj = json.loads(match.group())
            if "meridian_cmd" in obj:
                commands.append(obj)
        except json.JSONDecodeError:
            continue
    return commands


async def scan_and_execute():
    """Scan latest messages for commands and execute them."""
    async with BrowserComms() as comms:
        messages = await comms.read_all()
        if not messages:
            print("No messages found.")
            return

        for msg in reversed(messages[-5:]):
            if msg["role"] != "assistant":
                continue

            commands = extract_commands(msg["text"])
            if not commands:
                continue

            for cmd in commands:
                action = cmd.get("meridian_cmd")
                print(f"\n[CMD] {action}: {json.dumps(cmd)[:100]}...")

                result = await execute_command(cmd)
                print(f"[RESULT] {result[:200]}")

                reply = f"[RELAY] {action} → {result[:1500]}"
                await comms.send(reply)
                print("[SENT] Reply sent")


async def watch_mode(interval: float = 10.0):
    """Continuously watch for new commands."""
    print(f"Watching for meridian_cmd blocks (polling every {interval}s)...")
    seen_hashes: set = set()

    while True:
        try:
            async with BrowserComms() as comms:
                messages = await comms.read_all()
                if not messages:
                    await asyncio.sleep(interval)
                    continue

                for msg in messages:
                    if msg["role"] != "assistant":
                        continue

                    msg_hash = hash(msg["text"][:200])
                    if msg_hash in seen_hashes:
                        continue

                    commands = extract_commands(msg["text"])
                    if not commands:
                        seen_hashes.add(msg_hash)
                        continue

                    seen_hashes.add(msg_hash)

                    for cmd in commands:
                        action = cmd.get("meridian_cmd")
                        print(f"\n[CMD] {action}")
                        result = await execute_command(cmd)
                        print(f"  RESULT: {result[:200]}")

                        reply = f"[RELAY] {action} → {result[:1500]}"
                        await comms.send(reply)

        except Exception as e:
            print(f"Error: {e}")

        await asyncio.sleep(interval)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        asyncio.run(watch_mode())
    else:
        asyncio.run(scan_and_execute())
