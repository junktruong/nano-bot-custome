#!/usr/bin/env python3
"""Minimal Facebook Messenger web helper (list/read/send once)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _jprint(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _cfg_get(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        if key in cur:
            cur = cur[key]
            continue
        # Support both snake_case and camelCase config keys
        camel = "".join(part.capitalize() if i else part for i, part in enumerate(key.split("_")))
        if camel in cur:
            cur = cur[camel]
            continue
        return None
    return cur


def _load_chatgpt_web_defaults() -> dict[str, str]:
    defaults = {
        "profile_dir": "~/.nanobot/playwright/chatgpt",
        "channel": "chrome",
        "executable_path": "",
    }
    cfg_path = Path.home() / ".nanobot" / "config.json"
    if not cfg_path.exists():
        return defaults
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return defaults

    profile = _cfg_get(data, "providers", "chatgpt_web", "user_data_dir")
    if isinstance(profile, str) and profile.strip():
        defaults["profile_dir"] = profile.strip()

    channel = _cfg_get(data, "providers", "chatgpt_web", "browser_channel")
    if isinstance(channel, str) and channel.strip():
        defaults["channel"] = channel.strip()

    exe = _cfg_get(data, "providers", "chatgpt_web", "executable_path")
    if isinstance(exe, str) and exe.strip():
        defaults["executable_path"] = exe.strip()

    return defaults


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _safe_text(raw: str) -> str:
    parts = [p.strip() for p in (raw or "").splitlines() if p.strip()]
    return " ".join(parts)


def _collect_threads(page: Any, max_scan: int = 200) -> list[dict[str, Any]]:
    anchors = page.locator('a[href*="/t/"]')
    total = min(anchors.count(), max_scan)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for idx in range(total):
        item = anchors.nth(idx)
        try:
            if not item.is_visible():
                continue
            href = (item.get_attribute("href") or "").strip()
            if "/t/" not in href:
                continue
            label = _safe_text(item.get_attribute("aria-label") or "")
            text = _safe_text(item.inner_text() or "")
            name = label or text
            if not name:
                continue
            key = _norm(name)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"index": idx, "name": name, "href": href})
        except Exception:
            continue
    return out


def _pick_thread(threads: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    target = _norm(name)
    if not target:
        return None
    exact = [t for t in threads if _norm(str(t.get("name", ""))) == target]
    if exact:
        return exact[0]
    contains = [t for t in threads if target in _norm(str(t.get("name", "")))]
    if contains:
        return contains[0]
    return None


def _read_messages(page: Any, limit: int) -> list[str]:
    scope = page.locator('div[role="main"]').first
    selectors = (
        'div[role="row"] div[dir="auto"]',
        'div[dir="auto"]',
    )
    texts: list[str] = []

    for selector in selectors:
        try:
            loc = scope.locator(selector)
            count = min(loc.count(), 500)
            for idx in range(count):
                t = _safe_text(loc.nth(idx).inner_text() or "")
                if not t:
                    continue
                if texts and texts[-1] == t:
                    continue
                texts.append(t)
            if texts:
                break
        except Exception:
            continue

    if limit <= 0:
        return texts
    return texts[-limit:]


def _find_composer(page: Any) -> Any:
    selectors = (
        'div[role="textbox"][contenteditable="true"]',
        'div[aria-label*="Message"][contenteditable="true"]',
        'div[contenteditable="true"][data-lexical-editor="true"]',
    )
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                return loc
        except Exception:
            continue
    raise RuntimeError("Cannot find Messenger composer textbox")


def _ensure_logged_in(page: Any, profile_dir: str) -> None:
    url = page.url.lower()
    if "login" in url:
        raise RuntimeError(
            f"Not logged in in profile '{profile_dir}'. "
            "Login manually once in this profile directory."
        )
    if page.locator('input[name="email"]').count() > 0:
        raise RuntimeError(
            f"Login form detected in profile '{profile_dir}'. "
            "Use the same Playwright profile as ChatGPT Web (usually ~/.nanobot/playwright/chatgpt), "
            "or pass --profile-dir explicitly."
        )


def _run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        _jprint({"ok": False, "error": f"Playwright is required: {e}"})
        return 1

    profile_dir = str(Path(args.profile_dir).expanduser())
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    ctx = None
    try:
        launch_opts: dict[str, Any] = {
            "user_data_dir": profile_dir,
            "headless": bool(args.headless),
            "viewport": {"width": 1440, "height": 960},
            "args": ["--no-first-run", "--no-default-browser-check"],
        }
        if args.channel:
            launch_opts["channel"] = args.channel
        if args.executable_path:
            launch_opts["executable_path"] = args.executable_path

        ctx = pw.chromium.launch_persistent_context(**launch_opts)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.messenger.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        _ensure_logged_in(page, profile_dir)

        if args.command == "list-chats":
            threads = _collect_threads(page)
            chats = [{"name": t["name"], "href": t["href"]} for t in threads[: max(1, int(args.limit))]]
            _jprint({"ok": True, "count": len(chats), "chats": chats})
            return 0

        threads = _collect_threads(page)
        picked = _pick_thread(threads, args.name)
        if not picked:
            _jprint(
                {
                    "ok": False,
                    "error": f"Chat not found: {args.name}",
                    "available": [t["name"] for t in threads[:20]],
                }
            )
            return 2

        page.locator('a[href*="/t/"]').nth(int(picked["index"])).click(timeout=4000)
        page.wait_for_timeout(1200)

        if args.command == "read-chat":
            messages = _read_messages(page, limit=max(1, int(args.limit)))
            _jprint({"ok": True, "chat": picked["name"], "messages": messages})
            return 0

        if args.command == "send-message":
            if not args.approve_send:
                _jprint(
                    {
                        "ok": False,
                        "error": "Refused: send requires --approve-send for explicit confirmation.",
                    }
                )
                return 3
            text = (args.text or "").strip()
            if not text:
                _jprint({"ok": False, "error": "Missing --text"})
                return 4

            composer = _find_composer(page)
            composer.click(timeout=3000)
            page.keyboard.type(text, delay=0)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)

            _jprint({"ok": True, "chat": picked["name"], "sent": text})
            return 0

        _jprint({"ok": False, "error": f"Unknown command: {args.command}"})
        return 5
    except Exception as e:
        _jprint({"ok": False, "error": str(e)})
        return 1
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def _build_parser() -> argparse.ArgumentParser:
    defaults = _load_chatgpt_web_defaults()

    parser = argparse.ArgumentParser(description="Facebook Messenger web helper")
    parser.add_argument(
        "--profile-dir",
        default=defaults["profile_dir"],
        help="Playwright persistent profile dir",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument(
        "--channel",
        default=defaults["channel"],
        help="Browser channel (default from providers.chatgpt_web.browser_channel, fallback: chrome)",
    )
    parser.add_argument(
        "--executable-path",
        default=defaults["executable_path"],
        help="Custom browser executable (default from providers.chatgpt_web.executable_path)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-chats", help="List top conversations")
    p_list.add_argument("--limit", type=int, default=10)

    p_read = sub.add_parser("read-chat", help="Read latest messages in a chat")
    p_read.add_argument("--name", required=True, help="Chat display name")
    p_read.add_argument("--limit", type=int, default=20)

    p_send = sub.add_parser("send-message", help="Send one message to a chat")
    p_send.add_argument("--name", required=True, help="Chat display name")
    p_send.add_argument("--text", required=True, help="Message text")
    p_send.add_argument(
        "--approve-send",
        action="store_true",
        help="Explicit confirmation switch before sending",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
