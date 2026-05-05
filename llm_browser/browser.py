"""
Persistent browser session manager.

Uses a single Chromium profile stored at ~/.config/llm-browser/browser-profile/
so cookies and local storage survive between CLI invocations.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from llm_browser.config import NAVIGATION_TIMEOUT, PROFILE_DIR
from llm_browser.providers.base import BaseProvider
from playwright.async_api import (Browser, BrowserContext, Page, Playwright,
                                  async_playwright)


class BrowserSession:
    """Wraps a persistent Chromium context with a single active page."""

    def __init__(
        self,
        *,
        headless: bool = False,
        slow_mo: int = 0,
    ) -> None:
        self._headless = headless
        self._slow_mo = slow_mo
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        # persistent_context keeps the profile (cookies, localStorage, etc.)
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=self._headless,
            slow_mo=self._slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            # Spoof a normal Chrome UA so sites don't block automation
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        self._context.set_default_timeout(NAVIGATION_TIMEOUT)
        # Block media that isn't needed for text-based LLM UIs
        await self._context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        # Reuse existing tab or open a fresh one
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Session not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Session not started. Call start() first.")
        return self._context

    # ------------------------------------------------------------------
    # High-level query
    # ------------------------------------------------------------------

    async def query(
        self,
        provider: BaseProvider,
        query_text: str,
        *,
        force_dom: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Stream response chunks from the given provider."""
        async for chunk in provider.query(self.page, query_text, force_dom=force_dom):
            yield chunk

    async def ensure_logged_in(self, provider: BaseProvider) -> bool:
        """
        Returns True if already logged in.
        If not, opens the provider's login page and waits for the user.
        """
        logged_in = await provider.is_logged_in(self.page)
        if logged_in:
            return True

        # Open login page and wait until the user completes auth
        await self.page.goto(provider.meta.url, wait_until="domcontentloaded")
        return False


@asynccontextmanager
async def browser_session(
    *,
    headless: bool = False,
    slow_mo: int = 0,
) -> AsyncGenerator[BrowserSession, None]:
    """Async context manager that starts and stops a BrowserSession."""
    session = BrowserSession(headless=headless, slow_mo=slow_mo)
    await session.start()
    try:
        yield session
    finally:
        await session.stop()