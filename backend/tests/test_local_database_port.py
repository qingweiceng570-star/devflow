import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

port_setup = SimpleNamespace(**runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/local_database_port.py")))


def config(tmp_path):
    path = tmp_path / ".env"
    path.write_text("POSTGRES_PORT=55432\nDATABASE_URL=postgresql+psycopg://user:keep-secret@localhost:55432/db\nLLM_API_KEY=keep-model-key\n")
    return path


def test_reserved_port_updates_only_local_port_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = config(tmp_path)
    before = path.read_text()
    result = port_setup.ensure_local_port(path, probe=lambda port: "reserved" if port == 55432 else "available")
    assert result == (55432, 15432, "changed")
    assert path.read_text() == before.replace("55432", "15432")


def test_check_or_occupied_port_never_changes_configuration(tmp_path):
    path = config(tmp_path)
    before = path.read_bytes()
    port_setup.ensure_local_port(path, check=True, probe=lambda _: "reserved")
    assert path.read_bytes() == before
    port_setup.ensure_local_port(path, probe=lambda _: "occupied")
    assert path.read_bytes() == before


def test_remote_database_is_never_rewritten(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = config(tmp_path)
    path.write_text(path.read_text().replace("localhost", "database.example.com"))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="non-local"):
        port_setup.ensure_local_port(path, probe=lambda _: "reserved")
    assert path.read_bytes() == before
