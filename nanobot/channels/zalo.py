"""Zalo channel implementation (polling + webhook modes)."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

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

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                path = parsed.path
                if path != expected_path:
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
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"forbidden")
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length > 0 else b"{}"
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"bad request")
                    return

                if channel._loop and channel._webhook_queue:
                    channel._loop.call_soon_threadsafe(channel._webhook_queue.put_nowait, payload)

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

        update_obj = None
        try:
            update_cls = getattr(self._zalo_bot_mod, "Update", None)
            if update_cls is not None:
                update_obj = update_cls.de_json(raw, self._bot)
        except Exception:
            update_obj = None

        if update_obj:
            await self._on_update(update_obj)
            return

        # Fallback parser when SDK cannot deserialize payload format.
        if not isinstance(raw, dict):
            return
        msg = raw.get("message", {})
        text = (msg.get("text") or raw.get("text") or "").strip()
        if not text:
            return
        chat_id = str((msg.get("chat", {}) or {}).get("id") or msg.get("chat_id") or raw.get("chat_id") or "")
        sender = msg.get("from_user") or msg.get("sender") or raw.get("sender") or {}
        sender_id = str(sender.get("id") or chat_id)
        username = sender.get("username")
        sender_full = f"{sender_id}|{username}" if username else sender_id
        if not chat_id:
            return
        await self._on_text(
            chat_id=chat_id,
            sender_id=sender_full,
            text=text,
            display_name=sender.get("display_name", "bạn"),
            message_id=str(msg.get("message_id") or raw.get("message_id") or ""),
        )

    async def _on_update(self, update: Any) -> None:
        """Handle inbound update from Zalo SDK model."""
        message = getattr(update, "message", None)
        if not message:
            return

        text = (getattr(message, "text", None) or "").strip()
        if not text:
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
        )

    async def _on_text(
        self,
        chat_id: str,
        sender_id: str,
        text: str,
        display_name: str,
        message_id: str,
    ) -> None:
        lower = text.lower()
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

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=text,
            metadata=metadata,
        )

    async def _call_bot(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call SDK method and await only when it is coroutine-based."""
        if not self._bot:
            raise RuntimeError("Zalo bot is not initialized")
        fn = getattr(self._bot, method)
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
