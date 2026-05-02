"""Provider implementation for gemini.google.com."""

import asyncio
from typing import AsyncGenerator

from playwright.async_api import Page

from llm_browser.config import POLL_INTERVAL_MS, RESPONSE_TIMEOUT, STREAM_SETTLE_MS
from llm_browser.providers.base import BaseProvider, ProviderMeta


class GeminiProvider(BaseProvider):
    meta = ProviderMeta(
        name="gemini",
        display_name="Gemini (gemini.google.com)",
        url="https://gemini.google.com",
    )

    async def is_logged_in(self, page: Page) -> bool:
        await page.goto("https://gemini.google.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        return "accounts.google.com" not in page.url

    async def navigate_to_chat(self, page: Page) -> None:
        await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
        await page.wait_for_selector(
            'rich-textarea, textarea[placeholder], [contenteditable="true"]',
            timeout=15_000,
        )

    async def submit_query(self, page: Page, query: str) -> None:
        # Gemini uses a rich-textarea web component
        composer = page.locator("rich-textarea").first
        if await composer.count() == 0:
            composer = page.locator('[contenteditable="true"]').first
        await composer.click()
        await page.keyboard.type(query, delay=20)
        # Try send button first
        send_btn = page.locator('button[aria-label*="Send"], button[data-mat-icon-name="send"]').first
        if await send_btn.count() > 0 and await send_btn.is_enabled():
            await send_btn.click()
        else:
            await page.keyboard.press("Enter")

    # ------------------------------------------------------------------
    # Mode 1: network stream
    # Gemini uses a streaming JSON endpoint; network interception is
    # non-trivial (chunked transfer, nested JSON arrays).
    # We skip it and rely on DOM which works well here.
    # ------------------------------------------------------------------

    async def network_stream(self, page: Page, query: str) -> AsyncGenerator[str, None]:
        raise NotImplementedError("Gemini: using DOM mode")
        yield  # pragma: no cover

    # ------------------------------------------------------------------
    # Mode 2: DOM extraction
    # ------------------------------------------------------------------

    async def dom_extract(self, page: Page) -> AsyncGenerator[str, None]:
        selectors = [
            "model-response .response-content",
            "model-response",
            "[class*='response-container']",
            "message-content",
        ]
        combined = ", ".join(selectors)

        last_text = ""
        stable_count = 0
        stable_needed = STREAM_SETTLE_MS // POLL_INTERVAL_MS
        deadline = asyncio.get_event_loop().time() + RESPONSE_TIMEOUT / 1000

        await page.wait_for_selector(combined, timeout=RESPONSE_TIMEOUT)

        while asyncio.get_event_loop().time() < deadline:
            current_text = ""
            for sel in selectors:
                elements = await page.query_selector_all(sel)
                if elements:
                    current_text = await elements[-1].inner_text()
                    break

            if current_text and current_text == last_text:
                stable_count += 1
            else:
                stable_count = 0
                if current_text:
                    if current_text.startswith(last_text):
                        delta = current_text[len(last_text):]
                        if delta:
                            yield delta
                    else:
                        yield current_text
                    last_text = current_text

            if stable_count >= stable_needed:
                break

            await page.wait_for_timeout(POLL_INTERVAL_MS)