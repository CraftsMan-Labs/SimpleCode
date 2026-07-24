"""Tests for the cocoindex-code CLI bridge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from simplecode.cocoindex import CocoIndexError, ccc_bin, write_context_file


def test_ccc_bin_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simplecode.cocoindex.shutil.which", lambda _: None)
    with pytest.raises(CocoIndexError, match="ccc not found"):
        ccc_bin()


def test_write_context_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simplecode.cocoindex.shutil.which", lambda _: "/usr/bin/ccc")

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "src/auth.py:42 login()\n"
    proc.stderr = ""
    monkeypatch.setattr("simplecode.cocoindex.subprocess.run", lambda *a, **k: proc)

    dest = tmp_path / "ctx.md"
    write_context_file(tmp_path, "login", dest=dest)
    text = dest.read_text()
    assert "Query: login" in text
    assert "src/auth.py" in text
