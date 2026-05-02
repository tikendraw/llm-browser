"""Provider implementation for chatgpt.com."""

import asyncio
import json
from typing import AsyncGenerator

from playwright.async_api import Page, Response

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
        """
        ChatGPT streams from /backend-api/conversation via SSE.
        Each `data:` line contains JSON with message delta content.
        """
        collected: list[str] = []
        done_event = asyncio.Event()

        async def handle_response(response: Response) -> None:
            if "backend-api/conversation" not in response.url:
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
                        parts = (
                            data.get("message", {})
                            .get("content", {})
                            .get("parts", [])
                        )
                        if parts and isinstance(parts[0], str):
                            collected.append(parts[0])
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
            await asyncio.wait_for(done_event.wait(), timeout=RESPONSE_TIMEOUT / 1000)
            # parts are full snapshots; take the last one
            yield collected[-1] if collected else ""
        finally:
            page.remove_listener("response", handle_response)

    # ------------------------------------------------------------------
    # Mode 2: DOM extraction
    # ------------------------------------------------------------------

    async def dom_extract(self, page: Page) -> AsyncGenerator[str, None]:
        selectors = [
            '[data-message-author-role="assistant"]',
            '[class*="agent-turn"]',
            '.markdown',
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