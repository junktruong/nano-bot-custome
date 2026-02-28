"""Zalo channel implementation (polling + webhook modes)."""

from __future__ import annotations

import asyncio
import inspect
import json
import mimetypes
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import ZaloConfig

_SECRET_HEADERS = (
    "x-secret-token",
    "x-zalo-secret-token",
    "x-webhook-secret",
)


class ZaloChannel(BaseChannel):
    """Zalo channel supporting both long polling and webhook delivery."""

    name = "zalo"

    def __init__(self, config: ZaloConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: ZaloConfig = config

        try:
            import zalo_bot  # type: ignore
            from zalo_bot.constants import ChatAction  # type: ignore
        except ImportError as e:
            raise ImportError(
                "zalo_bot dependency is missing. Install with: pip install python-zalo-bot"
            ) from e

        self._zalo_bot_mod = zalo_bot
        self._typing_action = ChatAction.TYPING
        self._bot: Any | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._webhook_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._webhook_consumer_task: asyncio.Task | None = None
        self._webhook_server: ThreadingHTTPServer | None = None
        self._webhook_thread: threading.Thread | None = None
        self._media_dir = Path.home() / ".nanobot" / "media" / "zalo"

    async def start(self) -> None:
        if not self.config.token:
            logger.error("Zalo bot token not configured")
            return

        self._bot = self._zalo_bot_mod.Bot(self.config.token)
        self._running = True

        mode = self.config.mode or "polling"
        if mode == "webhook":
            await self._run_webhook()
        else:
            await self._run_polling()

    async def stop(self) -> None:
        self._running = False

        if self._webhook_consumer_task:
            self._webhook_consumer_task.cancel()
            self._webhook_consumer_task = None

        if self._webhook_server:
            self._webhook_server.shutdown()
            self._webhook_server.server_close()
            self._webhook_server = None

        if self._webhook_thread and self._webhook_thread.is_alive():
            self._webhook_thread.join(timeout=2.0)
        self._webhook_thread = None

    async def send(self, msg: OutboundMessage) -> None:
        if not self._bot:
            logger.warning("Zalo bot not running")
            return

        chat_id = str(msg.chat_id)
        if msg.metadata.get("_progress"):
            try:
                await self._call_bot("send_chat_action", chat_id, self._typing_action)
            except Exception:
                pass

        for media in msg.media or []:
            try:
                if media.startswith("http://") or media.startswith("https://"):
                    await self._call_bot("send_photo", chat_id, "", media)
                else:
                    filename = media.rsplit("/", 1)[-1]
                    await self._call_bot(
                        "send_message",
                        chat_id,
                        f"[Attachment skipped: {filename}] "
                        "Zalo channel currently supports media URLs only.",
                    )
            except Exception as e:
                logger.error("Failed to send Zalo media {}: {}", media, e)

        content = (msg.content or "").strip()
        if content:
            try:
                await self._call_bot("send_message", chat_id, content)
            except Exception as e:
                logger.error("Failed to send Zalo message: {}", e)

    async def _run_polling(self) -> None:
        logger.info("Starting Zalo bot (polling mode)...")
        async with self._bot:
            try:
                me = await self._call_bot("get_me")
                logger.info(
                    "Zalo bot connected: {} ({})",
                    getattr(me, "account_name", "?"),
                    getattr(me, "id", "?"),
                )
                # Ensure polling mode can receive updates even if webhook was enabled before.
                await self._call_bot("delete_webhook")
            except Exception as e:
                logger.warning("Failed to initialize Zalo polling: {}", e)

            while self._running:
                try:
                    update = await self._call_bot("get_update", timeout=self.config.poll_timeout_seconds)
                    if update:
                        await self._on_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Zalo polling error: {}", e)
                    await asyncio.sleep(1)

    async def _run_webhook(self) -> None:
        if not self.config.webhook_url:
            logger.error("Zalo webhook mode requires channels.zalo.webhookUrl")
            return
        if not self.config.webhook_secret_token:
            logger.error("Zalo webhook mode requires channels.zalo.webhookSecretToken")
            return

        logger.info(
            "Starting Zalo bot (webhook mode): {} -> {}:{}{}",
            self.config.webhook_url,
            self.config.webhook_host,
            self.config.webhook_port,
            self.config.webhook_path,
        )

        async with self._bot:
            try:
                me = await self._call_bot("get_me")
                logger.info(
                    "Zalo bot connected: {} ({})",
                    getattr(me, "account_name", "?"),
                    getattr(me, "id", "?"),
                )
            except Exception as e:
                # get_me is useful for diagnostics but should not block webhook startup.
                logger.warning("Zalo get_me failed (continuing): {}", e)

            registered = False
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    await self._call_bot(
                        "set_webhook",
                        url=self.config.webhook_url,
                        secret_token=self.config.webhook_secret_token,
                    )
                    registered = True
                    logger.info("Zalo webhook registered: {}", self.config.webhook_url)
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "set_webhook attempt {}/3 failed: {}",
                        attempt,
                        e,
                    )
                    if attempt < 3:
                        await asyncio.sleep(1.0 * attempt)

            if not registered:
                logger.error("Failed to initialize Zalo webhook after retries: {}", last_error)
                return

            try:
                self._loop = asyncio.get_running_loop()
                self._webhook_queue = asyncio.Queue()
                self._start_webhook_server()
                self._webhook_consumer_task = asyncio.create_task(self._consume_webhook_queue())

                while self._running:
                    await asyncio.sleep(1)
            finally:
                if self._webhook_consumer_task:
                    self._webhook_consumer_task.cancel()
                    self._webhook_consumer_task = None
                if self._webhook_server:
                    self._webhook_server.shutdown()
                    self._webhook_server.server_close()
                    self._webhook_server = None

    def _start_webhook_server(self) -> None:
        channel = self
        expected_secret = self.config.webhook_secret_token.strip()
        expected_path = (self.config.webhook_path or "/zalo/webhook").strip() or "/zalo/webhook"

        class _WebhookHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
                return

            @staticmethod
            def _norm_path(p: str) -> str:
                if not p:
                    return "/"
                p = p.rstrip("/")
                return p or "/"

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                path = self._norm_path(parsed.path)
                expected = self._norm_path(expected_path)
                if path != expected:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                logger.debug("Zalo webhook GET probe: path={}", parsed.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                path = self._norm_path(parsed.path)
                expected = self._norm_path(expected_path)
                if path != expected:
                    logger.warning("Zalo webhook path mismatch: got={}, expected={}", parsed.path, expected_path)
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return

                provided = ""
                for key in _SECRET_HEADERS:
                    val = self.headers.get(key)
                    if val:
                        provided = val.strip()
                        break
                if not provided:
                    qs = parse_qs(parsed.query or "")
                    provided = (
                        (qs.get("secret_token") or [None])[0]
                        or (qs.get("token") or [None])[0]
                        or ""
                    )
                if expected_secret and provided != expected_secret:
                    logger.warning("Zalo webhook secret mismatch for path={}", parsed.path)
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"forbidden")
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length > 0 else b"{}"
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    logger.warning("Zalo webhook bad JSON payload")
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"bad request")
                    return

                if channel._loop and channel._webhook_queue:
                    channel._loop.call_soon_threadsafe(channel._webhook_queue.put_nowait, payload)
                    logger.debug("Zalo webhook accepted and queued (path={})", parsed.path)

                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

        self._webhook_server = ThreadingHTTPServer(
            (self.config.webhook_host, self.config.webhook_port),
            _WebhookHandler,
        )
        self._webhook_thread = threading.Thread(
            target=self._webhook_server.serve_forever,
            name="zalo-webhook-server",
            daemon=True,
        )
        self._webhook_thread.start()
        logger.info(
            "Zalo webhook listener started at http://{}:{}{}",
            self.config.webhook_host,
            self.config.webhook_port,
            expected_path,
        )

    async def _consume_webhook_queue(self) -> None:
        if not self._webhook_queue:
            return
        while self._running:
            try:
                payload = await self._webhook_queue.get()
                await self._on_webhook_payload(payload)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Zalo webhook payload processing failed")

    async def _on_webhook_payload(self, payload: dict[str, Any]) -> None:
        raw = payload.get("result", payload)
        raw_media_paths = await self._extract_media_paths(payload)

        update_obj = None
        try:
            update_cls = getattr(self._zalo_bot_mod, "Update", None)
            if update_cls is not None:
                update_obj = update_cls.de_json(raw, self._bot)
        except Exception:
            update_obj = None

        if update_obj:
            await self._on_update(update_obj, extra_media_paths=raw_media_paths, raw_fallback=raw)
            return

        # Fallback parser when SDK cannot deserialize payload format.
        if not isinstance(raw, dict):
            logger.debug("Zalo payload ignored (non-dict raw): {}", type(raw).__name__)
            return
        msg = self._coerce_dict(raw.get("message"))
        text = (
            msg.get("text")
            or msg.get("caption")
            or raw.get("text")
            or raw.get("content")
            or ""
        ).strip()
        media_paths = raw_media_paths
        if not text and not media_paths:
            logger.debug(
                "Zalo payload has no text/media keys: event={} top_keys={} message_keys={}",
                raw.get("event_name") or payload.get("event_name"),
                list(raw.keys())[:20],
                list(msg.keys())[:20],
            )
            return
        chat_obj = self._coerce_dict(msg.get("chat"))
        sender_obj = self._coerce_dict(msg.get("from_user")) or self._coerce_dict(msg.get("sender"))
        chat_id = str(
            chat_obj.get("id")
            or msg.get("chat_id")
            or raw.get("chat_id")
            or sender_obj.get("id")
            or ""
        )
        sender = (
            self._coerce_dict(msg.get("from_user"))
            or self._coerce_dict(msg.get("sender"))
            or self._coerce_dict(raw.get("sender"))
        )
        sender_id = str(sender.get("id") or chat_id)
        username = sender.get("username")
        sender_full = f"{sender_id}|{username}" if username else sender_id
        if not chat_id:
            logger.debug("Zalo fallback payload missing chat_id: top_keys={}", list(raw.keys())[:20])
            return
        await self._on_text(
            chat_id=chat_id,
            sender_id=sender_full,
            text=text,
            display_name=sender.get("display_name", "bạn"),
            message_id=str(msg.get("message_id") or raw.get("message_id") or ""),
            media_paths=media_paths,
        )

    async def _on_update(
        self,
        update: Any,
        extra_media_paths: list[str] | None = None,
        raw_fallback: Any | None = None,
    ) -> None:
        """Handle inbound update from Zalo SDK model."""
        message = getattr(update, "message", None)
        if not message:
            return

        text = (getattr(message, "text", None) or "").strip()
        media_paths = await self._extract_media_paths(message)
        if extra_media_paths:
            media_paths = list(dict.fromkeys([*media_paths, *extra_media_paths]))
        if not text and raw_fallback and isinstance(raw_fallback, dict):
            msg = raw_fallback.get("message", {})
            text = (
                (msg.get("caption") or msg.get("title") or msg.get("description") or "").strip()
                if isinstance(msg, dict)
                else ""
            )
        if not text and not media_paths:
            logger.debug("Zalo SDK update has no text/media")
            return

        chat_obj = getattr(message, "chat", None)
        user_obj = (
            getattr(update, "effective_user", None)
            or getattr(message, "from_user", None)
            or getattr(message, "sender", None)
            or chat_obj
        )

        chat_id = str(getattr(chat_obj, "id", "") or getattr(message, "chat_id", ""))
        sender_id_raw = str(getattr(user_obj, "id", "") or chat_id)
        username = getattr(user_obj, "username", None)
        sender_id = f"{sender_id_raw}|{username}" if username else sender_id_raw

        if not chat_id:
            logger.warning("Zalo update missing chat_id")
            return

        await self._on_text(
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
            display_name=getattr(user_obj, "display_name", "bạn"),
            message_id=str(getattr(message, "message_id", "") or ""),
            media_paths=media_paths,
        )

    async def _on_text(
        self,
        chat_id: str,
        sender_id: str,
        text: str,
        display_name: str,
        message_id: str,
        media_paths: list[str] | None = None,
    ) -> None:
        lower = text.lower() if text else ""
        if lower == "/start":
            await self._call_bot(
                "send_message",
                chat_id,
                f"Xin chào {display_name}! Tôi là nanobot.\n"
                "Gửi tin nhắn bất kỳ để bắt đầu.\n"
                "Dùng /help để xem lệnh hỗ trợ.",
            )
            return

        if lower == "/help":
            await self._call_bot(
                "send_message",
                chat_id,
                "🐈 nanobot commands:\n"
                "/new — Start a new conversation\n"
                "/stop — Stop the current task\n"
                "/help — Show available commands",
            )
            return

        metadata: dict[str, Any] = {}
        if message_id:
            metadata["message_id"] = message_id

        logger.info(
            "Zalo inbound message: chat_id={} text_len={} media_count={}",
            chat_id,
            len(text or ""),
            len(media_paths or []),
        )
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=text or "[image]",
            media=media_paths or [],
            metadata=metadata,
        )

    async def _extract_media_paths(self, source: Any) -> list[str]:
        data = self._to_plain(source)
        urls = self._find_media_urls(data)
        if not urls:
            return []
        http_urls = [u for u in urls if u.startswith("http://") or u.startswith("https://")]
        downloaded: list[str] = []
        if http_urls:
            downloaded = await self._download_image_urls(http_urls)
        # Keep original URLs as fallback if local download fails (or for providers
        # that can consume remote images directly).
        return list(dict.fromkeys([*downloaded, *urls]))

    def _to_plain(self, obj: Any, depth: int = 0) -> Any:
        if depth > 6:
            return None
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {str(k): self._to_plain(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [self._to_plain(v, depth + 1) for v in obj]

        if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
            try:
                return self._to_plain(obj.model_dump(), depth + 1)
            except Exception:
                pass
        if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
            try:
                return self._to_plain(obj.dict(), depth + 1)
            except Exception:
                pass
        if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
            try:
                return self._to_plain(obj.to_dict(), depth + 1)
            except Exception:
                pass

        if hasattr(obj, "__dict__"):
            out: dict[str, Any] = {}
            for k, v in vars(obj).items():
                if k.startswith("_"):
                    continue
                if k.lower() in {"bot", "context", "application", "dispatcher"}:
                    continue
                out[k] = self._to_plain(v, depth + 1)
            return out
        if hasattr(obj, "__slots__"):
            out: dict[str, Any] = {}
            for k in getattr(obj, "__slots__", []) or []:
                if k.startswith("_"):
                    continue
                try:
                    v = getattr(obj, k)
                except Exception:
                    continue
                out[k] = self._to_plain(v, depth + 1)
            if out:
                return out
        return None

    @staticmethod
    def _coerce_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            s = value.strip()
            if s and s[0] in "{[" and s[-1] in "}]":
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    return {}
        return {}

    def _find_media_urls(self, data: Any) -> list[str]:
        urls: set[str] = set()
        seen: set[int] = set()

        def _walk(node: Any, key_hint: str = "") -> None:
            oid = id(node)
            if oid in seen:
                return
            seen.add(oid)

            if isinstance(node, dict):
                for k, v in node.items():
                    _walk(v, str(k).lower())
                return

            if isinstance(node, list):
                for v in node:
                    _walk(v, key_hint)
                return

            if isinstance(node, str):
                s = node.strip()
                if s.startswith("data:image/"):
                    urls.add(s)
                    return
                if not (s.startswith("http://") or s.startswith("https://")):
                    if s and s[0] in "{[" and s[-1] in "}]":
                        try:
                            parsed = json.loads(s)
                            _walk(parsed, key_hint)
                        except Exception:
                            pass
                    return
                if self._looks_like_image_url(s) or any(
                    t in key_hint
                    for t in (
                        "image",
                        "photo",
                        "thumb",
                        "media",
                        "attachment",
                        "url",
                        "src",
                        "source",
                        "link",
                        "cover",
                        "file",
                    )
                ):
                    urls.add(s)

        _walk(data)
        return list(urls)

    @staticmethod
    def _looks_like_image_url(url: str) -> bool:
        u = url.lower()
        return any(u.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"))

    @staticmethod
    def _looks_like_image_bytes(raw: bytes) -> bool:
        if not raw:
            return False
        signatures = (
            b"\xff\xd8\xff",  # JPEG
            b"\x89PNG\r\n\x1a\n",  # PNG
            b"GIF87a",
            b"GIF89a",
            b"RIFF",  # WebP in RIFF container (further checked below)
            b"BM",  # BMP
        )
        if any(raw.startswith(sig) for sig in signatures):
            if raw.startswith(b"RIFF"):
                return b"WEBP" in raw[:16]
            return True
        return False

    async def _download_image_urls(self, urls: list[str]) -> list[str]:
        self._media_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []

        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            for i, url in enumerate(urls):
                try:
                    r = await client.get(
                        url,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 (X11; Linux x86_64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/121.0.0.0 Safari/537.36"
                            )
                        },
                    )
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    looks_image = (
                        ctype.startswith("image/")
                        or self._looks_like_image_url(url)
                        or self._looks_like_image_bytes(r.content)
                    )
                    if not looks_image:
                        logger.debug("Skip non-image media URL: {} (content-type={})", url, ctype or "n/a")
                        continue
                    ext = mimetypes.guess_extension(ctype) or Path(urlsplit(url).path).suffix or ".jpg"
                    path = self._media_dir / f"{uuid.uuid4().hex}_{i}{ext}"
                    path.write_bytes(r.content)
                    saved.append(str(path))
                except Exception as e:
                    logger.debug("Failed to download media URL {}: {}", url, e)
                    continue
        return saved

    async def _call_bot(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call SDK method safely inside an existing event loop.

        Some `python-zalo-bot` methods are sync wrappers that internally call
        `asyncio.run(...)` (for example `set_webhook`). That crashes under our
        async runtime. Prefer the SDK's async counterparts when available.
        """
        if not self._bot:
            raise RuntimeError("Zalo bot is not initialized")

        # Prefer async variants exposed by the SDK internals.
        for alt_name in (f"_{method}_async", f"{method}_async"):
            alt = getattr(self._bot, alt_name, None)
            if callable(alt):
                result = alt(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result

        fn = getattr(self._bot, method)
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
