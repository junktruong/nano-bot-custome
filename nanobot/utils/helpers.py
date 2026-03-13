"""Utility functions for nanobot."""

import hashlib
import json
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
    """Sync bundled templates to workspace."""
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
            if name.startswith("[updated] "):
                Console().print(f"  [dim]Updated {name[len('[updated] '):]}[/dim]")
            else:
                Console().print(f"  [dim]Created {name}[/dim]")
    return added


def sync_builtin_skills(workspace: Path) -> list[str]:
    """Sync bundled built-in skills to workspace/skills.

    Strategy:
    - Create missing files.
    - Auto-update managed files when package source changes.
    - Preserve local overrides when file content diverged after prior sync.
    - To fully opt out for a skill, create `skills/<skill-name>/.nanobot-local`.
    """
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    builtin_root = Path(__file__).resolve().parent.parent / "skills"
    if not builtin_root.is_dir():
        return []

    added: list[str] = []
    manifest = _load_builtin_skills_manifest(skills_dir)
    seen: set[str] = set()
    for skill_dir in builtin_root.iterdir():
        if not skill_dir.is_dir():
            continue
        if not (skill_dir / "SKILL.md").exists():
            continue
        skill_name = skill_dir.name
        local_override = (skills_dir / skill_name / ".nanobot-local").exists()

        for src in skill_dir.rglob("*"):
            if src.is_dir():
                continue
            rel = src.relative_to(skill_dir)
            dest = skills_dir / skill_name / rel
            key = f"{skill_name}/{rel.as_posix()}"
            seen.add(key)

            src_bytes = src.read_bytes()
            src_hash = _sha256_bytes(src_bytes)
            prev_hash = manifest.get(key)

            if dest.exists():
                dest_hash = _sha256_bytes(dest.read_bytes())
                if dest_hash == src_hash:
                    manifest[key] = src_hash
                    continue

                # Safe auto-update when file was previously synced and unchanged locally.
                can_auto_update = (not local_override) and (prev_hash is None or dest_hash == prev_hash)
                if can_auto_update:
                    dest.write_bytes(src_bytes)
                    manifest[key] = src_hash
                    try:
                        added.append(f"[updated] {dest.relative_to(workspace)}")
                    except Exception:
                        added.append(f"[updated] {dest}")
                    continue

                # Preserve local version; seed manifest for future comparisons.
                if prev_hash is None:
                    manifest[key] = dest_hash
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src_bytes)
            manifest[key] = src_hash
            try:
                added.append(str(dest.relative_to(workspace)))
            except Exception:
                added.append(str(dest))

    manifest = {k: v for k, v in manifest.items() if k in seen}
    _save_builtin_skills_manifest(skills_dir, manifest)
    _migrate_legacy_skill_paths(skills_dir)
    return added


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_builtin_skills_manifest(skills_dir: Path) -> dict[str, str]:
    path = skills_dir / ".builtin_sync_manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data.get("files", {})
        if isinstance(files, dict):
            return {
                str(k): str(v)
                for k, v in files.items()
                if isinstance(k, str) and isinstance(v, str)
            }
    except Exception:
        return {}
    return {}


def _save_builtin_skills_manifest(skills_dir: Path, manifest: dict[str, str]) -> None:
    path = skills_dir / ".builtin_sync_manifest.json"
    payload = {"version": 1, "files": manifest}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


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
