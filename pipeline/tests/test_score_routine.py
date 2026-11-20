import contextlib
import datetime as dt
from types import SimpleNamespace as NS

from itn import cli
from itn.analyze import data, stages
from itn.analyze.data import Item


class Conn:
    def __init__(self, unscored):
        self.unscored, self.scores, self.analyses = unscored, [], []

    def execute(self, q, p=()):
        if q.startswith("SELECT id FROM artifact WHERE in_scope AND NOT EXISTS"):
            return NS(fetchall=lambda: [(i,) for i in self.unscored])
        if q.startswith("INSERT INTO analysis"):
            self.analyses.append(p)
            return NS(fetchone=lambda: (7,))
        if q.startswith("INSERT INTO score"):
            self.scores.append(p)
        return NS(fetchone=lambda: None, fetchall=lambda: [])

    def commit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _item(aid, title, typ="executive_document", subtype="proclamation"):
    return Item(id=aid, type=typ, subtype=subtype, title=title, effective_date=dt.date(2026, 11, 10),
                summary="", metadata={})


def test_score_routine_scores_only_routine_items(monkeypatch, capsys):
    conn = Conn(["fr/1", "fr/2", "scotus/1-us-1"])
    monkeypatch.setattr(cli.db, "connect", lambda: conn)
    monkeypatch.setattr(stages.db, "connect", lambda: conn)
    monkeypatch.setattr(data, "load_items", lambda c, ids: [
        _item("fr/1", "Veterans Day, 2026"),
        _item("fr/2", "Establishment of the Oval Office National Monument"),
        _item("scotus/1-us-1", "A v. B", typ="scotus_case", subtype="opinion"),
    ])
    cli.main(["score-routine"])
    scored = {p[0] for p in conn.scores}
    assert scored == {"fr/1"}
    assert all(p[5] is True for p in conn.scores)          # current, shown on the site
    assert "scored by rule now: 1; left for the model: 2" in capsys.readouterr().out


def test_analyze_unscored_with_nothing_left_succeeds(monkeypatch, capsys):
    conn = Conn([])
    monkeypatch.setattr(cli.db, "connect", lambda: conn)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    cli.main(["analyze", "--unscored", "--budget", "1", "--profile", "economy"])
    assert "Nothing to score" in capsys.readouterr().out
