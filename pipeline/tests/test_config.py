from pathlib import Path

from itn.config import migrations_dir


def test_migrations_found_from_repo_root(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(repo)
    monkeypatch.delenv("ITN_MIGRATIONS_DIR", raising=False)
    d = migrations_dir()
    assert d == repo / "schema" / "migrations"
    assert any(d.glob("*.sql"))


def test_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ITN_MIGRATIONS_DIR", str(tmp_path))
    assert migrations_dir() == tmp_path
