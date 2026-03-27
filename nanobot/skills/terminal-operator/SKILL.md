---
name: terminal-operator
description: Operate shell and terminal sessions on the VPS or current machine. Use when the user wants to open a terminal, run commands, inspect command output, check running processes, tail logs, or keep an interactive TTY session alive.
metadata: {"nanobot":{"emoji":"💻","os":["darwin","linux"],"requires":{"bins":[],"env":[]}}}
---

# Terminal Operator

Use this skill when the user asks to:
- open a terminal or shell
- run one or more shell commands
- inspect logs, processes, ports, services, or deployments from the command line
- keep an interactive CLI alive and come back to it later
- debug why a server/process is still running

## Core Rules

- Prefer `exec` for quick, non-interactive commands.
- `exec` runs on the same machine that hosts nanobot. If nanobot is deployed on the VPS, `exec` is already running on that VPS.
- Use short, verifiable commands first: `pwd`, `ls`, `rg`, `ps`, `df`, `du`, `tail`, `ss`, `lsof`.
- If approval gating is enabled, keep commands granular so the user can confirm each risky step.
- For destructive or state-changing commands, summarize the exact command before running it.
- If the task needs a real TTY or a long-lived interactive process, also use skill `tmux`.

## Default Workflow

1. Verify current directory or target path.
2. Inspect before changing anything.
3. Run one command at a time and read the output.
4. For deploy/debug flows, report what changed and what still needs checking.

## Good `exec` Patterns

- Current directory:
  - `exec(command="pwd")`
- Quick file scan:
  - `exec(command="rg --files . | head -n 200")`
- Running processes:
  - `exec(command="ps -eo pid,ppid,pcpu,pmem,etime,comm,args --sort=-pcpu | head -n 20")`
- Open ports:
  - `exec(command="ss -tulpn | head -n 30")`
  - fallback: `exec(command="lsof -i -P -n | head -n 30")`
- Recent logs:
  - `exec(command="tail -n 200 /path/to/log")`

## When To Switch To `tmux`

Use `tmux` when:
- the command expects keyboard input
- the process needs to stay alive after the current turn
- you need repeated capture of terminal output over time
- you want to monitor a deploy/build session

When switching:
- create an isolated tmux session/socket
- print the exact monitor commands for later capture/attach
- capture pane output after each important step

## Process Checks

For “what is running now?” requests:
- inspect processes with `ps`
- inspect listeners with `ss`/`lsof`
- inspect sessions with `tmux list-sessions`
- if the user wants a visual summary, combine this skill with `vps-file-manager`

## Response Rules

- Do not tell the user to run commands manually when `exec` or `tmux` can do it now.
- If a command fails, include the real stderr or exit reason.
- For deploy/server tasks, always finish with current status: running, failed, or waiting for confirmation.
