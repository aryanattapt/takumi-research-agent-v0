from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from .url_policy import PublicSourceUrlPolicy, UnsafeSourceUrl

_BROWSER_SEMAPHORE = asyncio.Semaphore(1)


class _VisibleText(HTMLParser):
    HIDDEN = {"script", "style", "noscript", "svg", "canvas", "nav", "footer", "header", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.HIDDEN:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.HIDDEN and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if self.depth == 0 and text:
            self.parts.append(text)


def extract_visible_text(html: str, *, limit: int = 100_000) -> str:
    parser = _VisibleText()
    parser.feed(html)
    return "\n".join(parser.parts)[:limit]


class SourceRetriever:
    """HTTP-first retrieval with an isolated, read-only local Chromium fallback."""

    def __init__(
        self,
        policy: PublicSourceUrlPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_bytes: int = 3_000_000,
        timeout_seconds: float = 20,
        browser_enabled: bool = False,
    ):
        self.policy = policy
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.browser_enabled = browser_enabled
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=8),
            follow_redirects=False,
            headers={"User-Agent": "TakumiResearchAgent/0.2 (+source-research; contact: operator)"},
            transport=transport,
        )
        self._browser_semaphore = _BROWSER_SEMAPHORE

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch_http(self, url: str) -> dict[str, Any]:
        current = url
        for _ in range(4):
            await self.policy.validate(current)
            async with self.client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Source redirect omitted Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type and "xml" not in content_type and "text/" not in content_type:
                    raise ValueError("Source response is not readable text, HTML, or XML")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self.max_bytes:
                    raise ValueError("Source response exceeds configured size limit")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > self.max_bytes:
                        raise ValueError("Source response exceeds configured size limit")
                    chunks.extend(chunk)
                text = bytes(chunks).decode(response.encoding or "utf-8", errors="replace")
                if "html" in content_type:
                    return {"url": str(response.url), "title": "", "text": extract_visible_text(text), "retrieval": "http"}
                return {"url": str(response.url), "title": "", "text": text, "retrieval": "http"}
        raise ValueError("Source exceeded the redirect limit")

    async def fetch_browser(self, url: str) -> dict[str, Any]:
        if not self.browser_enabled:
            raise RuntimeError("Headless browser fallback is disabled")
        await self.policy.validate(url)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Install the browser extra and Chromium before enabling browser fallback") from exc

        async with self._browser_semaphore, async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(accept_downloads=False, java_script_enabled=True, service_workers="block")
            page = await context.new_page()

            async def guard(route):
                request_url = route.request.url
                resource_type = route.request.resource_type
                if resource_type in {"image", "media", "font", "websocket"}:
                    await route.abort()
                    return
                try:
                    await self.policy.validate(request_url)
                except UnsafeSourceUrl:
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", guard)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout_seconds * 1000))
                await page.wait_for_timeout(500)
                title = (await page.title())[:500]
                text = (await page.locator("body").inner_text(timeout=5_000))[:100_000]
                final_url = page.url
                await self.policy.validate(final_url)
                return {"url": final_url, "title": title, "text": text, "retrieval": "playwright"}
            finally:
                await context.close()
                await browser.close()

    async def read(self, url: str) -> dict[str, Any]:
        try:
            return await self.fetch_http(url)
        except (httpx.HTTPError, ValueError):
            if not self.browser_enabled:
                raise
            return await self.fetch_browser(url)
