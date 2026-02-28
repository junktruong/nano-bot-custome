"""Playwright-powered provider that uses ChatGPT Web instead of API calls."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from nanobot.providers.base import LLMProvider, LLMResponse

_COMPOSER_SELECTORS = (
    'textarea[data-testid="composer-input"]',
    "#prompt-textarea",
    'textarea[placeholder*="Message"]',
    "textarea",
)
_SEND_BUTTON_SELECTORS = (
    'button[data-testid="send-button"]',
    'button[aria-label*="Send"]',
    'button:has-text("Send")',
)
_ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
_STOP_BUTTON_SELECTORS = (
    'button[aria-label*="Stop"]',
    'button:has-text("Stop generating")',
)


class ChatGPTWebProvider(LLMProvider):
    """Provider that sends prompts via browser automation to chatgpt.com."""

    def __init__(
        self,
        default_model: str = "chatgpt-web/default",
        chat_url: str = "https://chatgpt.com/",
        user_data_dir: str = "~/.nanobot/playwright/chatgpt",
        headless: bool = False,
        timeout_seconds: int = 180,
    ):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model
        self.chat_url = chat_url
        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.timeout_seconds = timeout_seconds

        self._playwright: Any | None = None
        self._context: Any | None = None
        self._lock = asyncio.Lock()

    def get_default_model(self) -> str:
        return self.default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        del tools, model, max_tokens, temperature  # ChatGPT Web does not expose API tool-calls.

        async with self._lock:
            try:
                context = await self._ensure_context()
                page = await context.new_page()
                try:
                    await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=30000)
                    prompt = self._messages_to_prompt(messages)
                    response = await self._submit_and_wait(page, prompt)
                    return LLMResponse(content=response, finish_reason="stop")
                finally:
                    await page.close()
            except Exception as e:
                return LLMResponse(
                    content=f"Error calling ChatGPT Web: {e}",
                    finish_reason="error",
                )

    async def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from e

        Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            viewport={"width": 1440, "height": 960},
        )
        return self._context

    async def _find_first_visible(self, page: Any, selectors: tuple[str, ...], timeout_ms: int) -> Any:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:
                continue
        raise RuntimeError(
            "Cannot find ChatGPT composer. Ensure you are logged in at https://chatgpt.com/ "
            f"inside profile dir: {self.user_data_dir}"
        )

    async def _submit_and_wait(self, page: Any, prompt: str) -> str:
        assistant = page.locator(_ASSISTANT_SELECTOR)
        previous_count = await assistant.count()

        composer = await self._find_first_visible(page, _COMPOSER_SELECTORS, timeout_ms=5000)
        await composer.click()
        await composer.fill(prompt)

        sent = False
        for selector in _SEND_BUTTON_SELECTORS:
            btn = page.locator(selector).first
            try:
                if await btn.count() > 0 and await btn.is_enabled():
                    await btn.click(timeout=1500)
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            await composer.press("Enter")

        deadline = time.monotonic() + self.timeout_seconds
        last_text = ""
        stable_ticks = 0

        while time.monotonic() < deadline:
            count = await assistant.count()
            if count > 0:
                idx = count - 1
                current = (await assistant.nth(idx).inner_text()).strip()
                if current:
                    if current == last_text:
                        stable_ticks += 1
                    else:
                        last_text = current
                        stable_ticks = 0

                    if count > previous_count and stable_ticks >= 2 and not await self._is_generating(page):
                        return current

            await asyncio.sleep(1.0)

        if last_text:
            return last_text
        raise TimeoutError(f"Timed out waiting for ChatGPT response after {self.timeout_seconds}s")

    async def _is_generating(self, page: Any) -> bool:
        for selector in _STOP_BUTTON_SELECTORS:
            btn = page.locator(selector).first
            try:
                if await btn.count() > 0 and await btn.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
                elif item.get("type") == "image_url":
                    chunks.append("[image]")
            return "\n".join(c for c in chunks if c).strip()
        if content is None:
            return ""
        return str(content)

    def _messages_to_prompt(self, messages: list[dict[str, Any]]) -> str:
        # Keep prompt bounded because browser chat input is not optimized for massive context.
        sliced = self._sanitize_empty_content(messages)[-30:]
        parts: list[str] = []
        for msg in sliced:
            role = str(msg.get("role", "user")).upper()
            text = self._content_to_text(msg.get("content", ""))
            if not text:
                continue
            parts.append(f"{role}:\n{text}")
        parts.append(
            "INSTRUCTION:\nRespond to the latest USER message. "
            "Do not call tools or output function-call JSON."
        )
        return "\n\n".join(parts)
