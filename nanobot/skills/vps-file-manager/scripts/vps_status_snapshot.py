#!/usr/bin/env python3
"""Generate a screenshot-style PNG snapshot for a headless VPS."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _cfg_get(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        if key in cur:
            cur = cur[key]
            continue
        camel = "".join(part.capitalize() if i else part for i, part in enumerate(key.split("_")))
        if camel in cur:
            cur = cur[camel]
            continue
        return None
    return cur


def _load_browser_defaults() -> dict[str, str]:
    defaults = {
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

    channel = _cfg_get(data, "providers", "chatgpt_web", "browser_channel")
    if isinstance(channel, str) and channel.strip():
        defaults["channel"] = channel.strip()

    exe = _cfg_get(data, "providers", "chatgpt_web", "executable_path")
    if isinstance(exe, str) and exe.strip():
        defaults["executable_path"] = exe.strip()
    return defaults


def _run_shell(command: str, timeout: int = 8) -> str:
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return "bash not found"
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s"
    except Exception as exc:
        return f"Command failed: {exc}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = out
    if err:
        combined = f"{combined}\n{err}".strip()
    if not combined:
        combined = "(no output)"
    return combined[:5000]


def _first_working(*commands: str) -> str:
    for command in commands:
        output = _run_shell(command)
        bad = ("not found", "command failed", "Timed out")
        if output and not any(token in output for token in bad):
            return output
    return _run_shell(commands[0]) if commands else "(no command)"


def _truncate_lines(text: str, limit: int = 14) -> str:
    lines = (text or "").splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit] + [f"... ({len(lines) - limit} more lines)"])


def _build_panels() -> list[dict[str, str]]:
    panels = [
        {
            "title": "Top CPU Processes",
            "body": _truncate_lines(
                _run_shell("ps -eo pid,pcpu,pmem,etime,comm,args --sort=-pcpu | head -n 14")
            ),
        },
        {
            "title": "Top Memory Processes",
            "body": _truncate_lines(
                _run_shell("ps -eo pid,pmem,pcpu,etime,comm,args --sort=-pmem | head -n 14")
            ),
        },
        {
            "title": "Disk Usage",
            "body": _truncate_lines(
                _first_working(
                    "df -h / ~/.nanobot 2>/dev/null",
                    "df -h /",
                ),
                limit=12,
            ),
        },
        {
            "title": "Memory",
            "body": _truncate_lines(
                _first_working(
                    "free -h",
                    "vm_stat",
                ),
                limit=16,
            ),
        },
        {
            "title": "Network Listeners",
            "body": _truncate_lines(
                _first_working(
                    "ss -tulpn | head -n 18",
                    "netstat -tulpn | head -n 18",
                    "lsof -i -P -n | head -n 18",
                ),
                limit=16,
            ),
        },
        {
            "title": "Sessions",
            "body": _truncate_lines(
                _first_working(
                    "tmux list-sessions 2>/dev/null || echo '(no tmux sessions)'",
                    "screen -ls 2>/dev/null || echo '(no screen sessions)'",
                    "who",
                ),
                limit=12,
            ),
        },
    ]
    return panels


def _build_html(title: str, host: str, generated_at: str, panels: list[dict[str, str]]) -> str:
    cards = []
    for panel in panels:
        cards.append(
            f"""
            <section class="card">
              <h2>{html.escape(panel["title"])}</h2>
              <pre>{html.escape(panel["body"])}</pre>
            </section>
            """
        )

    system_line = html.escape(platform.platform())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1020;
      --panel: #141c33;
      --panel-2: #1b2646;
      --text: #ecf2ff;
      --muted: #9ab0d8;
      --line: #2d3b67;
      --accent: #76e4c3;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 28px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background:
        radial-gradient(circle at top left, rgba(118,228,195,0.16), transparent 28%),
        linear-gradient(135deg, #0b1020 0%, #0f1630 45%, #111a38 100%);
      color: var(--text);
    }}
    .frame {{
      width: 1560px;
      margin: 0 auto;
      border: 1px solid var(--line);
      border-radius: 20px;
      overflow: hidden;
      background: rgba(8, 13, 27, 0.88);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.42);
    }}
    header {{
      padding: 24px 28px 18px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(118,228,195,0.08), rgba(255,255,255,0));
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 32px;
      line-height: 1.2;
    }}
    .meta {{
      display: flex;
      gap: 18px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 15px;
    }}
    .meta strong {{
      color: var(--accent);
      font-weight: 700;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      padding: 18px;
    }}
    .card {{
      min-height: 300px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: linear-gradient(180deg, var(--panel), var(--panel-2));
      overflow: hidden;
    }}
    .card h2 {{
      margin: 0;
      padding: 14px 16px;
      font-size: 16px;
      border-bottom: 1px solid var(--line);
      color: var(--accent);
    }}
    pre {{
      margin: 0;
      padding: 14px 16px 18px;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.45;
      font-size: 14px;
      color: var(--text);
    }}
  </style>
</head>
<body>
  <div class="frame">
    <header>
      <h1>{html.escape(title)}</h1>
      <div class="meta">
        <div><strong>Host</strong> {html.escape(host)}</div>
        <div><strong>Generated</strong> {html.escape(generated_at)}</div>
        <div><strong>Platform</strong> {system_line}</div>
      </div>
    </header>
    <main class="grid">
      {''.join(cards)}
    </main>
  </div>
</body>
</html>
"""


def _build_panel_html(title: str, host: str, generated_at: str, panel: dict[str, str]) -> str:
    system_line = html.escape(platform.platform())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)} - {html.escape(panel["title"])}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1020;
      --panel: #141c33;
      --panel-2: #1b2646;
      --text: #ecf2ff;
      --muted: #9ab0d8;
      --line: #2d3b67;
      --accent: #76e4c3;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background:
        radial-gradient(circle at top left, rgba(118,228,195,0.16), transparent 28%),
        linear-gradient(135deg, #0b1020 0%, #0f1630 45%, #111a38 100%);
      color: var(--text);
    }}
    .frame {{
      width: 1240px;
      margin: 0 auto;
      border: 1px solid var(--line);
      border-radius: 20px;
      overflow: hidden;
      background: rgba(8, 13, 27, 0.9);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.42);
    }}
    header {{
      padding: 22px 26px 16px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(118,228,195,0.08), rgba(255,255,255,0));
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 28px;
      line-height: 1.2;
    }}
    .meta {{
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 14px;
    }}
    .meta strong {{
      color: var(--accent);
      font-weight: 700;
    }}
    .card {{
      margin: 18px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: linear-gradient(180deg, var(--panel), var(--panel-2));
      overflow: hidden;
    }}
    .card h2 {{
      margin: 0;
      padding: 14px 16px;
      font-size: 20px;
      border-bottom: 1px solid var(--line);
      color: var(--accent);
    }}
    pre {{
      margin: 0;
      padding: 16px 18px 20px;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.5;
      font-size: 18px;
      color: var(--text);
    }}
  </style>
</head>
<body>
  <div class="frame">
    <header>
      <h1>{html.escape(title)}</h1>
      <div class="meta">
        <div><strong>Host</strong> {html.escape(host)}</div>
        <div><strong>Generated</strong> {html.escape(generated_at)}</div>
        <div><strong>Platform</strong> {system_line}</div>
      </div>
    </header>
    <section class="card">
      <h2>{html.escape(panel["title"])}</h2>
      <pre>{html.escape(panel["body"])}</pre>
    </section>
  </div>
</body>
</html>
"""


async def _render_pngs(
    jobs: list[tuple[Path, Path, dict[str, int]]],
    browser_channel: str,
    executable_path: str,
) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        launch_opts: dict[str, Any] = {
            "headless": True,
        }
        if browser_channel:
            launch_opts["channel"] = browser_channel
        if executable_path:
            launch_opts["executable_path"] = executable_path

        browser = await pw.chromium.launch(**launch_opts)
        try:
            for html_path, image_path, viewport in jobs:
                page = await browser.new_page(viewport=viewport, device_scale_factor=1)
                try:
                    await page.goto(html_path.as_uri(), wait_until="networkidle")
                    await page.screenshot(path=str(image_path), full_page=True)
                finally:
                    await page.close()
        finally:
            await browser.close()


def _default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.home() / ".nanobot" / "media" / "snapshots" / f"vps-status-{stamp}.png"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "panel"


def main() -> int:
    defaults = _load_browser_defaults()

    parser = argparse.ArgumentParser(description="Generate a screenshot-style VPS status PNG")
    parser.add_argument("--output", default=str(_default_output_path()), help="Output PNG path")
    parser.add_argument("--title", default="VPS Status Snapshot", help="Dashboard title")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    parser.add_argument(
        "--layout",
        choices=("dashboard", "panels", "both"),
        default="dashboard",
        help="Render one dashboard image, one image per panel, or both",
    )
    parser.add_argument(
        "--browser-channel",
        default=defaults["channel"],
        help="Playwright browser channel",
    )
    parser.add_argument(
        "--executable-path",
        default=defaults["executable_path"],
        help="Optional browser executable path",
    )
    args = parser.parse_args()

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = output_path.with_suffix(".html")
    panel_dir = output_path.with_suffix("")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host = platform.node() or "unknown-host"
    panels = _build_panels()
    jobs: list[tuple[Path, Path, dict[str, int]]] = []
    panel_assets: list[dict[str, str]] = []

    if args.layout in {"dashboard", "both"}:
        html_text = _build_html(args.title, host, generated_at, panels)
        html_path.write_text(html_text, encoding="utf-8")
        jobs.append((html_path, output_path, {"width": 1660, "height": 1800}))

    if args.layout in {"panels", "both"}:
        panel_dir.mkdir(parents=True, exist_ok=True)
        for panel in panels:
            slug = _slugify(panel["title"])
            panel_html_path = panel_dir / f"{slug}.html"
            panel_image_path = panel_dir / f"{slug}.png"
            panel_html_path.write_text(
                _build_panel_html(args.title, host, generated_at, panel),
                encoding="utf-8",
            )
            panel_assets.append({
                "title": panel["title"],
                "image_path": str(panel_image_path),
                "html_path": str(panel_html_path),
            })
            jobs.append((panel_html_path, panel_image_path, {"width": 1280, "height": 960}))

    try:
        asyncio.run(
            _render_pngs(
                jobs=jobs,
                browser_channel=args.browser_channel,
                executable_path=args.executable_path,
            )
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "html_path": str(html_path) if html_path.exists() else "",
            "panel_images": panel_assets,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    payload = {
        "ok": True,
        "image_path": str(output_path) if args.layout in {"dashboard", "both"} else "",
        "html_path": str(html_path) if args.layout in {"dashboard", "both"} else "",
        "host": host,
        "generated_at": generated_at,
        "panel_titles": [panel["title"] for panel in panels],
        "panel_images": panel_assets,
        "layout": args.layout,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if args.layout in {"dashboard", "both"}:
            print(f"image_path={output_path}")
        if panel_assets:
            print(f"panel_count={len(panel_assets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
