---
name: facebook-messenger-assist
description: Assist Facebook Messenger conversations from a user-logged browser profile: list top chats, open a selected person, read latest messages, draft style-based replies, and send only with explicit per-message user confirmation.
metadata: {"nanobot":{"emoji":"💬","requires":{"bins":["python3"],"env":[]}}}
---

# Facebook Messenger Assist

Use this skill to help the user manage Messenger chats from their own logged-in browser profile.

## Safety And Operating Rules

- Do not run unattended auto-chat loops that impersonate the user.
- Do not send messages without explicit user confirmation for each outgoing message.
- Always draft first, then wait for user approval before sending.
- If user asks for fully autonomous impersonation, refuse and keep human-in-the-loop mode.

## Script

Use script:

`nanobot/skills/facebook-messenger-assist/scripts/messenger_web.py`

It supports:

- `list-chats`: list top conversations
- `read-chat`: open a chat by name and read latest messages
- `send-message`: send one message (requires `--approve-send`)

## Standard Workflow

1. List first 10 chats:
- `python3 nanobot/skills/facebook-messenger-assist/scripts/messenger_web.py list-chats --limit 10`
2. Ask user to pick one exact name from the list.
3. Read latest messages:
- `python3 nanobot/skills/facebook-messenger-assist/scripts/messenger_web.py read-chat --name "Nguyen Van A" --limit 20`
4. Ask user for conversation style and intent.
5. Draft reply in that style.
6. Send only after user confirms:
- `python3 nanobot/skills/facebook-messenger-assist/scripts/messenger_web.py send-message --name "Nguyen Van A" --text "..." --approve-send`
7. Repeat read -> draft -> confirm -> send.

## Notes

- User must login manually once in browser profile.
- Default profile directory: `~/.nanobot/playwright/facebook`.
- If script cannot find selectors, report exact failure and stop; do not fake a success.
