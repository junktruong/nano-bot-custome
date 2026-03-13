"""Utility functions for nanobot."""

import re
from pathlib import Path
from datetime import datetime


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """~/.nanobot data directory."""
    return ensure_dir(Path.home() / ".nanobot")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure workspace path. Defaults to ~/.nanobot/workspace."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')

def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files
    try:
        tpl = pkg_files("nanobot") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if item.name.endswith(".md"):
            _write(item, workspace / item.name)
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")
    (workspace / "skills").mkdir(exist_ok=True)
    added.extend(sync_builtin_skills(workspace))

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added


def sync_builtin_skills(workspace: Path) -> list[str]:
    """Sync bundled built-in skills to workspace/skills. Only creates missing files."""
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    builtin_root = Path(__file__).resolve().parent.parent / "skills"
    if not builtin_root.is_dir():
        return []

    added: list[str] = []
    for skill_dir in builtin_root.iterdir():
        if not skill_dir.is_dir():
            continue
        if not (skill_dir / "SKILL.md").exists():
            continue

        for src in skill_dir.rglob("*"):
            if src.is_dir():
                continue
            rel = src.relative_to(skill_dir)
            dest = skills_dir / skill_dir.name / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            try:
                added.append(str(dest.relative_to(workspace)))
            except Exception:
                added.append(str(dest))

    _migrate_legacy_skill_paths(skills_dir)
    return added


def _migrate_legacy_skill_paths(skills_dir: Path) -> None:
    """Best-effort migration for legacy script paths in copied skills."""
    skill_md = skills_dir / "facebook-messenger-assist" / "SKILL.md"
    if not skill_md.exists():
        return
    try:
        content = skill_md.read_text(encoding="utf-8")
        legacy = "nanobot/skills/facebook-messenger-assist/scripts/messenger_web.py"
        new = "skills/facebook-messenger-assist/scripts/messenger_web.py"
        if legacy in content:
            skill_md.write_text(content.replace(legacy, new), encoding="utf-8")
    except Exception:
        pass

    # Migrate old script default profile path to the shared ChatGPT web profile.
    script = skills_dir / "facebook-messenger-assist" / "scripts" / "messenger_web.py"
    if not script.exists():
        return
    try:
        s = script.read_text(encoding="utf-8")
        old = 'default="~/.nanobot/playwright/facebook"'
        new = 'default="~/.nanobot/playwright/chatgpt"'
        if old in s:
            script.write_text(s.replace(old, new), encoding="utf-8")
    except Exception:
        pass
