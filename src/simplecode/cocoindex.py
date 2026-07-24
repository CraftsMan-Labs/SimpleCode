"""Thin bridge to cocoindex-code's ``ccc`` CLI (optional extra)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from simplecode.errors import SimpleCodeError


class CocoIndexError(SimpleCodeError):
    """cocoindex-code / ccc is missing or failed."""


def ccc_bin() -> str:
    path = shutil.which("ccc")
    if not path:
        raise CocoIndexError(
            "ccc not found. Install cocoindex support with:\n"
            "  pip install 'simplecode[code]'\n"
            "Then run once in the target repo: ccc init && ccc index"
        )
    return path


def run_ccc(
    args: list[str],
    *,
    cwd: Path,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = [ccc_bin(), *args]
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=capture,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CocoIndexError(
            f"ccc {' '.join(args)} failed (exit {result.returncode})"
            + (f":\n{detail}" if detail else "")
        )
    return result


def index_codebase(working_dir: Path) -> None:
    """Incremental index of *working_dir* (auto-inits on first use when interactive)."""
    run_ccc(["index"], cwd=working_dir)


def search_codebase(
    working_dir: Path,
    query: str,
    *,
    refresh: bool = False,
    limit: int = 10,
) -> str:
    """Semantic search; returns raw ``ccc search`` stdout."""
    args = ["search", query, "--limit", str(limit)]
    if refresh:
        args.append("--refresh")
    result = run_ccc(args, cwd=working_dir, capture=True)
    return result.stdout or ""


def write_context_file(
    working_dir: Path,
    query: str,
    *,
    dest: Path,
    refresh: bool = False,
    limit: int = 10,
) -> Path:
    """Search and write a markdown context pack for pipeline ``-i`` injection."""
    body = search_codebase(working_dir, query, refresh=refresh, limit=limit)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        f"# Code context (cocoindex)\n\nQuery: {query}\n\n{body}",
        encoding="utf-8",
    )
    return dest
