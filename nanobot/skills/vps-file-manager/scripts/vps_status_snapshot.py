#!/usr/bin/env python3
"""Generate a screenshot-style PNG snapshot for a headless VPS."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
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
    return _pick_snapshot_dir() / f"vps-status-{stamp}.png"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "panel"


def _pick_snapshot_dir() -> Path:
    candidates: list[Path] = []
    if raw := os.environ.get("NANOBOT_SNAPSHOT_DIR"):
        candidates.append(Path(raw).expanduser())
    candidates.append(Path.home() / ".nanobot" / "media" / "snapshots")
    candidates.append(Path(tempfile.gettempdir()) / "nanobot" / "media" / "snapshots")
    candidates.append(Path.cwd() / ".nanobot-snapshots")

    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return base
        except Exception:
            continue
    return Path.cwd()


def _load_pillow_font(size: int):
    from PIL import ImageFont

    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
        "/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
    ]
    for candidate in font_candidates:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_size(draw: Any, text: str, font: Any, spacing: int = 4) -> tuple[int, int]:
    sample = text or " "
    bbox = draw.multiline_textbbox((0, 0), sample, font=font, spacing=spacing)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _render_dashboard_with_pillow(
    image_path: Path,
    title: str,
    host: str,
    generated_at: str,
    panels: list[dict[str, str]],
) -> None:
    from PIL import Image, ImageDraw

    width = 1600
    outer_pad = 28
    inner_gap = 18
    header_gap = 14
    text_spacing = 5
    bg = "#0b1020"
    frame_bg = "#10182f"
    panel_bg = "#17213d"
    line = "#2d3b67"
    text = "#ecf2ff"
    muted = "#9ab0d8"
    accent = "#76e4c3"

    title_font = _load_pillow_font(28)
    meta_font = _load_pillow_font(16)
    card_title_font = _load_pillow_font(20)
    body_font = _load_pillow_font(16)

    dummy = Image.new("RGB", (width, 10), bg)
    dummy_draw = ImageDraw.Draw(dummy)
    _, title_h = _text_size(dummy_draw, title, title_font)
    meta_lines = [
        f"Host: {host}",
        f"Generated: {generated_at}",
        f"Platform: {platform.platform()}",
    ]
    _, meta_h = _text_size(dummy_draw, "\n".join(meta_lines), meta_font, spacing=text_spacing)
    header_h = 28 + title_h + header_gap + meta_h + 24

    card_width = (width - outer_pad * 2 - inner_gap) // 2
    body_heights: list[int] = []
    card_heights: list[int] = []
    for panel in panels:
        _, card_title_h = _text_size(dummy_draw, panel["title"], card_title_font)
        _, body_h = _text_size(dummy_draw, panel["body"], body_font, spacing=text_spacing)
        body_heights.append(body_h)
        card_heights.append(22 + card_title_h + 18 + body_h + 24)

    row_heights: list[int] = []
    for i in range(0, len(card_heights), 2):
        row_heights.append(max(card_heights[i:i + 2]))
    content_h = sum(row_heights) + inner_gap * max(0, len(row_heights) - 1)
    height = outer_pad + header_h + 18 + content_h + outer_pad

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((16, 16, width - 16, height - 16), radius=18, fill=frame_bg, outline=line, width=2)

    header_y = outer_pad + 6
    draw.text((outer_pad + 12, header_y), title, font=title_font, fill=text)
    meta_y = header_y + title_h + header_gap
    draw.multiline_text((outer_pad + 12, meta_y), "\n".join(meta_lines), font=meta_font, fill=muted, spacing=text_spacing)

    y = outer_pad + header_h + 18
    for row_index in range(0, len(panels), 2):
        row_height = row_heights[row_index // 2]
        row = panels[row_index:row_index + 2]
        for col_index, panel in enumerate(row):
            x = outer_pad + col_index * (card_width + inner_gap)
            draw.rounded_rectangle(
                (x, y, x + card_width, y + row_height),
                radius=16,
                fill=panel_bg,
                outline=line,
                width=2,
            )
            draw.text((x + 18, y + 16), panel["title"], font=card_title_font, fill=accent)
            draw.multiline_text(
                (x + 18, y + 52),
                panel["body"],
                font=body_font,
                fill=text,
                spacing=text_spacing,
            )
        y += row_height + inner_gap

    image.save(image_path)


def _render_panel_with_pillow(
    image_path: Path,
    title: str,
    host: str,
    generated_at: str,
    panel: dict[str, str],
) -> None:
    from PIL import Image, ImageDraw

    width = 1280
    outer_pad = 28
    text_spacing = 6
    bg = "#0b1020"
    frame_bg = "#10182f"
    panel_bg = "#17213d"
    line = "#2d3b67"
    text = "#ecf2ff"
    muted = "#9ab0d8"
    accent = "#76e4c3"

    title_font = _load_pillow_font(28)
    meta_font = _load_pillow_font(16)
    card_title_font = _load_pillow_font(22)
    body_font = _load_pillow_font(18)

    dummy = Image.new("RGB", (width, 10), bg)
    dummy_draw = ImageDraw.Draw(dummy)
    _, title_h = _text_size(dummy_draw, title, title_font)
    meta_lines = [
        f"Host: {host}",
        f"Generated: {generated_at}",
        f"Platform: {platform.platform()}",
    ]
    _, meta_h = _text_size(dummy_draw, "\n".join(meta_lines), meta_font, spacing=text_spacing)
    _, body_h = _text_size(dummy_draw, panel["body"], body_font, spacing=text_spacing)
    height = outer_pad * 2 + title_h + meta_h + body_h + 128

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((16, 16, width - 16, height - 16), radius=18, fill=frame_bg, outline=line, width=2)
    draw.text((outer_pad + 10, outer_pad), title, font=title_font, fill=text)
    draw.multiline_text(
        (outer_pad + 10, outer_pad + title_h + 14),
        "\n".join(meta_lines),
        font=meta_font,
        fill=muted,
        spacing=text_spacing,
    )

    card_top = outer_pad + title_h + meta_h + 38
    draw.rounded_rectangle(
        (outer_pad, card_top, width - outer_pad, height - outer_pad),
        radius=16,
        fill=panel_bg,
        outline=line,
        width=2,
    )
    draw.text((outer_pad + 18, card_top + 16), panel["title"], font=card_title_font, fill=accent)
    draw.multiline_text(
        (outer_pad + 18, card_top + 58),
        panel["body"],
        font=body_font,
        fill=text,
        spacing=text_spacing,
    )
    image.save(image_path)


def _render_with_pillow(
    output_path: Path,
    panel_dir: Path,
    title: str,
    host: str,
    generated_at: str,
    panels: list[dict[str, str]],
    layout: str,
) -> None:
    try:
        import PIL  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "No PNG renderer available. Install playwright+browser or pillow."
        ) from exc

    if layout in {"dashboard", "both"}:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _render_dashboard_with_pillow(output_path, title, host, generated_at, panels)
    if layout in {"panels", "both"}:
        panel_dir.mkdir(parents=True, exist_ok=True)
        for panel in panels:
            slug = _slugify(panel["title"])
            panel_image_path = panel_dir / f"{slug}.png"
            _render_panel_with_pillow(panel_image_path, title, host, generated_at, panel)


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
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        fallback_output = (_pick_snapshot_dir() / output_path.name).resolve()
        output_path = fallback_output
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

    renderer = "playwright"
    renderer_error = ""
    try:
        asyncio.run(
            _render_pngs(
                jobs=jobs,
                browser_channel=args.browser_channel,
                executable_path=args.executable_path,
            )
        )
    except Exception as exc:
        renderer = "pillow"
        renderer_error = str(exc)
        try:
            _render_with_pillow(
                output_path=output_path,
                panel_dir=panel_dir,
                title=args.title,
                host=host,
                generated_at=generated_at,
                panels=panels,
                layout=args.layout,
            )
        except Exception as pillow_exc:
            combined = f"{renderer_error}; pillow fallback failed: {pillow_exc}"
            fix_hint = (
                "Install playwright with a browser, or install pillow for headless PNG rendering."
            )
            payload = {
                "ok": False,
                "error": combined,
                "fix_hint": fix_hint,
                "html_path": str(html_path) if html_path.exists() else "",
                "panel_images": panel_assets,
                "layout": args.layout,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 1

    if renderer_error and renderer == "pillow":
        payload = {
            "ok": True,
            "warning": renderer_error,
            "renderer": renderer,
        }
    else:
        payload = {
            "ok": True,
            "renderer": renderer,
        }
    payload.update({
        "image_path": str(output_path) if args.layout in {"dashboard", "both"} else "",
        "html_path": str(html_path) if args.layout in {"dashboard", "both"} else "",
        "host": host,
        "generated_at": generated_at,
        "panel_titles": [panel["title"] for panel in panels],
        "panel_images": panel_assets,
        "layout": args.layout,
    })
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
