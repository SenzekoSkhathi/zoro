"""Zoro's isolated virtual browser engine — powered by Playwright Chromium.

Provides Zoro with her own dedicated browser that runs silently in the background
without hijacking the user's cursor, keyboard, or desktop focus.

Key capabilities:
  * Programmatic web navigation, form filling, clicking, and scrolling.
  * Real-time visual snapshots streamed to Zoro's Web HUD (ZoroDisplay.tsx).
  * Direct interaction with local dev servers (e.g., localhost:3000 for Lumen AI).
  * DOM content extraction for LLM reading and analysis.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


class ZoroBrowser:
    """Manages an isolated Playwright Chromium instance."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._last_screenshot: str = ""
        self._last_action: str = "idle"

    async def _ensure_page(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page

        if not self._playwright:
            self._playwright = await async_playwright().start()

        headed = os.environ.get("ZORO_BROWSER_HEADED", "0") == "1"
        if not self._browser or not self._browser.is_connected():
            self._browser = await self._playwright.chromium.launch(
                headless=not headed,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36 ZoroAssistant/2.0"
            ),
        )
        self._page = await self._context.new_page()
        return self._page

    def _normalize_url(self, url: str) -> str:
        url = url.strip()
        if not url:
            return "about:blank"
        if url.startswith(("http://", "https://", "about:", "file://")):
            return url
        # Handle localhost ports
        if re.match(r"^localhost(:\d+)?(/.*)?$", url, re.IGNORECASE):
            return f"http://{url}"
        # Domain name default
        return f"https://{url}"

    async def navigate(self, url: str) -> dict[str, Any]:
        """Navigate to a URL and return page state."""
        async with self._lock:
            page = await self._ensure_page()
            target = self._normalize_url(url)
            self._last_action = f"Navigating to {target}"
            try:
                await page.goto(target, timeout=25000, wait_until="domcontentloaded")
                # Wait briefly for client-side hydration
                await asyncio.sleep(0.5)
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"Navigation failed: {e}",
                    "url": target,
                    "title": "Error",
                    "screenshot": "",
                }

            title = await page.title()
            shot = await self._capture_screenshot()
            return {
                "ok": True,
                "url": page.url,
                "title": title or target,
                "screenshot": shot,
                "action": f"Navigated to {page.url}",
            }

    async def click(self, target: str) -> dict[str, Any]:
        """Click a button, link, or element matching text or selector."""
        async with self._lock:
            page = await self._ensure_page()
            self._last_action = f"Clicking {target}"
            try:
                # Try standard selector or text match
                matched = False
                for sel in [target, f"text={target}", f"button:has-text('{target}')", f"a:has-text('{target}')"]:
                    try:
                        locator = page.locator(sel).first
                        if await locator.is_visible(timeout=1500):
                            await locator.click(timeout=3000)
                            matched = True
                            break
                    except Exception:
                        continue

                if not matched:
                    # Generic click attempt
                    await page.click(target, timeout=3000)

                await asyncio.sleep(0.5)
            except Exception as e:
                return {"ok": False, "error": f"Could not click '{target}': {e}"}

            title = await page.title()
            shot = await self._capture_screenshot()
            return {
                "ok": True,
                "url": page.url,
                "title": title,
                "screenshot": shot,
                "action": f"Clicked '{target}'",
            }

    async def type_text(self, selector: str, text: str) -> dict[str, Any]:
        """Fill or type into an input field."""
        async with self._lock:
            page = await self._ensure_page()
            self._last_action = f"Typing into {selector}"
            try:
                await page.fill(selector, text, timeout=4000)
                await asyncio.sleep(0.2)
            except Exception as e:
                return {"ok": False, "error": f"Could not type into '{selector}': {e}"}

            shot = await self._capture_screenshot()
            return {
                "ok": True,
                "url": page.url,
                "title": await page.title(),
                "screenshot": shot,
                "action": f"Typed into '{selector}'",
            }

    async def scroll(self, direction: str = "down", amount: int = 500) -> dict[str, Any]:
        """Scroll the active page up or down."""
        async with self._lock:
            page = await self._ensure_page()
            delta = amount if direction.lower() == "down" else -amount
            self._last_action = f"Scrolling {direction}"
            try:
                await page.evaluate(f"window.scrollBy({{ top: {delta}, behavior: 'smooth' }});")
                await asyncio.sleep(0.4)
            except Exception as e:
                return {"ok": False, "error": f"Scroll failed: {e}"}

            shot = await self._capture_screenshot()
            return {
                "ok": True,
                "url": page.url,
                "title": await page.title(),
                "screenshot": shot,
                "action": f"Scrolled {direction}",
            }

    async def read_content(self, max_chars: int = 4000) -> dict[str, Any]:
        """Extract clean text content from the current page."""
        async with self._lock:
            page = await self._ensure_page()
            try:
                # Extract clean text from body
                text = await page.evaluate(
                    """() => {
                        const clone = document.body.cloneNode(true);
                        const remove = clone.querySelectorAll('script, style, noscript, svg, nav, footer');
                        remove.forEach(el => el.remove());
                        return clone.innerText || '';
                    }"""
                )
                text = re.sub(r"\n\s*\n", "\n\n", text.strip())
                trimmed = text[:max_chars] if len(text) > max_chars else text
                return {
                    "ok": True,
                    "url": page.url,
                    "title": await page.title(),
                    "content": trimmed,
                    "length": len(text),
                }
            except Exception as e:
                return {"ok": False, "error": f"Could not read page: {e}"}

    async def _capture_screenshot(self) -> str:
        """Capture JPEG screenshot and return as base64 string."""
        if not self._page or self._page.is_closed():
            return ""
        try:
            bytes_data = await self._page.screenshot(type="jpeg", quality=60)
            self._last_screenshot = base64.b64encode(bytes_data).decode("ascii")
            return self._last_screenshot
        except Exception:
            return ""

    async def get_state(self) -> dict[str, Any]:
        """Return current browser state."""
        if not self._page or self._page.is_closed():
            return {"active": False, "url": "", "title": "Closed", "screenshot": ""}
        try:
            return {
                "active": True,
                "url": self._page.url,
                "title": await self._page.title(),
                "screenshot": self._last_screenshot,
                "action": self._last_action,
            }
        except Exception:
            return {"active": False, "url": "", "title": "", "screenshot": ""}

    async def close(self) -> None:
        """Close browser instance."""
        async with self._lock:
            if self._page and not self._page.is_closed():
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser and self._browser.is_connected():
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._last_action = "closed"


# --- Global Singleton -------------------------------------------------------

_GLOBAL_BROWSER: Optional[ZoroBrowser] = None


def get_browser() -> ZoroBrowser:
    global _GLOBAL_BROWSER
    if _GLOBAL_BROWSER is None:
        _GLOBAL_BROWSER = ZoroBrowser()
    return _GLOBAL_BROWSER
