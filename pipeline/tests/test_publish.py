from itn import ids
from itn.publish.export import band, percentiles


def test_percentiles_span_0_to_100():
    p = percentiles({"a": 0.1, "b": 0.5, "c": 0.9})
    assert p == {"a": 0.0, "b": 50.0, "c": 100.0}


def test_single_score_is_midpoint():
    assert percentiles({"a": 0.3}) == {"a": 50.0}


def test_bands():
    assert band(95) == "routine"
    assert band(60) == "ordinary"
    assert band(45) == "unusual"
    assert band(20) == "novel"
    assert band(3) == "unprecedented"


def test_ids():
    assert ids.public_law(117, 169) == "pl/117-169"
    assert ids.scotus_case(597, 215) == "scotus/597-us-215"
    assert ids.usc_section(42, "1983", 2003) == "usc/42/1983@2003"


def test_r2_settings_lists_every_missing_name(monkeypatch):
    import pytest
    from itn.config import MissingSetting, R2Settings

    monkeypatch.setenv("R2_BUCKET", "b")
    for n in ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]:
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(MissingSetting, match="R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"):
        R2Settings.from_env()


def test_build_details_links_opinions_and_citations():
    from datetime import date

    from itn.publish.export import build_details

    class Cur:
        def __init__(self, rows):
            self.rows = rows

        def __iter__(self):
            return iter(self.rows)

        def fetchall(self):
            return self.rows

    class Conn:
        def execute(self, sql, *_):
            if "FROM artifact WHERE in_scope" in sql:
                return Cur([("scotus/576-us-644", "scotus_case", "opinion", "Obergefell v. Hodges",
                             date(2015, 6, 26), True, None, [], None),
                            ("scotus/381-us-479", "scotus_case", "opinion", "Griswold v. Connecticut",
                             date(1965, 6, 7), False, None, [], None)])
            if "FROM component" in sql:
                return Cur([("scotus/576-us-644#0-majority", "scotus/576-us-644", "majority", "Kennedy",
                             [], "text/scotus/576-us-644/0-majority.txt")])
            if "SELECT id, title FROM artifact" in sql:
                return Cur([("scotus/576-us-644", "Obergefell v. Hodges"),
                            ("scotus/381-us-479", "Griswold v. Connecticut")])
            return Cur([("scotus/576-us-644#0-majority", "scotus/576-us-644", "scotus/381-us-479",
                         "485", "381 U.S. 479"),
                        ("scotus/576-us-644#0-majority", "scotus/576-us-644", "usc/42/1983", None,
                         "42 U.S.C. § 1983")])

        def commit(self):
            pass

    d = build_details(Conn())
    o = d["scotus/576-us-644"]
    assert o["opinions"][0]["text"] == "text/scotus/576-us-644/0-majority.txt"
    assert [c["title"] for c in o["cites"]] == ["Griswold v. Connecticut", None]
    assert d["scotus/381-us-479"]["cited_by"] == [{"id": "scotus/576-us-644", "title": "Obergefell v. Hodges"}]


def test_changed_details_skips_known_hashes():
    from itn.publish.export import changed_details
    details = {"a": {"id": "a"}, "b": {"id": "b"}}
    first = changed_details(details, {})
    assert set(first) == {"a", "b"}
    known = {aid: digest for aid, (_, digest) in first.items()}
    assert changed_details(details, known) == {}
    details["b"]["title"] = "new"
    assert set(changed_details(details, known)) == {"b"}
