"""Abstract base class for all LLM browser providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator

from playwright.async_api import Page


class LimitReachedError(RuntimeError):
    """Raised when a provider's usage/rate limit is hit."""


@dataclass(frozen=True)
class ProviderMeta:
    name: str
    display_name: str
    url: str


class BaseProvider(ABC):
    """
    Each provider must implement two extraction strategies:
      1. network_stream  – intercept the SSE / XHR response (fast, precise)
      2. dom_extract     – poll the DOM until the answer stabilises (robust fallback)

    `query` tries network first; falls back to DOM on any exception.
    """

    meta: ProviderMeta  # class-level attribute, set by subclasses

    # ------------------------------------------------------------------
    # Mandatory overrides
    # ------------------------------------------------------------------

    @abstractmethod
    async def is_logged_in(self, page: Page) -> bool:
        """Return True if the current page indicates an active session."""

    @abstractmethod
    async def navigate_to_chat(self, page: Page) -> None:
        """Navigate to the provider's new-chat URL and wait until ready."""

    @abstractmethod
    async def submit_query(self, page: Page, query: str) -> None:
        """Type *query* into the input box and send it."""

    @abstractmethod
    async def network_stream(self, page: Page, query: str) -> AsyncGenerator[str, None]:
        """
        Yield response tokens by intercepting network traffic.
        Should raise NotImplementedError if the provider doesn't support this mode.
        """
        # make the type-checker happy; body replaced by subclass
        raise NotImplementedError
        yield  # pragma: no cover

    @abstractmethod
    async def dom_extract(self, page: Page) -> AsyncGenerator[str, None]:
        """
        Yield the full response text (possibly in chunks as the DOM updates)
        by polling DOM selectors.
        """
        raise NotImplementedError
        yield  # pragma: no cover

    # ------------------------------------------------------------------
    # Public interface used by the browser runner
    # ------------------------------------------------------------------

    async def query(
        self,
        page: Page,
        query: str,
        *,
        force_dom: bool = False,
    ) -> AsyncGenerator[str, None]:
        """
        Try network interception; fall back to DOM extraction on failure.
        Yields text chunks as they arrive.
        """
        if not force_dom:
            try:
                async for chunk in self._network_query(page, query):
                    yield chunk
                return
            except LimitReachedError:
                raise
            except Exception:
                pass  # fall through to DOM mode

        await self.navigate_to_chat(page)
        await self.submit_query(page, query)
        async for chunk in self.dom_extract(page):
            yield chunk

    async def _network_query(
        self,
        page: Page,
        query: str,
    ) -> AsyncGenerator[str, None]:
        await self.navigate_to_chat(page)
        async for chunk in self.network_stream(page, query):
            yield chunk