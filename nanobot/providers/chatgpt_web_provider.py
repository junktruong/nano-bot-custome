"""Playwright-powered provider that uses ChatGPT Web instead of API calls."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import time
import uuid
import weakref
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_COMPOSER_SELECTORS = (
    'div#prompt-textarea[contenteditable="true"]',
    'div[data-testid="composer-input"][contenteditable="true"]',
    'textarea[data-testid="composer-input"]',
    "#prompt-textarea",
    'textarea[placeholder*="Message"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
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
_USER_SELECTOR = '[data-message-author-role="user"]'
_STOP_BUTTON_SELECTORS = (
    'button[aria-label*="Stop"]',
    'button:has-text("Stop generating")',
)
_RUNTIME_TAG = "[Runtime Context"
_DATA_IMAGE_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*([\s\S]*?)\s*</tool_call>", re.I)
_TOOL_CALLS_TAG_RE = re.compile(r"<tool_calls>\s*([\s\S]*?)\s*</tool_calls>", re.I)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


class ChatGPTWebProvider(LLMProvider):
    """Provider that sends prompts via browser automation to chatgpt.com."""

    def __init__(
        self,
        default_model: str = "chatgpt-web/default",
        chat_url: str = "https://chatgpt.com/",
        user_data_dir: str = "~/.nanobot/playwright/chatgpt",
        headless: bool = False,
        timeout_seconds: int = 300,
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
        self._context_lock = asyncio.Lock()
        self._page_alloc_lock = asyncio.Lock()
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._pages: dict[str, Any] = {}
        self._warm_page: Any | None = None
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
        del model, max_tokens, temperature  # Not supported by ChatGPT web UI.

        temp_files: list[Path] = []
        session_key = self._extract_session_key(messages)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            try:
                page = await self._ensure_page(session_key)
                prompt, images, temp_files = await self._build_turn_input(messages, session_key, tools)
                logger.debug(
                    "ChatGPT Web submit: session={} prompt_len={} images={} tools={}",
                    session_key,
                    len(prompt or ""),
                    len(images),
                    len(tools or []),
                )
                response = ""
                last_error: Exception | None = None
                for attempt in range(1, 3):
                    try:
                        response = await self._submit_and_wait(
                            page=page,
                            session_key=session_key,
                            prompt=prompt,
                            image_paths=images,
                        )
                        last_error = None
                        break
                    except Exception as e:
                        last_error = e
                        transient = (
                            "Cannot find ChatGPT composer" in str(e)
                            or "Locator.click" in str(e)
                            or "Message was not submitted" in str(e)
                        )
                        if attempt >= 2 or not transient:
                            break
                        logger.warning(
                            "ChatGPT Web transient error (attempt {}/2), reloading page: {}",
                            attempt,
                            e,
                        )
                        await self._recover_page(page, session_key)
                if last_error is not None:
                    raise last_error
                tool_calls, clean_content = self._extract_tool_calls(response, tools)
                logger.debug(
                    "ChatGPT Web response: session={} response_len={} tool_calls={}",
                    session_key,
                    len(response or ""),
                    len(tool_calls),
                )
                self._turn_count[session_key] = self._turn_count.get(session_key, 0) + 1
                if tool_calls:
                    return LLMResponse(
                        content=clean_content or None,
                        tool_calls=tool_calls,
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content=clean_content, finish_reason="stop")
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
        async with self._context_lock:
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

        created_new = False
        async with self._page_alloc_lock:
            page = self._pages.get(session_key)
            if page is not None and not page.is_closed():
                return page

            if self._warm_page is not None and not self._warm_page.is_closed():
                page = self._warm_page
                self._warm_page = None
                self._pages[session_key] = page
                self._turn_count.setdefault(session_key, 0)
                logger.debug("ChatGPT Web warm page claimed for session={}", session_key)
                return page

            page = await context.new_page()
            self._pages[session_key] = page
            self._turn_count.setdefault(session_key, 0)
            created_new = True

        if created_new:
            try:
                await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=30000)
                await self._find_composer(page, session_key, max_wait_s=30.0)
            except Exception:
                async with self._page_alloc_lock:
                    if self._pages.get(session_key) is page:
                        self._pages.pop(session_key, None)
                try:
                    await page.close()
                except Exception:
                    pass
                raise
        return page

    async def warmup(self) -> None:
        """Best-effort warmup to reduce first-message latency after startup."""
        context = await self._ensure_context()
        async with self._page_alloc_lock:
            if self._warm_page is not None and not self._warm_page.is_closed():
                return
            page = await context.new_page()
            self._warm_page = page

        try:
            await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=30000)
            # Non-fatal: login gates can block composer until user signs in.
            try:
                await self._find_composer(page, "__warmup__", max_wait_s=8.0)
            except Exception as e:
                logger.debug("ChatGPT Web warmup composer not ready yet: {}", e)
            logger.info("ChatGPT Web warmup page is ready")
        except Exception:
            async with self._page_alloc_lock:
                if self._warm_page is page:
                    self._warm_page = None
            try:
                await page.close()
            except Exception:
                pass
            raise

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

    async def _recover_page(self, page: Any, session_key: str) -> None:
        try:
            await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                return
        try:
            await self._find_composer(page, session_key, max_wait_s=20.0)
        except Exception:
            pass

    async def _submit_and_wait(
        self,
        page: Any,
        session_key: str,
        prompt: str,
        image_paths: list[str],
    ) -> str:
        assistant = page.locator(_ASSISTANT_SELECTOR)
        user_msgs = page.locator(_USER_SELECTOR)
        previous_count = await assistant.count()
        previous_user_count = await user_msgs.count()
        baseline_text = ""
        if previous_count > 0:
            try:
                baseline_text = (await assistant.nth(previous_count - 1).inner_text()).strip()
            except Exception:
                baseline_text = ""

        composer = await self._find_composer(page, session_key, max_wait_s=4.0)
        if image_paths:
            await self._attach_images(page, image_paths)

        await self._set_composer_text(page, composer, prompt or "Please analyze the attached image.")

        sent = await self._click_send(page, composer, max_wait_s=8.0)
        if not sent:
            raise TimeoutError("Failed to trigger send in ChatGPT composer")

        timeout_s = min(max(5, int(self.timeout_seconds)), 300)
        deadline = time.monotonic() + timeout_s
        last_text = ""
        stable_ticks = 0
        submitted = False

        while time.monotonic() < deadline:
            try:
                if await user_msgs.count() > previous_user_count:
                    submitted = True
            except Exception:
                pass

            count = await assistant.count()
            if count > 0:
                idx = count - 1
                current = (await assistant.nth(idx).inner_text()).strip()
                if current:
                    changed = (count > previous_count) or (current != baseline_text)
                    if not changed:
                        await asyncio.sleep(0.5)
                        continue

                    if current == last_text:
                        stable_ticks += 1
                    else:
                        last_text = current
                        stable_ticks = 0

                    if stable_ticks >= 2 and not await self._is_generating(page):
                        return current

            await asyncio.sleep(0.5)

        if last_text and last_text != baseline_text:
            return last_text
        if not submitted:
            raise TimeoutError(f"Message was not submitted to ChatGPT within {timeout_s}s")
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
                await self._focus_composer_soft(composer)
                await composer.press("Enter")
                await asyncio.sleep(0.15)
                # Even if stop button is not visible yet, Enter may have submitted.
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

    async def _set_composer_text(self, page: Any, composer: Any, text: str) -> None:
        # Fast path: direct DOM write for both input and contenteditable composers.
        try:
            await composer.evaluate(
                """(el, value) => {
                    const isInput = el.tagName === "TEXTAREA" || el.tagName === "INPUT";
                    if (isInput) {
                        el.value = value;
                        el.dispatchEvent(new Event("input", { bubbles: true }));
                        return;
                    }
                    if (el.isContentEditable) {
                        el.innerHTML = "";
                        const lines = String(value).split("\\n");
                        for (let i = 0; i < lines.length; i++) {
                            if (i > 0) el.appendChild(document.createElement("br"));
                            el.appendChild(document.createTextNode(lines[i]));
                        }
                        el.dispatchEvent(new Event("input", { bubbles: true }));
                        return;
                    }
                    el.textContent = value;
                    el.dispatchEvent(new Event("input", { bubbles: true }));
                }""",
                text,
            )
            return
        except Exception:
            pass

        await self._focus_composer_soft(composer)

        # Fallback path for textarea/input.
        try:
            await composer.fill(text, timeout=700)
            return
        except Exception:
            pass

        # Fallback click path when direct write fails.
        try:
            await composer.click(timeout=700)
        except Exception:
            pass

        # Last-resort keyboard typing if DOM write paths fail.
        await page.keyboard.type(text, delay=0)

    async def _focus_composer_soft(self, composer: Any) -> None:
        # Best-effort focus: never raise.
        try:
            await composer.scroll_into_view_if_needed(timeout=500)
        except Exception:
            pass
        try:
            await composer.click(timeout=700)
            return
        except Exception:
            pass
        try:
            await composer.click(timeout=700, force=True)
            return
        except Exception:
            pass
        try:
            await composer.evaluate(
                """(el) => {
                    if (!el) return;
                    if (typeof el.focus === "function") el.focus();
                    if (el.isContentEditable) {
                        const range = document.createRange();
                        range.selectNodeContents(el);
                        range.collapse(false);
                        const sel = window.getSelection();
                        if (sel) {
                            sel.removeAllRanges();
                            sel.addRange(range);
                        }
                    }
                }"""
            )
        except Exception:
            pass

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
        tools: list[dict[str, Any]] | None,
    ) -> tuple[str, list[str], list[Path]]:
        runtime_context = self._extract_latest_runtime_context(messages)
        latest_text, latest_images, temp_files = await self._extract_latest_user_input(messages)

        # During tool loop, include recent tool context so the model can continue.
        if self._has_pending_tool_context(messages):
            return self._messages_to_prompt(messages, tools=tools), [], temp_files

        # First turn of a browser session: bootstrap with full prompt once.
        if self._turn_count.get(session_key, 0) == 0:
            bootstrap = self._messages_to_prompt(messages, tools=tools)
            return bootstrap or latest_text, latest_images, temp_files

        if tools:
            latest_text = self._append_tool_hint(latest_text)
        if runtime_context:
            latest_text = f"{runtime_context}\n\n{latest_text}".strip()
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

    @staticmethod
    def _extract_latest_runtime_context(messages: list[dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content.strip()
                if text.startswith(_RUNTIME_TAG):
                    return text
        return ""

    def _messages_to_prompt(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> str:
        # Bootstrap only: keep context bounded to reduce initial compose latency.
        sliced = self._sanitize_empty_content(messages)[-20:]
        parts: list[str] = []
        for msg in sliced:
            role = str(msg.get("role", "user")).upper()
            text = self._content_to_text(msg.get("content", ""))
            if not text:
                continue
            parts.append(f"{role}:\n{text}")
        parts.append(f"INSTRUCTION:\n{self._build_tool_instruction(tools)}")
        return "\n\n".join(parts)

    @staticmethod
    def _has_pending_tool_context(messages: list[dict[str, Any]]) -> bool:
        for msg in reversed(messages):
            role = str(msg.get("role", ""))
            if role == "user":
                return False
            if role == "tool":
                return True
            if role == "assistant" and msg.get("tool_calls"):
                return True
        return False

    @staticmethod
    def _build_tool_instruction(tools: list[dict[str, Any]] | None) -> str:
        if not tools:
            return "Respond naturally to the latest USER message."

        lines = [
            "Respond to the latest USER message.",
            "If no tool is needed, answer naturally.",
            "Do not call the 'message' tool for normal replies to the current user.",
            "Use 'message' tool only when explicitly asked to send to a different channel/chat.",
            "Never claim a listed tool is unavailable; if it's listed below, it is available now.",
            "For reminder/scheduling requests (nhac lich/remind/schedule), prefer the `cron` tool directly.",
            "Do not ask the user to run manual CLI commands when `cron` can do it.",
            "If a tool is needed, output EXACTLY one tag with compact JSON and nothing else:",
            '<tool_call>{"name":"tool_name","arguments":{"key":"value"}}</tool_call>',
            "Do not wrap with markdown code fences.",
            "Available tools:",
        ]
        for tool in tools[:20]:
            fn = tool.get("function", {}) if isinstance(tool, dict) else {}
            name = str(fn.get("name", "")).strip()
            if not name:
                continue
            desc = str(fn.get("description", "")).strip().splitlines()[0][:120]
            props = ((fn.get("parameters", {}) or {}).get("properties", {}) or {})
            params = ", ".join(list(props.keys())[:8])
            suffix = f" params: {params}" if params else ""
            lines.append(f"- {name}: {desc}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _append_tool_hint(text: str) -> str:
        base = text.strip() if text else ""
        hint = (
            'If you need a tool, output only: '
            '<tool_call>{"name":"tool_name","arguments":{...}}</tool_call>'
        )
        return f"{base}\n\n{hint}".strip()

    def _extract_tool_calls(
        self,
        text: str,
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[ToolCallRequest], str]:
        if not text:
            return [], ""

        allowed: set[str] = set()
        for tool in tools or []:
            fn = tool.get("function", {}) if isinstance(tool, dict) else {}
            name = fn.get("name")
            if isinstance(name, str) and name.strip():
                allowed.add(name.strip())

        payload_texts: list[str] = []
        payload_texts.extend(m.group(1).strip() for m in _TOOL_CALL_TAG_RE.finditer(text))
        payload_texts.extend(m.group(1).strip() for m in _TOOL_CALLS_TAG_RE.finditer(text))

        # Recover from malformed output where model emits "<tool_call>{...}" but
        # forgets the closing tag.
        if not payload_texts:
            lower = text.lower()
            if "<tool_call>" in lower:
                payload_texts.append(text[text.lower().find("<tool_call>") + len("<tool_call>"):].strip())
            elif "<tool_calls>" in lower:
                payload_texts.append(text[text.lower().find("<tool_calls>") + len("<tool_calls>"):].strip())

        stripped = text.strip()
        if not payload_texts:
            if stripped.startswith("{") or stripped.startswith("["):
                payload_texts.append(stripped)
            else:
                for m in _JSON_FENCE_RE.finditer(text):
                    candidate = m.group(1).strip()
                    if candidate.startswith("{") or candidate.startswith("["):
                        payload_texts.append(candidate)

        calls: list[ToolCallRequest] = []
        for payload_text in payload_texts:
            parsed = self._safe_json_loads(payload_text)
            if parsed is None:
                continue
            for name, args in self._normalize_tool_payload(parsed):
                if allowed and name not in allowed:
                    continue
                if not isinstance(args, dict):
                    continue
                calls.append(
                    ToolCallRequest(
                        id=str(uuid.uuid4())[:8],
                        name=name,
                        arguments=args,
                    )
                )

        clean = _TOOL_CALL_TAG_RE.sub("", text)
        clean = _TOOL_CALLS_TAG_RE.sub("", clean)
        clean = re.sub(r"<tool_calls?>\s*", "", clean, flags=re.I)
        content_from_message_tool = ""
        filtered_calls: list[ToolCallRequest] = []
        for call in calls:
            if call.name != "message":
                filtered_calls.append(call)
                continue
            args = call.arguments or {}
            channel = str(args.get("channel", "") or "").strip()
            chat_id = str(args.get("chat_id", "") or "").strip()
            media = args.get("media") or []
            msg_text = str(args.get("content", "") or "").strip()
            # Treat simple same-chat message tool calls as normal assistant text.
            if not channel and not chat_id and not media and msg_text:
                if not content_from_message_tool:
                    content_from_message_tool = msg_text
                continue
            filtered_calls.append(call)

        clean_text = clean.strip()
        if content_from_message_tool and not clean_text:
            clean_text = content_from_message_tool
        return filtered_calls, clean_text

    @staticmethod
    def _safe_json_loads(raw: str) -> Any | None:
        try:
            return json.loads(raw)
        except Exception:
            candidate = ChatGPTWebProvider._extract_first_json_blob(raw)
            if candidate:
                try:
                    return json.loads(candidate)
                except Exception:
                    return None
            return None

    @staticmethod
    def _extract_first_json_blob(raw: str) -> str | None:
        text = (raw or "").strip()
        if not text:
            return None
        start = -1
        opener = ""
        for ch in text:
            if ch in "{[":
                opener = ch
                break
        if not opener:
            return None
        start = text.find(opener)
        if start < 0:
            return None

        stack: list[str] = [opener]
        in_string = False
        escaped = False
        quote_char = '"'

        for idx in range(start + 1, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == quote_char:
                    in_string = False
                continue

            if ch in ("'", '"'):
                in_string = True
                quote_char = ch
                continue
            if ch in "{[":
                stack.append(ch)
                continue
            if ch in "}]":
                if not stack:
                    return None
                top = stack[-1]
                if (top == "{" and ch != "}") or (top == "[" and ch != "]"):
                    return None
                stack.pop()
                if not stack:
                    return text[start:idx + 1]

        return None

    def _normalize_tool_payload(self, parsed: Any) -> list[tuple[str, dict[str, Any]]]:
        entries: list[Any] = []
        if isinstance(parsed, dict):
            if isinstance(parsed.get("tool_calls"), list):
                entries.extend(parsed["tool_calls"])
            elif "name" in parsed and "arguments" in parsed:
                entries.append(parsed)
            elif isinstance(parsed.get("function"), dict):
                fn = parsed["function"]
                entries.append({"name": fn.get("name"), "arguments": fn.get("arguments", {})})
        elif isinstance(parsed, list):
            entries.extend(parsed)

        out: list[tuple[str, dict[str, Any]]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("tool")
            if not name and isinstance(item.get("function"), dict):
                name = item["function"].get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            args: Any = item.get("arguments", item.get("args", {}))
            if args is None and isinstance(item.get("function"), dict):
                args = item["function"].get("arguments", {})
            if isinstance(args, str):
                parsed_args = self._safe_json_loads(args)
                if parsed_args is None:
                    continue
                args = parsed_args
            if not isinstance(args, dict):
                continue
            out.append((name.strip(), args))
        return out

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
