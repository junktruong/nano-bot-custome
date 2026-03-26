---
name: vps-file-manager
description: Manage files, directories, archives, and image attachments on the VPS. Use when the user wants to inspect, move, copy, rename, summarize, or send files, or when they ask to check VPS status with a screenshot-style image of running tasks and system health.
metadata: {"nanobot":{"emoji":"🗂️","requires":{"bins":["python3"],"env":[]}}}
---

# VPS File Manager

Use this skill when the user asks to:
- inspect or manage files/directories on the VPS
- find large files or check disk usage
- send a local image/file back through the current chat
- check VPS status with a screenshot/image of running tasks
- prepare or verify VPS-side code/deploy steps, especially when the user wants to review each shell action before it runs

## Core Rules

- Verify paths first with `list_dir`, `read_file`, or `exec`.
- If `tools.exec.ssh_target` is configured, assume `exec` is already targeting the VPS over SSH; otherwise it inspects the host running nanobot.
- For precise file edits, prefer built-in file tools over shell.
- For copy/move/archive/search/disk-usage operations, use `exec`.
- For image delivery, send the local path with `message(media=[...])`.
- For a visual VPS status check, generate a PNG snapshot first, then send that PNG.
- If the current channel is Zalo, verify webhook mode + a public `channels.zalo.webhookUrl` before relying on local image delivery.
- For operational shell actions on VPS, keep steps granular so the user can confirm each risky step when approval gating is enabled.

## Common File Ops

- List a directory:
  - `list_dir(path=".")`
- Find files quickly:
  - `exec(command="rg --files . | head -n 200")`
- Check disk usage:
  - `exec(command="du -sh . ~/.nanobot 2>/dev/null")`
- Move/copy/archive:
  - `exec(command="mv ...")`
  - `exec(command="cp ...")`
  - `exec(command="zip -r ...")`
  - `exec(command="unzip ...")`

## VPS Snapshot Workflow

Run:

Dashboard image:

`python3 skills/vps-file-manager/scripts/vps_status_snapshot.py --json --layout dashboard`

Per-panel images (better for Zalo/mobile readability):

`python3 skills/vps-file-manager/scripts/vps_status_snapshot.py --json --layout panels`

Both dashboard + panels:

`python3 skills/vps-file-manager/scripts/vps_status_snapshot.py --json --layout both`

The script returns JSON with:
- `image_path`
- `html_path`
- `panel_images` (list of `{title, image_path, html_path}`)
- `host`
- `generated_at`

Then send the PNG to the current user:

`message(content="Day la anh chup trang thai VPS hien tai.", media=["<image_path>"])`

If using per-panel output, send the panel PNGs in order:

`message(content="Day la tung anh trang thai VPS de de doc tren dien thoai.", media=["<panel_1>", "<panel_2>", ...])`

## Response Rules

- If the user asks "VPS dang nhu nao", generate the snapshot image instead of replying with text only.
- On Zalo or small-screen channels, prefer `--layout panels` unless the user explicitly asks for one dashboard image.
- If sending media fails, report the exact transport reason.
- If the user asks only for text status, you can summarize the same command outputs without generating the PNG.
