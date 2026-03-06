# nanobot Skills

This directory contains built-in skills that extend nanobot's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

## Attribution

These skills are adapted from [OpenClaw](https://github.com/openclaw/openclaw)'s skill system.
The skill format and metadata structure follow OpenClaw's conventions to maintain compatibility.

## Available Skills

| Skill | Description |
|-------|-------------|
| `github` | Interact with GitHub using the `gh` CLI |
| `weather` | Get weather info using wttr.in and Open-Meteo |
| `summarize` | Summarize URLs, files, and YouTube videos |
| `tmux` | Remote-control tmux sessions |
| `clawhub` | Search and install skills from ClawHub registry |
| `skill-creator` | Create new skills |
| `memory` | Persist long-term facts and search event history |
| `cron` | Schedule reminders and recurring tasks |
| `research` | Research deeply, publish to Google Docs, return link |
| `schedule-manager` | Full reminder CRUD, hourly/weekday scheduling, and day/week/month listing |
| `daily-ops` | Run recurring daily workflows with supervision and reporting |
| `google-workspace` | Execute Docs/Sheets/Drive actions via extension workers |
| `facebook-messenger-assist` | Assist Messenger chat workflow (list/read/draft/send with explicit approval) |
| `skill-checker` | Check discovered skills, availability status, and missing requirements |
