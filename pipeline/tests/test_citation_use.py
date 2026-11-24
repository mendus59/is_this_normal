import contextlib
import datetime as dt
from types import SimpleNamespace as NS

from itn.analyze import citation_use as cu
from itn.analyze import data, prompts
from itn.analyze.rubric import load_rubric

OPINION = ("The right is broad. As we said, the Constitution protects \"a right of personal privacy\" "
           "that is fundamental. Roe v. Wade, 410 U. S. 113, 152 (1973). Later we held otherwise. "
           "Roe v. Wade, 410 U. S. 113, 160. See 42 U. S. C. § 1983.")
CITED = ("Syllabus text. " + "filler words here. " * 80
         + "This holds a right of personal privacy includes the abortion decision, but it is not unqualified. "
         + "more filler. " * 40)


def _item():
    return data.Item("scotus/505-us-833", "scotus_case", "opinion", "Planned Parenthood v. Casey",
                     dt.date(1992, 6, 29), None, {})


def test_locate_prefers_the_pinpoint_and_skips_used_spans():
    first = cu.locate(OPINION, "410 U. S. 113", "152", set())
    second = cu.locate(OPINION, "410 U. S. 113", "160", set())
    assert first and second and first[0] < second[0]
    assert OPINION[second[1]:second[1] + 5] == ", 160"
    assert cu.locate(OPINION, "410 U. S. 113", None, {first[0]}) == second
    assert cu.locate(OPINION, "999 U. S. 1", None, set()) is None
    # A line break inside the citation still matches.
    assert cu.locate("see 410 U. S.\n113 here", "410 U. S. 113", None, set()) == (4, 17)


def test_passage_marks_the_citation():
    span = cu.locate(OPINION, "410 U. S. 113", "152", set())
    p = cu.passage(OPINION, span)
    assert "⟦410 U. S. 113⟧" in p and p.startswith("The right is broad.")


def test_excerpt_finds_a_quotation():
    p = cu.passage(OPINION, cu.locate(OPINION, "410 U. S. 113", "152", set()))
    ex, found = cu.excerpt(p, CITED)
    assert found is True and "a right of personal privacy includes" in ex


def test_excerpt_reports_a_missing_quotation():
    p = 'It held that "states may never regulate this at any point at all." ⟦410 U. S. 113⟧.'
    ex, found = cu.excerpt(p, CITED)
    assert found is False and ex


def test_excerpt_without_quotation_uses_word_overlap():
    p = "The abortion decision is protected but unqualified rights are not recognized ⟦410 U. S. 113⟧"
    ex, found = cu.excerpt(p, CITED)
    assert found is None and "abortion decision" in ex
    assert cu.excerpt(p, "") == ("", None)


def test_parse_drops_unknown_numbers_and_verdicts():
    out = {"checks": [{"n": 1, "verdict": "stretch", "reason": " dicta "}, {"n": 9, "verdict": "improper", "reason": ""},
                      {"n": 2, "verdict": "wrong", "reason": ""}, {"n": 1, "verdict": "improper", "reason": "dup"}]}
    assert cu.parse(out, 2) == {1: ("stretch", "dicta")}
    assert cu.parse(None, 2) == {}


def test_settle_needs_two_checks_for_improper():
    assert cu.settle("appropriate", None) == "appropriate"
    assert cu.settle("improper", "improper") == "improper"
    assert cu.settle("improper", "stretch") == "stretch"
    assert cu.settle("improper", "appropriate") == "appropriate"
    assert cu.settle("improper", None) == "improper"


def test_system_prompt_for_the_stage():
    assert prompts.system_prompt(load_rubric(), cu.STAGE) == cu.SYSTEM
    assert set(cu.schema()["properties"]["checks"]["items"]["properties"]["verdict"]["enum"]) == set(cu.VERDICTS)


def _rows():
    # (artifact, citation id, component, kind, author, text key, to, pin, raw, context, title, type, date, summary)
    comp = "scotus/505-us-833#1-majority"
    return [("scotus/505-us-833", 11, comp, "majority", "O'Connor", "k-maj", "scotus/410-us-113", "152", "410 U. S. 113",
             "ctx", "Roe v. Wade", "scotus_case", dt.date(1973, 1, 22), "Roe held ..."),
            ("scotus/505-us-833", 12, comp, "majority", "O'Connor", "k-maj", "scotus/410-us-113", "160", "410 U. S. 113",
             "ctx", "Roe v. Wade", "scotus_case", dt.date(1973, 1, 22), "Roe held ..."),
            ("scotus/505-us-833", 13, "scotus/505-us-833#2-dissent", "dissent", "Scalia", "k-dis", "usc/42/1983", None,
             "42 U.S.C. § 1983", "Suits under 42 U.S.C. § 1983 lie.", "42 U.S.C. 1983", "usc_section", None, "")]


def test_groups_split_by_opinion_and_size(monkeypatch):
    r2 = NS(get_text=lambda k: {"k-maj": OPINION, "k-roe": CITED}.get(k))
    monkeypatch.setattr(cu, "CHUNK", 1)
    groups = cu.groups_for(_item(), _rows(), r2, {"scotus/410-us-113": ["k-roe"]}, cu.TextCache(r2))
    assert [g.subject for g in groups] == ["scotus/505-us-833#1-majority@0", "scotus/505-us-833#1-majority@1",
                                           "scotus/505-us-833#2-dissent@0"]
    a, b, d = (g.cites[0] for g in groups)
    assert a.passage != b.passage and a.quote_found is True
    assert d.passage == "Suits under ⟦42 U.S.C. § 1983⟧ lie."   # no text: the stored context
    assert groups[2].label == "dissenting opinion by Scalia"
    user = cu.user_prompt(groups[0])
    assert "[scotus/410-us-113] Roe v. Wade (scotus case, 1973-01-22): Roe held" in user
    assert '#1 cites [scotus/410-us-113] as "410 U. S. 113, 152"' in user


def test_run_rechecks_improper_and_stores(monkeypatch):
    rubric = load_rubric()
    r2 = NS(get_text=lambda k: {"k-maj": OPINION, "k-roe": CITED}.get(k))
    monkeypatch.setattr(cu.db, "connect", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(cu.data, "load_items", lambda c, ids: [_item()])
    monkeypatch.setattr(cu, "build_groups", lambda r2_, items, log: (cu.groups_for(
        items[0], _rows(), r2, {"scotus/410-us-113": ["k-roe"]}, cu.TextCache(r2)), []))
    stored = {}
    monkeypatch.setattr(cu, "store", lambda results: stored.update(results))
    calls, tiers = [], []

    def call(stage, jobs):
        calls.append([j[0] for j in jobs])
        tiers.append(p.stages[stage])
        if len(calls) == 1:
            return {j[0]: ({"checks": [{"n": 1, "verdict": "appropriate", "reason": "ok"},
                                       {"n": 2, "verdict": "improper", "reason": "Roe did not say that"}]}, 1)
                    if "majority" in j[0] else ({"checks": [{"n": 1, "verdict": "improper", "reason": "no"}]}, 2)
                    for j in jobs}
        return {j[0]: ({"checks": [{"n": 1, "verdict": "improper" if "majority" in j[0] else "stretch",
                                    "reason": "second"}]}, 3) for j in jobs}

    p = NS(r2=r2, log=lambda m: None, rubric={**rubric, "citation_use": {"confirm_tier": "sonnet"}},
           stages={cu.STAGE: "haiku"}, compare=False, call=call)
    cu.run(p, ["scotus/505-us-833"])
    assert calls[1] == ["scotus/505-us-833#1-majority@0#2", "scotus/505-us-833#2-dissent@0#2"]
    assert tiers == ["haiku", "sonnet"] and p.stages[cu.STAGE] == "haiku"
    assert cu.settle(stored[11]["first"], stored[11].get("second")) == "appropriate"
    assert stored[12]["second"] == "improper" and stored[12]["reason"] == "Roe did not say that"
    assert cu.settle(stored[13]["first"], stored[13]["second"]) == "stretch" and stored[13]["reason"] == "second"


def test_estimate_counts_requests_and_cost():
    rows = [("scotus/1-us-1", "scotus_case", "scotus/1-us-1#0-majority", 45, 30, 3),
            ("eo/1", "executive_document", "eo/1#0-text", 2, 2, 0),
            ("eo/2", "executive_document", "eo/2#0-text", 0, 0, 1)]
    e = cu.estimate(rows)
    assert e["items"] == 3 and e["citations"] == 47 and e["requests"] == 4 and e["unloaded"] == 4
    assert 0 < e["usd"] < 0.05
    assert cu.estimate(rows, "sonnet", "sonnet")["usd"] > e["usd"] * 10
    assert any("scotus_case" in line for line in cu.report("t", e))


def test_name_mismatch_skips_a_different_case_at_the_cited_page():
    p = "as we held in Crosby v. Natl. Foreign Trade Council, ⟦528 U. S. 1018⟧ (1999)."
    assert cu.name_mismatch(p, "In re Reidt", "scotus_case")
    assert not cu.name_mismatch(p, "Crosby v. National Foreign Trade Council", "scotus_case")
    assert not cu.name_mismatch("See ⟦410 U. S. 113⟧.", "In re Reidt", "scotus_case")   # no name to compare
    assert not cu.name_mismatch(p, "16 U.S.C. 668d", "usc_section")
    assert not cu.name_mismatch("Roe v. Wade, ⟦410 U. S. 113⟧", "Roe v. Wade", "scotus_case")
    # String cite: the earlier case's name belongs to the earlier citation.
    assert not cu.name_mismatch("See Roe v. Wade, 410 U. S. 113 (1973); ⟦505 U. S. 833⟧", "Planned Parenthood v. Casey",
                                "scotus_case")


def test_groups_list_skipped_citations():
    r2 = NS(get_text=lambda k: None)
    rows = [("scotus/1-us-1", 21, "scotus/1-us-1#1-majority", "majority", None, None, "scotus/528-us-1018", None,
             "528 U. S. 1018", "as held in Crosby v. Council, 528 U. S. 1018 (1999).", "In re Reidt", "scotus_case",
             dt.date(1999, 11, 1), "")]
    skipped = []
    assert cu.groups_for(_item(), rows, r2, {}, cu.TextCache(r2), skipped) == [] and skipped == [21]
