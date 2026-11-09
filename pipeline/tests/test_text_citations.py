from itn.extract import text_citations as tc


def test_statute_rows_have_context_and_dedupe():
    text = "Under section 212(f) of the INA, 8 U.S.C. 1182(f), and 50 U.S.C. 1701 ... again 8 U.S.C. 1182(f)."
    rows = tc.statute_rows(text, "2017-01-27")
    assert [r["to_artifact"] for r in rows] == ["usc/8/1182", "usc/50/1701"]
    assert "212(f)" in rows[0]["context_snippet"] and rows[0]["citing_date"] == "2017-01-27"


def test_rows_for_drops_self_and_skips_eyecite_on_huge_text(monkeypatch):
    called = []
    monkeypatch.setattr(tc.citations, "extract", lambda t, d: called.append(1) or [])
    tc.rows_for("x" * (tc.EYECITE_MAX_CHARS + 1) + " 26 U.S.C. 1", None, "pl/1-1")
    assert not called
    rows = tc.rows_for("See Pub. L. 1-1 and 26 U.S.C. 1.", None, "usc/26/1")
    assert rows == [] and called
