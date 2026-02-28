"""Playwright-powered provider that uses ChatGPT Web instead of API calls."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

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
_ATTACH_BUTTON_SELECTORS = (
    'button[aria-label*="Attach"]',
    'button[aria-label*="Upload"]',
)
_FILE_INPUT_SELECTORS = (
    'input[type="file"]',
)
_ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
_STOP_BUTTON_SELECTORS = (
    'button[aria-label*="Stop"]',
    'button:has-text("Stop generating")',
)
_RUNTIME_TAG = "[Runtime Context"
_DATA_IMAGE_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)


class ChatGPTWebProvider(LLMProvider):
    """Provider that sends prompts via browser automation to chatgpt.com."""

    def __init__(
        self,
        default_model: str = "chatgpt-web/default",
        chat_url: str = "https://chatgpt.com/",
        user_data_dir: str = "~/.nanobot/playwright/chatgpt",
        headless: bool = False,
        timeout_seconds: int = 60,
        browser_channel: str = "chrome",
        executable_path: str | None = None,
    ):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model
        self.chat_url = chat_url
        self.user_data_dir = str(Path(user_data_dir).expanduser())
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self.browser_channel = browser_channel
        self.executable_path = executable_path

        self._playwright: Any | None = None
        self._context: Any | None = None
        self._lock = asyncio.Lock()
        self._pages: dict[str, Any] = {}
        self._turn_count: dict[str, int] = {}
        self._composer_selector_hint: dict[str, str] = {}
        self._upload_dir = Path(self.user_data_dir) / "uploads"

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

        temp_files: list[Path] = []
        async with self._lock:
            try:
                session_key = self._extract_session_key(messages)
                page = await self._ensure_page(session_key)
                prompt, images, temp_files = await self._build_turn_input(messages, session_key)
                response = await self._submit_and_wait(
                    page=page,
                    session_key=session_key,
                    prompt=prompt,
                    image_paths=images,
                )
                self._turn_count[session_key] = self._turn_count.get(session_key, 0) + 1
                return LLMResponse(content=response, finish_reason="stop")
            except Exception as e:
                return LLMResponse(
                    content=f"Error calling ChatGPT Web: {e}",
                    finish_reason="error",
                )
            finally:
                for p in temp_files:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass

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
        self._upload_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        launch_opts: dict[str, Any] = {
            "user_data_dir": self.user_data_dir,
            "headless": self.headless,
            "viewport": {"width": 1440, "height": 960},
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if self.browser_channel:
            launch_opts["channel"] = self.browser_channel
        if self.executable_path:
            launch_opts["executable_path"] = self.executable_path
        self._context = await self._playwright.chromium.launch_persistent_context(**launch_opts)
        return self._context

    async def _ensure_page(self, session_key: str) -> Any:
        context = await self._ensure_context()
        page = self._pages.get(session_key)
        if page is not None and not page.is_closed():
            return page

        page = await context.new_page()
        self._pages[session_key] = page
        self._turn_count.setdefault(session_key, 0)
        await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=30000)
        await self._find_composer(page, session_key, max_wait_s=30.0)
        return page

    async def _find_composer(self, page: Any, session_key: str, max_wait_s: float = 8.0) -> Any:
        selectors = list(_COMPOSER_SELECTORS)
        hinted = self._composer_selector_hint.get(session_key)
        if hinted and hinted in selectors:
            selectors.remove(hinted)
            selectors.insert(0, hinted)

        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            for selector in selectors:
                locator = page.locator(selector).first
                try:
                    if await locator.count() > 0 and await locator.is_visible():
                        self._composer_selector_hint[session_key] = selector
                        return locator
                except Exception:
                    continue
            await asyncio.sleep(0.2)

        raise RuntimeError(
            "Cannot find ChatGPT composer. Ensure you are logged in at https://chatgpt.com/ "
            f"inside profile dir: {self.user_data_dir}"
        )

    async def _submit_and_wait(
        self,
        page: Any,
        session_key: str,
        prompt: str,
        image_paths: list[str],
    ) -> str:
        assistant = page.locator(_ASSISTANT_SELECTOR)
        previous_count = await assistant.count()

        composer = await self._find_composer(page, session_key, max_wait_s=8.0)
        await composer.click(timeout=1000)
        if image_paths:
            await self._attach_images(page, image_paths)

        await composer.fill(prompt or "Please analyze the attached image.")

        sent = await self._click_send(page, composer, max_wait_s=8.0)
        if not sent:
            raise TimeoutError("Failed to trigger send in ChatGPT composer")

        timeout_s = min(max(5, int(self.timeout_seconds)), 60)
        deadline = time.monotonic() + timeout_s
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

            await asyncio.sleep(0.5)

        if last_text:
            return last_text
        raise TimeoutError(f"Timed out waiting for ChatGPT response after {timeout_s}s")

    async def _click_send(self, page: Any, composer: Any, max_wait_s: float) -> bool:
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            for selector in _SEND_BUTTON_SELECTORS:
                btn = page.locator(selector).first
                try:
                    if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                        await btn.click(timeout=1000)
                        return True
                except Exception:
                    continue

            # Fallback to Enter if no send button surfaced yet.
            try:
                await composer.press("Enter")
                await asyncio.sleep(0.2)
                if await self._is_generating(page):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def _attach_images(self, page: Any, image_paths: list[str]) -> None:
        if not image_paths:
            return

        for selector in _ATTACH_BUTTON_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                    await btn.click(timeout=500)
                    break
            except Exception:
                continue

        for selector in _FILE_INPUT_SELECTORS:
            locator = page.locator(selector).first
            try:
                if await locator.count() > 0:
                    await locator.set_input_files(image_paths, timeout=10000)
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue

    async def _is_generating(self, page: Any) -> bool:
        for selector in _STOP_BUTTON_SELECTORS:
            btn = page.locator(selector).first
            try:
                if await btn.count() > 0 and await btn.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _build_turn_input(
        self,
        messages: list[dict[str, Any]],
        session_key: str,
    ) -> tuple[str, list[str], list[Path]]:
        latest_text, latest_images, temp_files = await self._extract_latest_user_input(messages)

        # First turn of a browser session: bootstrap with full prompt once.
        if self._turn_count.get(session_key, 0) == 0:
            bootstrap = self._messages_to_prompt(messages)
            return bootstrap or latest_text, latest_images, temp_files

        return latest_text, latest_images, temp_files

    async def _extract_latest_user_input(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[str], list[Path]]:
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")

            if isinstance(content, str):
                text = content.strip()
                if not text or text.startswith(_RUNTIME_TAG):
                    continue
                return text, [], []

            if isinstance(content, list):
                text_chunks: list[str] = []
                image_paths: list[str] = []
                temp_files: list[Path] = []

                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        t = str(item.get("text", "")).strip()
                        if t:
                            text_chunks.append(t)
                    elif item.get("type") == "image_url":
                        url = str((item.get("image_url") or {}).get("url", "")).strip()
                        local = await self._materialize_image(url)
                        if local:
                            image_paths.append(str(local))
                            temp_files.append(local)

                text = "\n".join(text_chunks).strip()
                if text or image_paths:
                    return text, image_paths, temp_files

        return "", [], []

    async def _materialize_image(self, url: str) -> Path | None:
        if not url:
            return None

        if url.startswith("data:image/"):
            m = _DATA_IMAGE_RE.match(url)
            if not m:
                return None
            mime = m.group(1)
            raw_b64 = m.group(2)
            data = base64.b64decode(raw_b64)
            ext = mimetypes.guess_extension(mime) or ".png"
            path = self._upload_dir / f"{uuid.uuid4().hex}{ext}"
            path.write_bytes(data)
            return path

        if url.startswith("http://") or url.startswith("https://"):
            async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not ctype.startswith("image/"):
                    return None
                ext = mimetypes.guess_extension(ctype) or ".png"
                path = self._upload_dir / f"{uuid.uuid4().hex}{ext}"
                path.write_bytes(r.content)
                return path
        return None

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in ("text", "input_text", "output_text"):
                    chunks.append(str(item.get("text", "")))
                elif item.get("type") == "image_url":
                    chunks.append("[image]")
            return "\n".join(c for c in chunks if c).strip()
        if content is None:
            return ""
        return str(content)

    def _messages_to_prompt(self, messages: list[dict[str, Any]]) -> str:
        # Bootstrap only: keep context bounded to reduce initial compose latency.
        sliced = self._sanitize_empty_content(messages)[-20:]
        parts: list[str] = []
        for msg in sliced:
            role = str(msg.get("role", "user")).upper()
            text = self._content_to_text(msg.get("content", ""))
            if not text:
                continue
            parts.append(f"{role}:\n{text}")
        parts.append(
            "INSTRUCTION:\nRespond to the latest USER message naturally. "
            "Do not output any function-call JSON."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _extract_session_key(messages: list[dict[str, Any]]) -> str:
        channel = ""
        chat_id = ""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.startswith(_RUNTIME_TAG):
                continue
            for line in content.splitlines():
                if line.startswith("Channel:"):
                    channel = line.split(":", 1)[1].strip()
                elif line.startswith("Chat ID:"):
                    chat_id = line.split(":", 1)[1].strip()
            break
        if channel and chat_id:
            return f"{channel}:{chat_id}"
        return "default"
