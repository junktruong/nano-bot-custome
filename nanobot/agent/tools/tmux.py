"""tmux session management tool for interactive terminals."""

import asyncio
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class TmuxTool(Tool):
    """Create and control detached tmux sessions for interactive commands."""

    def __init__(self, socket_dir: str | None = None):
        default_dir = os.environ.get("NANOBOT_TMUX_SOCKET_DIR") or os.path.join(
            tempfile.gettempdir(),
            "nanobot-tmux-sockets",
        )
        self.socket_dir = Path(socket_dir or default_dir).expanduser()

    @property
    def name(self) -> str:
        return "tmux"

    @property
    def description(self) -> str:
        return (
            "Run and monitor commands in a separate detached tmux terminal session. "
            "Use for interactive or real-time commands like codex, htop, top, watch, or tail -f."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "run",
                        "send",
                        "capture",
                        "list",
                        "interrupt",
                        "kill_session",
                        "kill_server",
                    ],
                    "description": "tmux action to perform",
                },
                "session_name": {
                    "type": "string",
                    "description": "tmux session name",
                },
                "command": {
                    "type": "string",
                    "description": "Command to run or send to the tmux session",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory inside the tmux shell before running the command",
                },
                "socket_name": {
                    "type": "string",
                    "description": "Logical socket name. Actual socket path is managed by nanobot.",
                },
                "window_name": {
                    "type": "string",
                    "description": "Optional tmux window name for a new session",
                },
                "lines": {
                    "type": "integer",
                    "description": "How many history lines to capture",
                    "minimum": 20,
                    "maximum": 5000,
                },
                "enter": {
                    "type": "boolean",
                    "description": "Whether to press Enter after sending the command",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        session_name: str | None = None,
        command: str | None = None,
        working_dir: str | None = None,
        socket_name: str | None = None,
        window_name: str | None = None,
        lines: int = 200,
        enter: bool = True,
        **kwargs: Any,
    ) -> str:
        del kwargs
        if shutil.which("tmux") is None:
            return "Error: tmux is not installed or not on PATH."

        act = (action or "").strip().lower()
        socket_path = self._socket_path(socket_name)
        if act == "list":
            return await self._list_sessions(socket_path)
        if act == "kill_server":
            return await self._run_tmux(socket_path, "kill-server")

        safe_session = self._sanitize_name(session_name or "")
        if not safe_session:
            return "Error: session_name is required for this tmux action."

        if act == "run":
            return await self._run_command(
                socket_path=socket_path,
                session_name=safe_session,
                command=(command or "").strip(),
                working_dir=(working_dir or "").strip() or None,
                window_name=self._sanitize_name(window_name or "") or "shell",
                lines=lines,
                enter=enter,
            )
        if act == "send":
            if not command:
                return "Error: command is required for tmux send."
            await self._send_literal(socket_path, safe_session, command)
            if enter:
                await self._run_tmux(socket_path, "send-keys", "-t", f"{safe_session}:0.0", "Enter")
            return await self._capture(socket_path, safe_session, lines)
        if act == "capture":
            return await self._capture(socket_path, safe_session, lines)
        if act == "interrupt":
            await self._run_tmux(socket_path, "send-keys", "-t", f"{safe_session}:0.0", "C-c")
            return await self._capture(socket_path, safe_session, lines)
        if act == "kill_session":
            return await self._run_tmux(socket_path, "kill-session", "-t", safe_session)

        return f"Error: Unsupported tmux action '{action}'."

    def _socket_path(self, socket_name: str | None) -> Path:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        safe_socket = self._sanitize_name(socket_name or "nanobot") or "nanobot"
        return self.socket_dir / f"{safe_socket}.sock"

    @staticmethod
    def _sanitize_name(raw: str) -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "-", (raw or "").strip())
        return value.strip("-.")[:64]

    async def _run_command(
        self,
        socket_path: Path,
        session_name: str,
        command: str,
        working_dir: str | None,
        window_name: str,
        lines: int,
        enter: bool,
    ) -> str:
        created = False
        has_session = await self._run_tmux(socket_path, "has-session", "-t", session_name, check=False)
        if "Exit code: 1" in has_session or has_session.startswith("Error"):
            created = True
            created_result = await self._run_tmux(
                socket_path,
                "new-session",
                "-d",
                "-s",
                session_name,
                "-n",
                window_name,
            )
            if created_result.startswith("Error"):
                return created_result

        if working_dir:
            await self._send_literal(
                socket_path,
                session_name,
                f"cd {shlex.quote(working_dir)}",
            )
            await self._run_tmux(socket_path, "send-keys", "-t", f"{session_name}:0.0", "Enter")

        if command:
            await self._send_literal(socket_path, session_name, command)
            if enter:
                await self._run_tmux(socket_path, "send-keys", "-t", f"{session_name}:0.0", "Enter")

        captured = await self._capture(socket_path, session_name, lines)
        monitor = self._monitor_instructions(socket_path, session_name, lines)
        status = "created" if created else "reused"
        return (
            f"tmux session {status}: {session_name}\n"
            f"socket: {socket_path}\n"
            f"monitor:\n{monitor}\n\n"
            f"recent output:\n{captured}"
        )

    async def _list_sessions(self, socket_path: Path) -> str:
        result = await self._run_tmux(
            socket_path,
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_windows}\t#{session_attached}",
            check=False,
        )
        if "failed to connect to server" in result.lower() or "Exit code: 1" in result:
            return f"No tmux server running on socket {socket_path}"
        return result

    async def _capture(self, socket_path: Path, session_name: str, lines: int) -> str:
        return await self._run_tmux(
            socket_path,
            "capture-pane",
            "-p",
            "-J",
            "-t",
            f"{session_name}:0.0",
            "-S",
            f"-{max(20, int(lines))}",
        )

    async def _send_literal(self, socket_path: Path, session_name: str, command: str) -> str:
        return await self._run_tmux(
            socket_path,
            "send-keys",
            "-t",
            f"{session_name}:0.0",
            "-l",
            "--",
            command,
        )

    @staticmethod
    def _monitor_instructions(socket_path: Path, session_name: str, lines: int) -> str:
        return (
            f"  tmux -S {shlex.quote(str(socket_path))} attach -t {shlex.quote(session_name)}\n"
            f"  tmux -S {shlex.quote(str(socket_path))} capture-pane -p -J -t "
            f"{shlex.quote(session_name)}:0.0 -S -{max(20, int(lines))}"
        )

    async def _run_tmux(
        self,
        socket_path: Path,
        *args: str,
        check: bool = True,
    ) -> str:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            str(socket_path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = ""
        if stdout:
            output += stdout.decode("utf-8", errors="replace")
        if stderr:
            err = stderr.decode("utf-8", errors="replace")
            if err.strip():
                if output:
                    output += "\n"
                output += f"STDERR:\n{err}"
        output = output.strip() or "(no output)"
        if check and process.returncode != 0:
            return f"Error: tmux {' '.join(args)} failed.\n{output}\nExit code: {process.returncode}"
        if not check and process.returncode != 0:
            return f"{output}\nExit code: {process.returncode}"
        return output
