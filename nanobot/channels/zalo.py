"""Zalo channel implementation using python-zalo-bot long polling."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import ZaloConfig


class ZaloChannel(BaseChannel):
    """Zalo channel using long polling with `python-zalo-bot`."""

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

    async def start(self) -> None:
        """Start Zalo polling loop."""
        if not self.config.token:
            logger.error("Zalo bot token not configured")
            return

        self._bot = self._zalo_bot_mod.Bot(self.config.token)
        self._running = True

        logger.info("Starting Zalo bot (polling mode)...")
        async with self._bot:
            try:
                me = await self._bot.get_me()
                logger.info(
                    "Zalo bot connected: {} ({})",
                    getattr(me, "account_name", "?"),
                    getattr(me, "id", "?"),
                )
                # Ensure polling mode can receive updates even if webhook was enabled before.
                await self._bot.delete_webhook()
            except Exception as e:
                logger.warning("Failed to fetch Zalo bot profile: {}", e)

            while self._running:
                try:
                    update = await self._bot.get_update(timeout=self.config.poll_timeout_seconds)
                    if not update:
                        continue
                    await self._on_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("Zalo polling error: {}", e)
                    await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop polling loop."""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """Send an outbound message to Zalo."""
        if not self._bot:
            logger.warning("Zalo bot not running")
            return

        chat_id = str(msg.chat_id)
        if msg.metadata.get("_progress"):
            try:
                await self._bot.send_chat_action(chat_id, self._typing_action)
            except Exception:
                pass

        for media in msg.media or []:
            try:
                if media.startswith("http://") or media.startswith("https://"):
                    await self._bot.send_photo(chat_id, "", media)
                else:
                    filename = media.rsplit("/", 1)[-1]
                    await self._bot.send_message(
                        chat_id,
                        f"[Attachment skipped: {filename}] "
                        "Zalo channel currently supports media URLs only.",
                    )
            except Exception as e:
                logger.error("Failed to send Zalo media {}: {}", media, e)

        content = (msg.content or "").strip()
        if content:
            try:
                await self._bot.send_message(chat_id, content)
            except Exception as e:
                logger.error("Failed to send Zalo message: {}", e)

    async def _on_update(self, update: Any) -> None:
        """Handle inbound update from Zalo."""
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

        lower = text.lower()
        if lower == "/start":
            display_name = getattr(user_obj, "display_name", "bạn")
            await self._bot.send_message(
                chat_id,
                f"Xin chào {display_name}! Tôi là nanobot.\n"
                "Gửi tin nhắn bất kỳ để bắt đầu.\n"
                "Dùng /help để xem lệnh hỗ trợ.",
            )
            return

        if lower == "/help":
            await self._bot.send_message(
                chat_id,
                "🐈 nanobot commands:\n"
                "/new — Start a new conversation\n"
                "/stop — Stop the current task\n"
                "/help — Show available commands",
            )
            return

        metadata = {}
        if msg_id := getattr(message, "message_id", None):
            metadata["message_id"] = str(msg_id)

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=text,
            metadata=metadata,
        )
