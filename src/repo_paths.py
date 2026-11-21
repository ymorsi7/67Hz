"""Locate the GridSafe repo root and engine import paths (new + legacy layouts)."""

from __future__ import annotations

import sys
from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    """Walk parents until a directory containing ``pyproject.toml`` is found."""
    if start is None:
        cur = Path(__file__).resolve().parent
    else:
        p = Path(start).resolve()
        cur = p.parent if p.is_file() else p
    for candidate in [cur, *cur.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError("Could not find repository root (no pyproject.toml)")


def engine_path_entries(repo_root: Path | None = None) -> list[Path]:
    """Import roots for engine packages.

    Prefers ``src/`` (new layout). Also includes the repo root when legacy
    top-level packages exist, so a managed-agent sandbox on an older snapshot
    keeps working.
    """
    root = repo_root or find_repo_root()
    entries: list[Path] = []
    src = root / "src"
    if (src / "rules").is_dir():
        entries.append(src)
    if (root / "rules").is_dir():
        entries.append(root)
    if not entries:
        entries.append(src if src.is_dir() else root)
    if root not in entries:
        entries.append(root)
    return entries


def ensure_import_paths(start: Path | None = None) -> Path:
    """Put engine roots on ``sys.path`` (``src/`` first) and return the repo root."""
    root = find_repo_root(start)
    for path in reversed(engine_path_entries(root)):
        s = str(path)
        if s in sys.path:
            sys.path.remove(s)
        sys.path.insert(0, s)
    return root


def resolve_engine_file(*parts: str, start: Path | None = None) -> Path:
    """Resolve a file under an engine package for both layouts."""
    root = find_repo_root(start)
    for base in engine_path_entries(root):
        if base.name == "src" or (base / parts[0]).exists():
            candidate = base.joinpath(*parts)
            if candidate.is_file():
                return candidate
    return root / "src" / Path(*parts)


def resolve_engine_dir(*parts: str, start: Path | None = None) -> Path:
    """Resolve a directory under an engine package for both layouts."""
    root = find_repo_root(start)
    for base in engine_path_entries(root):
        candidate = base.joinpath(*parts)
        if candidate.is_dir():
            return candidate
    return root / "src" / Path(*parts)
