"""Zoro's isolated virtual browser engine — powered by Playwright Chromium.

Provides Zoro with her own dedicated browser that runs silently in the background
without hijacking the user's cursor, keyboard, or desktop focus.

Supports Human-In-The-Loop (HITL) takeover:
  * Persistent session profiles (cookies, logins, 2FA, localStorage saved across runs).
  * Seamless handoff to visible (headed) desktop window for user credentials/2FA/CAPTCHA.
  * Instant resumption back to autonomous headless mode once logged in.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from importlib import import_module
from pathlib import Path
from typing import Any, Optional

# Playwright is optional until the browser is actually used.  Loading it
# dynamically keeps importing the rest of Zoro usable when the dependency is
# not installed (and avoids a hard import-time failure).
try:
    _playwright_api = import_module("playwright.async_api")
except ImportError:
    _playwright_api = None

BrowserContext = Any
Page = Any
Playwright = Any

ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = ROOT / "data" / "browser_profile"


class ZoroBrowser:
    """Manages a persistent Playwright Chromium instance with HITL handoff."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._is_headed: bool = False
        self._waiting_for_user: bool = False
        self._current_url: str = "about:blank"
        self._last_screenshot: str = ""
        self._last_action: str = "idle"

    async def _ensure_page(self, headless: bool = True) -> Page:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        # If already running with matching headless state, reuse page
        if self._context and self._page and not self._page.is_closed():
            if self._is_headed == (not headless):
                return self._page
            # Need to switch headless mode: save URL and close existing context
            self._current_url = self._page.url
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None

        if not self._playwright:
            if _playwright_api is None:
                raise RuntimeError(
                    "Playwright is required for browser operations. "
                    "Install it with: pip install playwright"
                )
            self._playwright = await _playwright_api.async_playwright().start()

        env_headed = os.environ.get("ZORO_BROWSER_HEADED", "0") == "1"
        effective_headless = False if (not headless or env_headed) else True
        self._is_headed = not effective_headless

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=effective_headless,
            viewport={"width": 1280, "height": 800},
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36 ZoroAssistant/2.0"
            ),
        )

        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        return self._page

    def _normalize_url(self, url: str) -> str:
        url = url.strip()
        if not url:
            return "about:blank"
        if url.startswith(("http://", "https://", "about:", "file://")):
            return url
        if re.match(r"^localhost(:\d+)?(/.*)?$", url, re.IGNORECASE):
            return f"http://{url}"
        return f"https://{url}"

    async def detect_auth_gate(self) -> bool:
        """Heuristic check: does the current page require login or authentication?"""
        if not self._page or self._page.is_closed():
            return False
        try:
            url_low = self._page.url.lower()
            auth_url_patterns = [
                "/login", "/signin", "/auth", "/session", "accounts.google.com",
                "github.com/login", "auth0.com", "okta.com", "challenge", "captcha",
            ]
            if any(p in url_low for p in auth_url_patterns):
                return True

            # Check for visible password input
            pw_count = await self._page.locator("input[type='password']").count()
            if pw_count > 0:
                return True

            title_low = (await self._page.title()).lower()
            if any(w in title_low for w in ["sign in", "log in", "authentication", "two-factor", "2fa"]):
                return True
        except Exception:
            return False
        return False

    async def handoff_to_user(self, reason: str = "Please log in") -> dict[str, Any]:
        """Switch browser to a visible window on user's desktop for HITL login/2FA."""
        async with self._lock:
            curr_url = self._current_url
            if self._page and not self._page.is_closed():
                curr_url = self._page.url

            # Launch visible headed window
            page = await self._ensure_page(headless=False)
            if curr_url and curr_url != "about:blank":
                try:
                    await page.goto(curr_url, timeout=20000, wait_until="domcontentloaded")
                except Exception:
                    pass

            self._waiting_for_user = True
            self._last_action = f"User Takeover: {reason}"
            title = await page.title()
            shot = await self._capture_screenshot()

            return {
                "ok": True,
                "mode": "user_takeover",
                "status": "waiting_for_user",
                "url": page.url,
                "title": title,
                "reason": reason,
                "screenshot": shot,
                "action": f"Browser opened on desktop for user login ({reason})",
            }

    async def resume_from_user(self) -> dict[str, Any]:
        """User finished login; return browser to autonomous background mode."""
        async with self._lock:
            curr_url = self._current_url
            if self._page and not self._page.is_closed():
                curr_url = self._page.url

            # Switch back to headless
            page = await self._ensure_page(headless=True)
            if curr_url and curr_url != "about:blank":
                try:
                    await page.goto(curr_url, timeout=20000, wait_until="domcontentloaded")
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

            self._waiting_for_user = False
            self._last_action = "Autonomous control resumed"
            title = await page.title()
            shot = await self._capture_screenshot()

            return {
                "ok": True,
                "mode": "autonomous",
                "status": "active",
                "url": page.url,
                "title": title,
                "screenshot": shot,
                "action": "Resumed autonomous background browsing",
            }

    async def navigate(self, url: str) -> dict[str, Any]:
        """Navigate to a URL and return page state."""
        async with self._lock:
            page = await self._ensure_page(headless=not self._is_headed)
            target = self._normalize_url(url)
            self._last_action = f"Navigating to {target}"
            try:
                await page.goto(target, timeout=25000, wait_until="domcontentloaded")
                await asyncio.sleep(0.5)
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"Navigation failed: {e}",
                    "url": target,
                    "title": "Error",
                    "screenshot": "",
                }

            self._current_url = page.url
            title = await page.title()
            shot = await self._capture_screenshot()
            auth_required = await self.detect_auth_gate()

            return {
                "ok": True,
                "url": page.url,
                "title": title or target,
                "screenshot": shot,
                "auth_required": auth_required,
                "action": f"Navigated to {page.url}",
            }

    async def click(self, target: str) -> dict[str, Any]:
        """Click a button, link, or element matching text or selector."""
        async with self._lock:
            page = await self._ensure_page(headless=not self._is_headed)
            self._last_action = f"Clicking {target}"
            try:
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
                    await page.click(target, timeout=3000)

                await asyncio.sleep(0.5)
            except Exception as e:
                return {"ok": False, "error": f"Could not click '{target}': {e}"}

            self._current_url = page.url
            title = await page.title()
            shot = await self._capture_screenshot()
            auth_required = await self.detect_auth_gate()

            return {
                "ok": True,
                "url": page.url,
                "title": title,
                "screenshot": shot,
                "auth_required": auth_required,
                "action": f"Clicked '{target}'",
            }

    async def type_text(self, selector: str, text: str) -> dict[str, Any]:
        """Fill or type into an input field."""
        async with self._lock:
            page = await self._ensure_page(headless=not self._is_headed)
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
            page = await self._ensure_page(headless=not self._is_headed)
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
            page = await self._ensure_page(headless=not self._is_headed)
            try:
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
        if not self._page or self._page.is_closed():
            return ""
        try:
            bytes_data = await self._page.screenshot(type="jpeg", quality=60)
            self._last_screenshot = base64.b64encode(bytes_data).decode("ascii")
            return self._last_screenshot
        except Exception:
            return ""

    async def get_state(self) -> dict[str, Any]:
        if not self._page or self._page.is_closed():
            return {"active": False, "url": "", "title": "Closed", "screenshot": ""}
        try:
            return {
                "active": True,
                "url": self._page.url,
                "title": await self._page.title(),
                "screenshot": self._last_screenshot,
                "action": self._last_action,
                "waiting_for_user": self._waiting_for_user,
                "is_headed": self._is_headed,
            }
        except Exception:
            return {"active": False, "url": "", "title": "", "screenshot": ""}

    async def close(self) -> None:
        async with self._lock:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
            self._page = None
            self._context = None
            self._playwright = None
            self._is_headed = False
            self._waiting_for_user = False
            self._last_action = "closed"


# --- Global Singleton -------------------------------------------------------

_GLOBAL_BROWSER: Optional[ZoroBrowser] = None


def get_browser() -> ZoroBrowser:
    global _GLOBAL_BROWSER
    if _GLOBAL_BROWSER is None:
        _GLOBAL_BROWSER = ZoroBrowser()
    return _GLOBAL_BROWSER
