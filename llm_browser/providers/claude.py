"""Provider implementation for claude.ai."""

import asyncio
import json
from typing import AsyncGenerator

from playwright.async_api import Page, Response

from llm_browser.config import POLL_INTERVAL_MS, RESPONSE_TIMEOUT, STREAM_SETTLE_MS
from llm_browser.providers.base import BaseProvider, ProviderMeta


class ClaudeProvider(BaseProvider):
    meta = ProviderMeta(
        name="claude",
        display_name="Claude (claude.ai)",
        url="https://claude.ai",
    )

    # ------------------------------------------------------------------
    # Session check
    # ------------------------------------------------------------------

    async def is_logged_in(self, page: Page) -> bool:
        await page.goto("https://claude.ai", wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        return "login" not in page.url and "sign" not in page.url.lower()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate_to_chat(self, page: Page) -> None:
        await page.goto("https://claude.ai/new", wait_until="domcontentloaded")
        # Wait for the composer to be present
        await page.wait_for_selector(
            'div[contenteditable="true"], textarea[placeholder]',
            timeout=15_000,
        )

    # ------------------------------------------------------------------
    # Input submission
    # ------------------------------------------------------------------

    async def submit_query(self, page: Page, query: str) -> None:
        # Claude uses a contenteditable div
        composer = page.locator('div[contenteditable="true"]').first
        await composer.click()
        await composer.fill("")
        await page.keyboard.type(query, delay=20)
        await page.keyboard.press("Enter")

    # ------------------------------------------------------------------
    # Mode 1: network interception (SSE stream)
    # ------------------------------------------------------------------

    async def network_stream(self, page: Page, query: str) -> AsyncGenerator[str, None]:
        """
        Claude streams completions via SSE from /api/organizations/.../completion
        We capture each event's `completion` field.
        """
        collected: list[str] = []
        done_event = asyncio.Event()

        async def handle_response(response: Response) -> None:
            if "/completion" not in response.url:
                return
            try:
                body = await response.text()
                for line in body.splitlines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw in ("", "[DONE]"):
                        continue
                    try:
                        data = json.loads(raw)
                        # streaming delta
                        delta = (
                            data.get("delta", {}).get("text")
                            or data.get("completion")
                            or ""
                        )
                        if delta:
                            collected.append(delta)
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass
            finally:
                done_event.set()

        page.on("response", handle_response)
        try:
            await self.navigate_to_chat(page)
            await self.submit_query(page, query)
            # wait for the response handler to fire
            await asyncio.wait_for(
                done_event.wait(),
                timeout=RESPONSE_TIMEOUT / 1000,
            )
            yield "".join(collected)
        finally:
            page.remove_listener("response", handle_response)

    # ------------------------------------------------------------------
    # Mode 2: DOM extraction (fallback)
    # ------------------------------------------------------------------

    async def dom_extract(self, page: Page) -> AsyncGenerator[str, None]:
        """
        Poll the last assistant message block until text stabilises.
        Claude renders responses inside elements with data-testid="assistant-message"
        or a class containing 'font-claude-message'.
        """
        # Selectors to try in order
        selectors = [
            '[data-testid="assistant-message"]',
            '.font-claude-message',
            '[class*="assistant-message"]',
        ]

        last_text = ""
        stable_count = 0
        stable_needed = STREAM_SETTLE_MS // POLL_INTERVAL_MS
        deadline = asyncio.get_event_loop().time() + RESPONSE_TIMEOUT / 1000

        # Wait for at least one response block to appear
        combined = ", ".join(selectors)
        await page.wait_for_selector(combined, timeout=RESPONSE_TIMEOUT)

        while asyncio.get_event_loop().time() < deadline:
            current_text = ""
            for sel in selectors:
                elements = await page.query_selector_all(sel)
                if elements:
                    # Use the LAST element (most recent assistant turn)
                    current_text = await elements[-1].inner_text()
                    break

            if current_text and current_text == last_text:
                stable_count += 1
            else:
                stable_count = 0
                if current_text:
                    # Yield only the new delta
                    if current_text.startswith(last_text):
                        delta = current_text[len(last_text):]
                        if delta:
                            yield delta
                    else:
                        yield current_text  # full replace (edge case)
                    last_text = current_text

            if stable_count >= stable_needed:
                break

            await page.wait_for_timeout(POLL_INTERVAL_MS)