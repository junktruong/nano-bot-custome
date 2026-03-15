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

## Core Rules

- Verify paths first with `list_dir`, `read_file`, or `exec`.
- For precise file edits, prefer built-in file tools over shell.
- For copy/move/archive/search/disk-usage operations, use `exec`.
- For image delivery, send the local path with `message(media=[...])`.
- For a visual VPS status check, generate a PNG snapshot first, then send that PNG.

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

`python3 skills/vps-file-manager/scripts/vps_status_snapshot.py --json`

The script returns JSON with:
- `image_path`
- `html_path`
- `host`
- `generated_at`

Then send the PNG to the current user:

`message(content="Day la anh chup trang thai VPS hien tai.", media=["<image_path>"])`

## Response Rules

- If the user asks "VPS dang nhu nao", generate the snapshot image instead of replying with text only.
- If sending media fails, report the exact transport reason.
- If the user asks only for text status, you can summarize the same command outputs without generating the PNG.
