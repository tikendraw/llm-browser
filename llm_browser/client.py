"""
Client helpers for talking to the llm-browser daemon over a Unix socket.

The server speaks newline-delimited JSON. Each function opens a fresh
connection, sends one request line, and streams the response lines.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from llm_browser.config import SOCKET_PATH
from llm_browser.providers.base import LimitReachedError


async def is_server_running() -> bool:
    """Return True if the daemon is up and responding to pings."""
    if not SOCKET_PATH.exists():
        return False
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(SOCKET_PATH)),
            timeout=2.0,
        )
        writer.write(b'{"action":"ping"}\n')
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        return b"pong" in line
    except Exception:
        return False


async def ask(
    provider: str,
    query: str,
    *,
    force_dom: bool = False,
) -> AsyncGenerator[str, None]:
    """
    Stream response chunks from the daemon for a single provider.
    Raises LimitReachedError or RuntimeError on server-side errors.
    """
    reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    try:
        req = json.dumps({"action": "ask", "provider": provider, "query": query, "force_dom": force_dom})
        writer.write(req.encode() + b"\n")
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            msg = json.loads(line)
            if "error" in msg:
                if msg.get("type") == "LimitReachedError":
                    raise LimitReachedError(msg["error"])
                raise RuntimeError(msg["error"])
            if "chunk" in msg:
                yield msg["chunk"]
            if msg.get("done"):
                break
    finally:
        writer.close()


async def compare(
    providers: list[str],
    query: str,
    *,
    force_dom: bool = False,
) -> AsyncGenerator[tuple[str, str | None, int | None, str | None], None]:
    """
    Stream compare events from the daemon.

    Yields 4-tuples: (provider_name, chunk, elapsed_ms, error)
      - chunk only:        ("claude", "Hello", None, None)   — streaming token
      - done:              ("claude", None, 1234, None)       — provider finished
      - error:             ("claude", None, None, "msg")      — provider failed
      - all done sentinel: ("", None, None, None)             — all providers done
    """
    reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    try:
        req = json.dumps({
            "action": "compare",
            "providers": providers,
            "query": query,
            "force_dom": force_dom,
        })
        writer.write(req.encode() + b"\n")
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            msg = json.loads(line)

            if msg.get("all_done"):
                yield ("", None, None, None)
                break

            name = msg.get("provider", "")
            if "error" in msg:
                yield (name, None, None, msg["error"])
            elif "chunk" in msg:
                yield (name, msg["chunk"], None, None)
            elif msg.get("done"):
                yield (name, None, msg.get("elapsed_ms"), None)
    finally:
        writer.close()
