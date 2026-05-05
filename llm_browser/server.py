"""
Persistent browser daemon.

Keeps Chromium open between CLI invocations so each `llm ask` avoids the
cold-start cost of launching Playwright.

Start foreground:  llm serve
Start background:  llm daemon start
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time

from llm_browser.browser import BrowserSession
from llm_browser.config import PID_PATH, SOCKET_PATH
from llm_browser.db import save_chat
from llm_browser.providers import get_provider, list_providers
from llm_browser.providers.base import LimitReachedError


class LLMServer:
    def __init__(self, *, headless: bool = False, slow_mo: int = 0) -> None:
        self._headless = headless
        self._slow_mo = slow_mo
        self._session: BrowserSession | None = None
        self._server: asyncio.Server | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._session = BrowserSession(headless=self._headless, slow_mo=self._slow_mo)
        await self._session.start()

        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(SOCKET_PATH),
        )

        PID_PATH.write_text(str(os.getpid()))
        print(f"llm-browser daemon listening on {SOCKET_PATH}", flush=True)

        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        if self._session:
            await self._session.stop()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        if PID_PATH.exists():
            PID_PATH.unlink()

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        lock = asyncio.Lock()
        try:
            line = await reader.readline()
            if not line:
                return
            request = json.loads(line)
            action = request.get("action")

            if action == "ping":
                await _write(writer, lock, {"pong": True})
            elif action == "ask":
                await self._handle_ask(request, writer, lock)
            elif action == "compare":
                await self._handle_compare(request, writer, lock)
            else:
                await _write(writer, lock, {"error": f"unknown action: {action}"})
        except Exception as exc:
            try:
                await _write(writer, lock, {"error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass

    async def _handle_ask(
        self,
        req: dict,
        writer: asyncio.StreamWriter,
        lock: asyncio.Lock,
    ) -> None:
        assert self._session is not None
        provider = get_provider(req["provider"])
        query = req["query"]
        force_dom = req.get("force_dom", False)
        start = time.monotonic()
        full_response = ""

        page = await self._session.context.new_page()
        try:
            if not await provider.is_logged_in(page):
                await _write(writer, lock, {
                    "error": f"Not logged in — run: llm login {req['provider']}",
                    "type": "NotLoggedIn",
                })
                return

            async for chunk in provider.query(page, query, force_dom=force_dom):
                full_response += chunk
                await _write(writer, lock, {"chunk": chunk})
        except LimitReachedError as exc:
            await _write(writer, lock, {"error": str(exc), "type": "LimitReachedError"})
            return
        finally:
            await page.close()

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if full_response:
            save_chat(provider.meta.name, query, full_response, elapsed_ms)
        await _write(writer, lock, {"done": True, "elapsed_ms": elapsed_ms})

    async def _handle_compare(
        self,
        req: dict,
        writer: asyncio.StreamWriter,
        lock: asyncio.Lock,
    ) -> None:
        assert self._session is not None
        provider_names = req.get("providers") or [p.meta.name for p in list_providers()]
        query = req["query"]
        force_dom = req.get("force_dom", False)
        providers = [get_provider(name) for name in provider_names]

        async def run_one(provider) -> None:
            start = time.monotonic()
            text = ""
            page = await self._session.context.new_page()
            try:
                if not await provider.is_logged_in(page):
                    await _write(writer, lock, {
                        "provider": provider.meta.name,
                        "error": f"Not logged in — run: llm login {provider.meta.name}",
                    })
                    return

                async for chunk in provider.query(page, query, force_dom=force_dom):
                    text += chunk
                    await _write(writer, lock, {"provider": provider.meta.name, "chunk": chunk})
            except Exception as exc:
                await _write(writer, lock, {"provider": provider.meta.name, "error": str(exc)})
                return
            finally:
                await page.close()

            elapsed_ms = int((time.monotonic() - start) * 1000)
            if text:
                save_chat(provider.meta.name, query, text, elapsed_ms)
            await _write(writer, lock, {
                "provider": provider.meta.name,
                "done": True,
                "elapsed_ms": elapsed_ms,
            })

        await asyncio.gather(*[run_one(p) for p in providers])
        await _write(writer, lock, {"all_done": True})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _write(writer: asyncio.StreamWriter, lock: asyncio.Lock, obj: dict) -> None:
    async with lock:
        writer.write(json.dumps(obj).encode() + b"\n")
        await writer.drain()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="llm-browser daemon")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--slow", action="store_true")
    args = parser.parse_args()

    server = LLMServer(headless=args.headless, slow_mo=50 if args.slow else 0)

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        def _shutdown() -> None:
            loop.create_task(server.stop())

        loop.add_signal_handler(signal.SIGTERM, _shutdown)
        loop.add_signal_handler(signal.SIGINT, _shutdown)

        try:
            await server.start()
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
