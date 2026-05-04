"""Provider implementation for claude.ai."""

import asyncio
import json
from typing import AsyncGenerator

from playwright.async_api import Page, Response

from llm_browser.config import POLL_INTERVAL_MS, RESPONSE_TIMEOUT, STREAM_SETTLE_MS
from llm_browser.providers.base import BaseProvider, LimitReachedError, ProviderMeta

_RATE_LIMIT_SEL = 'text="Upgrade to keep chatting"'
_RATE_LIMIT_MSG = (
    "Claude message limit reached. Check the banner for the reset time."
)


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
        composer = page.locator('div[contenteditable="true"]').first
        await composer.click()
        await composer.fill(query)
        await page.wait_for_timeout(150)
        await page.keyboard.press("Enter")

    async def _check_rate_limit(self, page: Page) -> None:
        el = await page.query_selector(_RATE_LIMIT_SEL)
        if el:
            raise LimitReachedError(_RATE_LIMIT_MSG)

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
                if collected:
                    done_event.set()
            except Exception:
                pass

        page.on("response", handle_response)
        try:
            await self.navigate_to_chat(page)
            await self.submit_query(page, query)
            await page.wait_for_timeout(1_500)
            await self._check_rate_limit(page)
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
        Claude marks its response container with data-is-streaming="true|false";
        the prose lives inside .font-claude-response > .standard-markdown.
        """
        selectors = [
            '.font-claude-response .standard-markdown',
            '.font-claude-response',
        ]

        last_text = ""
        stable_count = 0
        stable_needed = STREAM_SETTLE_MS // POLL_INTERVAL_MS
        deadline = asyncio.get_event_loop().time() + RESPONSE_TIMEOUT / 1000

        await page.wait_for_selector('.font-claude-response', timeout=RESPONSE_TIMEOUT)

        while asyncio.get_event_loop().time() < deadline:
            await self._check_rate_limit(page)
            current_text = ""
            for sel in selectors:
                elements = await page.query_selector_all(sel)
                if elements:
                    # Use the LAST element (most recent assistant turn)
                    current_text = (await elements[-1].inner_text()).strip()
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

            if stable_count >= stable_needed:
                # action-bar-copy only renders after Claude finishes the turn
                done = await page.query_selector('[data-testid="action-bar-copy"]')
                if done:
                    break
                stable_count = 0  # still generating

            await page.wait_for_timeout(POLL_INTERVAL_MS)