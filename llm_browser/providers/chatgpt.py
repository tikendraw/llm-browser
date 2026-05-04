"""Provider implementation for chatgpt.com."""

import asyncio
from typing import AsyncGenerator

from playwright.async_api import Page

from llm_browser.config import POLL_INTERVAL_MS, RESPONSE_TIMEOUT, STREAM_SETTLE_MS
from llm_browser.providers.base import BaseProvider, ProviderMeta


class ChatGPTProvider(BaseProvider):
    meta = ProviderMeta(
        name="chatgpt",
        display_name="ChatGPT (chatgpt.com)",
        url="https://chatgpt.com",
    )

    async def is_logged_in(self, page: Page) -> bool:
        await page.goto("https://chatgpt.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        return "auth" not in page.url and "login" not in page.url.lower()

    async def navigate_to_chat(self, page: Page) -> None:
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        await page.wait_for_selector(
            '#prompt-textarea, textarea[placeholder]',
            timeout=15_000,
        )

    async def submit_query(self, page: Page, query: str) -> None:
        textarea = page.locator("#prompt-textarea").first
        await textarea.click()
        await textarea.fill(query)
        # Submit button or Enter
        send_btn = page.locator('[data-testid="send-button"]')
        if await send_btn.count() > 0:
            await send_btn.click()
        else:
            await page.keyboard.press("Enter")

    # ------------------------------------------------------------------
    # Mode 1: network interception
    # ------------------------------------------------------------------

    async def network_stream(self, page: Page, query: str) -> AsyncGenerator[str, None]:
        # ChatGPT now streams via WebSocket; HTTP response interception doesn't
        # capture the tokens.  DOM extraction is reliable and avoids the
        # double-submission that a failed network attempt causes.
        raise NotImplementedError("ChatGPT: using DOM mode")
        yield  # pragma: no cover

    # ------------------------------------------------------------------
    # Mode 2: DOM extraction
    # ------------------------------------------------------------------

    async def dom_extract(self, page: Page) -> AsyncGenerator[str, None]:
        # Scope everything to the last assistant turn so we never confuse
        # action buttons or text from a previous turn with the current one.
        # ChatGPT numbers turns with data-testid="conversation-turn-N" and
        # marks role with data-turn="assistant".
        text_selectors = [".markdown.prose", ".markdown"]

        last_text = ""
        stable_count = 0
        stable_needed = STREAM_SETTLE_MS // POLL_INTERVAL_MS
        deadline = asyncio.get_event_loop().time() + RESPONSE_TIMEOUT / 1000

        await page.wait_for_selector('[data-turn="assistant"]', timeout=RESPONSE_TIMEOUT)

        while asyncio.get_event_loop().time() < deadline:
            # Always re-query the LAST assistant turn (new turns may appear)
            turns = await page.query_selector_all('[data-turn="assistant"]')
            last_turn = turns[-1] if turns else None

            current_text = ""
            if last_turn:
                for sel in text_selectors:
                    els = await last_turn.query_selector_all(sel)
                    if els:
                        current_text = (await els[-1].inner_text()).strip()
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
                    last_text = current_text

            if stable_count >= stable_needed and last_turn:
                # ChatGPT sets data-stream-active on the scroll root while
                # generating and removes it when done. The copy button is always
                # in the DOM (just CSS-hidden), so it's not a reliable signal.
                is_streaming = await page.evaluate(
                    "() => document.querySelector('[data-stream-active]') !== null"
                )
                if not is_streaming:
                    break
                stable_count = 0  # still generating

            await page.wait_for_timeout(POLL_INTERVAL_MS)