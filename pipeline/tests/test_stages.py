import datetime as dt
import json
from types import SimpleNamespace as NS

import pytest

from itn.analyze import llm, prompts
from itn.analyze.data import Item, as_of
from itn.analyze.rubric import load_rubric
from itn.analyze.stages import combine, merge

RUBRIC = load_rubric()


def _item(**kw):
    base = dict(id="eo/13780", type="executive_document", subtype="executive_order",
                title="Protecting the Nation", effective_date=dt.date(2017, 3, 6), summary="", metadata={"president": "Donald Trump"})
    return Item(**{**base, **kw})


def _dims(rating, independent, evidence):
    return {k: {"rating": rating, "rating_independent": independent, "evidence": evidence, "reason": "r"}
            for k in RUBRIC["types"]["executive_document"]["dimensions"]}


def test_as_of_is_day_before():
    assert as_of("2022-06-24") == dt.date(2022, 6, 23)


def test_combine_same_actor_cap():
    it = _item()
    prior = {"eo/13769": {k: {"final": 0} for k in RUBRIC["types"]["executive_document"]["dimensions"]}}
    dims, raw, conf = combine(RUBRIC, it, _dims(4, 0, ["eo/13769"]), {"eo/13769"}, prior)
    assert raw == 0 and conf == 1 and all(d["same_actor_cap"] for d in dims.values())


def test_combine_drops_invented_evidence():
    it = _item()
    dims, raw, conf = combine(RUBRIC, it, _dims(3, 3, ["eo/99999"]), {"eo/13769"}, {})
    assert raw is None and conf == 0


def test_merge_lowers_confidence_on_disagreement():
    a = {"x": {"final": 4}, "y": {"final": 1}}
    b = {"x": {"final": 1}, "y": {"final": 1}}
    dims, raw, conf = merge(a, b, 1.0)
    assert dims["x"]["final"] == 2 and conf == pytest.approx(0.7) and raw == pytest.approx(3 / 8)


def test_assessment_schema_lists_every_dimension():
    _, schema = prompts.type_assessment(RUBRIC, _item(), "s", {}, {}, [])
    assert set(schema["properties"]["dimensions"]["required"]) == set(RUBRIC["types"]["executive_document"]["dimensions"])


def _msg(payload, stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=json.dumps(payload))],
              usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeClient:
    def __init__(self):
        self.created = []
        self.messages = NS(create=self._create, batches=NS(create=self._bcreate, retrieve=self._bget, results=self._bres))

    def _create(self, **params):
        self.created.append(params)
        return _msg({"summary": "ok", "relies_on": []})

    def _bcreate(self, requests):
        self.batch = requests
        return NS(id="b1")

    def _bget(self, _id):
        return NS(processing_status="ended")

    def _bres(self, _id):
        return [NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=_msg({"summary": "ok", "relies_on": []})))
                for r in self.batch]


def _req(i):
    return llm.Req(f"r{i}", "summary", "haiku", "sys", "user", {"type": "object"})


def test_run_direct_and_batch_track_spend():
    c, spend = FakeClient(), llm.Spend(1.0)
    out = llm.run(c, [_req(1)], RUBRIC["models"], spend, batch=False, caching=True)
    assert out["r1"].data == {"summary": "ok", "relies_on": []}
    assert c.created[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert c.created[0]["output_config"]["format"]["type"] == "json_schema"
    direct = spend.spent
    out = llm.run(c, [_req(2), _req(3)], RUBRIC["models"], spend, batch=True, caching=False)
    assert set(out) == {"r2", "r3"} and spend.spent == pytest.approx(direct * 2)  # batch is half price


def test_budget_stops_before_overspend():
    spend = llm.Spend(0.0001)
    with pytest.raises(llm.BudgetExceeded):
        llm.run(FakeClient(), [_req(1)], RUBRIC["models"], spend, batch=False, caching=False)


def test_refusal_is_recorded_not_parsed():
    data, err = llm._parse(_msg({}, stop="refusal"))
    assert data is None and err == "refusal"


def test_economy_path_one_pass_then_checks_and_second_pass_for_unusual(monkeypatch):
    import contextlib
    import datetime as dt

    from itn.analyze import data, stages
    from itn.analyze.rubric import load_rubric

    rubric = load_rubric()
    p = object.__new__(stages.Pipeline)
    p.rubric, p.economy, p.compare, p.log = rubric, rubric["economy"], True, lambda m: None
    p.stages = dict(rubric["economy"]["stages"])
    p.r2 = NS(get_text=lambda k: "text " * 100)
    monkeypatch.setattr(stages.db, "connect", lambda: contextlib.nullcontext(None))
    item = data.Item("eo/1", "executive_document", "executive_order", "Odd order", dt.date(2020, 1, 2),
                     None, {"president": "x"}, ["k"])
    plain = data.Item("eo/2", "executive_document", "executive_order", "Plain order", dt.date(2020, 1, 3),
                      None, {"president": "x"}, ["k"])
    cite = {"citation_id": None, "to": "usc/8/1182", "context": "under 8 U.S.C. 1182(f)", "title": "8 U.S.C. 1182",
            "date": None, "summary": "", "type": "usc_section"}
    monkeypatch.setattr(data, "citations", lambda c, r2, i, t: [cite])
    monkeypatch.setattr(data, "analog_candidates", lambda c, i, n: [])
    monkeypatch.setattr(data, "record_facts", lambda c, i, t=None, cited=None: {})
    monkeypatch.setattr(data, "_text_keys", lambda c, a: ["k2"])
    monkeypatch.setattr(data, "same_actor_finals", lambda c, i, ids: {})
    dims = rubric["types"]["executive_document"]["dimensions"]

    def rated(r):
        return {"dimensions": {k: {"rating": r, "rating_independent": r, "evidence": ["usc/8/1182"], "reason": ""}
                               for k in dims}}
    calls, written = [], []

    def fake_call(stage, jobs):
        calls.append((stage, [j[0] for j in jobs]))
        if stage == "type_assessment":
            return {"eo/1": (rated(0), 1), "eo/2": (rated(4), 2)}
        if stage == "citation_check":
            return {j[0]: ({"verdict": "supports", "reason": ""}, 3) for j in jobs}
        return {j[0]: (rated(1), 4) for j in jobs}
    p.call = fake_call
    p._write_score = lambda i, aid, d, raw, conf, c=None: written.append((i.id, aid, raw))
    p._score_economy([item, plain])
    assert [s for s, _ in calls] == ["type_assessment", "citation_check", "verification"]
    assert calls[1][1] == ["eo/1->usc/8/1182"] and calls[2][1] == ["eo/1"]
    assert written[-1][:2] == ("eo/1", 4) and ("eo/2", 2, 1.0) in written


def test_economy_disputed_items_settle_on_stronger_tier(monkeypatch):
    import contextlib
    import datetime as dt

    from itn.analyze import data, stages
    from itn.analyze.rubric import load_rubric

    rubric = load_rubric()
    p = object.__new__(stages.Pipeline)
    p.rubric, p.compare, p.log = rubric, True, lambda m: None
    p.economy = {**rubric["economy"], "escalate": {"to": "sonnet"}}
    p.stages = dict(rubric["economy"]["stages"])
    p.r2 = NS(get_text=lambda k: "text " * 100)
    monkeypatch.setattr(stages.db, "connect", lambda: contextlib.nullcontext(None))
    item = data.Item("eo/1", "executive_document", "executive_order", "Odd order", dt.date(2020, 1, 2),
                     None, {"president": "x"}, ["k"])
    plain = data.Item("eo/2", "executive_document", "executive_order", "Plain order", dt.date(2020, 1, 3),
                      None, {"president": "x"}, ["k"])
    cite = {"citation_id": None, "to": "usc/8/1182", "context": "under 8 U.S.C. 1182(f)", "title": "8 U.S.C. 1182",
            "date": None, "summary": "", "type": "usc_section"}
    monkeypatch.setattr(data, "citations", lambda c, r2, i, t: [cite])
    monkeypatch.setattr(data, "analog_candidates", lambda c, i, n: [])
    monkeypatch.setattr(data, "record_facts", lambda c, i, t=None, cited=None: {})
    monkeypatch.setattr(data, "_text_keys", lambda c, a: ["k2"])
    monkeypatch.setattr(data, "same_actor_finals", lambda c, i, ids: {})
    dims = rubric["types"]["executive_document"]["dimensions"]

    def rated(r):
        return {"dimensions": {k: {"rating": r, "rating_independent": r, "evidence": ["usc/8/1182"], "reason": ""}
                               for k in dims}}
    calls, written, tiers = [], [], []

    def fake_call(stage, jobs):
        calls.append((stage, [j[0] for j in jobs]))
        if stage == "type_assessment":
            return {"eo/1": (rated(0), 1), "eo/2": (rated(4), 2)}
        if stage == "citation_check":
            return {j[0]: ({"verdict": "supports", "reason": ""}, 3) for j in jobs}
        tiers.append(p.stages[stage])
        return {j[0]: (rated(3), 4) for j in jobs}
    p.call = fake_call
    p._write_score = lambda i, aid, d, raw, conf, c=None: written.append((i.id, aid, raw))
    p._score_economy([item, plain])
    assert [s for s, _ in calls] == ["type_assessment", "citation_check", "verification", "verification"]
    assert tiers == ["haiku", "sonnet"] and calls[3][1] == ["eo/1"]
    assert written[-1][0] == "eo/1" and written[-1][2] == 0.75


def test_law_facts_votes_and_vehicle():
    from itn.analyze import data

    class Conn:
        def execute(self, q, p):
            return NS(fetchone=lambda: (90, 100), fetchall=lambda: [("pl/109-171", "Deficit Reduction Act"), ("pl/111-152", "Health Care and Education Reconciliation Act")])

        def commit(self):
            pass
    law = _item(id="pl/115-97", type="public_law", subtype=None, effective_date=dt.date(2017, 12, 22),
                title="An act to provide for reconciliation pursuant to titles II and V", summary="x" * 5000,
                metadata={"votes": [{"chamber": "Senate", "date": "2017-12-20", "yeas": 51, "nays": 48},
                                    {"chamber": "House", "date": "2018-01-05", "yeas": 1, "nays": 0}]})
    facts = data.record_facts(Conn(), law)
    assert facts["final_passage_votes"] == [{"chamber": "Senate", "date": "2017-12-20", "yeas": 51, "nays": 48}]
    assert "reconciliation" in facts["vehicle"] and ": 2" in facts["vehicle"]
    user, _ = prompts.type_assessment(RUBRIC, law, "s", {}, {}, [], facts)
    assert "Facts from the record" in user and "Authorities it relies on" in user
    eo = data.record_facts(Conn(), _item())
    assert eo["emergency_powers_invoked"].startswith("none") and eo["rarely_used_provisions"] == "none"
    assert facts["size_percentile"].startswith("90 ")


def test_scotus_overruling_passages():
    from itn.analyze import data

    case = _item(id="scotus/597-us-215", type="scotus_case", subtype=None)
    text = "Held: The Constitution does not confer a right to abortion. Roe and Casey are overruled. Other text."
    assert data.record_facts(None, case, text)["overruling_passages"] == ["Roe and Casey are overruled."]
    assert data.record_facts(None, case, "Nothing here.")["overruling_passages"].startswith("none")


def test_overall_cap_above_worst():
    from itn.analyze.stages import overall

    assert overall([0, 3, 3, 3, 4]) == pytest.approx(0.65)          # plain mean: ordinary
    assert overall([0, 3, 3, 3, 4], cap=1) < 0.40                    # at most novel
    assert overall([3, 4, 4], cap=1) == overall([3, 4, 4])           # no effect near the top
    assert overall([2, 2, 2], cap=1) == pytest.approx(0.5)


def test_express_overrulings_skip_negations():
    from itn.analyze import data

    text = ("We decline to overrule Smith. Roe and Casey are overruled. The dissent would not have Casey overruled. "
            "Today we overrule Jones.")
    got = data.express_overrulings(text)
    assert got == ["Roe and Casey are overruled.", "Today we overrule Jones."]


def test_questions_about_overruling_are_not_express():
    from itn.analyze import data
    assert data.express_overrulings("Accordingly, in granting certiorari, we directed the parties to address the "
                                    "question whether Saucier should be overruled.") == []
    assert data.express_overrulings("II Before considering whether Austin should be overruled, we first address "
                                    "narrower grounds.") == []
    assert data.express_overrulings("Jackson should be and now is overruled.") == ["Jackson should be and now is overruled."]
    assert data.express_overrulings("Roe and Casey must be overruled.") == ["Roe and Casey must be overruled."]


def test_conditions_history_and_quotes_are_not_express():
    from itn.analyze import data
    for s in ("And, until we overrule Zadvydas, it will continue to invite nothing but mischief.",
              "The court found that no Supreme Court precedent is overruled or modified.",
              "Should we overrule them all? Taking the argument seriously would require just that.",
              "So if Almendarez-Torres is overruled, a defendant could be subject to all of that harm.",
              "Before the claim reached this Court, the Collins case was overruled.",
              "The second flaw in the dissent's proposed approach is its consequence: Gingles must be overruled.",
              "It concluded: “This Court's opinion in Harper I is overruled.”"):
        assert data.express_overrulings(s) == [], s
    for s in ("Insofar as it held that a defective indictment deprives a court of jurisdiction, Bain is overruled.",
              "To the extent that Meek and Wolman conflict with this holding, we overrule them.",
              "If anything more is left of Humphrey's, we overrule it.",
              "Accordingly, we reject Bowles' reliance on the doctrine, and we overrule Harris Truck Lines and "
              "Thompson to the extent they purport to authorize an exception to a jurisdictional rule.",
              "We hold that Roe and Casey must be overruled."):
        assert data.express_overrulings(s) == [s], s


def test_adverbs_and_commas_inside_an_express_overruling():
    from itn.analyze import data
    for s in ("Finding Ford inconsistent with that line of cases, we consequently overrule Ford insofar as it "
              "would otherwise apply.",
              "Quill and National Bellas Hess should be, and now are, overruled.",
              "It is, accordingly, overruled.",
              "We now expressly overrule Spaziano and Hildwin in relevant part."):
        assert data.express_overrulings(s) == [s], s
    for s in ("We do not, therefore, overrule Smith.",
              "Kansas' objection is, accordingly, overruled.",
              "The dissent says Smith is, consequently, overruled."):
        assert data.express_overrulings(s) == [], s


def test_ruling_path_from_caption_and_opening():
    from itn.analyze import data
    cap = "OCTOBER TERM, 2019 423\nREPUBLICAN NATIONAL COMMITTEE et al. v.\nDEMOCRATIC NATIONAL COMMITTEE et al.\n"
    assert data.ruling_path(cap + "on application for stay\nNo. 19A1016. Decided April 6, 2020\n", "Per Curiam.\nx") == "emergency_unargued"
    assert data.ruling_path("NFIB v. OSHA\non applications for stays\nNo. 21A244. Argued January 7, 2022—Decided January 13, 2022",
                            "Per Curiam.\nx") == "emergency_argued"
    assert data.ruling_path("", "OCTOBER TERM, 2019 345\non petition for writ of certiorari to the united\nstates court of "
                            "appeals\nNo. 19–5421. Decided March 23, 2020\nPer Curiam.\n") == "summary_disposition"
    assert data.ruling_path(None, "Per Curiam.\nRespondent was convicted. " + "x " * 900 +
                            "The petition for a writ of certiorari and the motion for leave to proceed in forma pauperis "
                            "are granted. The judgment is reversed.") == "summary_disposition"
    assert data.ruling_path(None, "Per Curiam.\nWe granted certiorari to decide two questions. " + "x " * 400 +
                            "The writ of certiorari is dismissed as improvidently granted.") == "dismissed_improvidently"
    assert data.ruling_path(None, "Chief Justice Rehnquist, Circuit Justice.\nThis case is before me on an application "
                            "for injunctive relief.") == "in_chambers"
    # Ordinary argued cases, including ones whose syllabus mentions an application or a stay.
    assert data.ruling_path("MORGAN v. SUNDANCE, INC.\ncertiorari to the united states court of appeals\nNo. 21–328. "
                            "Argued March 21, 2022—Decided May 23, 2022\nSundance moved to stay the litigation; an "
                            "application for a stay under § 3 was denied.", "Justice Kagan delivered the opinion.") is None
    assert data.ruling_path(None, "Justice Breyer delivered the opinion of the Court. After the application for a stay "
                            "was denied, the petition for a writ of certiorari is granted in a related case.") is None
    assert data.ruling_path(None, "Per Curiam.\nOn December 8, 2000, the Supreme Court of Florida ordered a recount. "
                            "We granted the application, treated it as a petition for a writ of certiorari, and granted "
                            "certiorari.") is None


def test_procedure_rule_sets_rating_both_ways():
    from itn.analyze.stages import apply_rules, overall
    case = Item(id="scotus/589-us-423", type="scotus_case", subtype=None, title="RNC v. DNC",
                effective_date=dt.date(2020, 4, 6), summary="", metadata={})
    names = RUBRIC["types"]["scotus_case"]["dimensions"]
    for start, kind, want in ((1, "summary_disposition", 2), (4, "summary_disposition", 2), (4, "emergency_unargued", 1),
                              (1, "in_chambers", 3)):
        dims = {k: {"rating": 3, "rating_independent": 3, "final": 3, "evidence": ["x"], "reason": "r"} for k in names}
        dims["procedure"]["final"] = start
        out, raw, _ = apply_rules(RUBRIC, case, dims, 0.7, 1.0, {"ruling_path": {"kind": kind, "description": "d"}})
        assert out["procedure"]["final"] == want and out["procedure"]["rule"] == "ruling_procedure"
        assert raw == pytest.approx(overall([3] * (len(names) - 1) + [want], 1))
    dims = {k: {"rating": 3, "rating_independent": 3, "final": 3, "evidence": ["x"], "reason": "r"} for k in names}
    same, raw, _ = apply_rules(RUBRIC, case, dict(dims), 0.75, 1.0, {"expressly_overrules": False})
    assert same == dims and raw == 0.75


def test_overruled_exceptions_are_not_precedent():
    from itn.analyze import data
    assert data.express_overrulings("For the reasons we discuss, Alaska's exceptions are overruled. "
                                    "We overrule Alaska's exception.") == []
    assert data.express_overrulings("Alaska's exception to the Special Master's recommendation on count I of the "
                                    "Amended Complaint is overruled.") == []


def test_statute_history_counts_other_presidents():
    from itn.analyze import data

    class Conn:
        def execute(self, q, p):
            rows = [("fr/a", dt.date(2005, 1, 1), {"president": "George W. Bush"}),
                    ("fr/b", dt.date(2010, 1, 1), {"president": "Barack Obama"})]
            return NS(fetchall=lambda: rows)

        def commit(self):
            pass
    eo = _item(metadata={"president": "Donald Trump"})
    facts = data.record_facts(Conn(), eo, None, ["usc/8/1182", "scotus/1-us-1"])
    assert facts["emergency_powers_invoked"] == ["INA 212(f) entry suspension"]
    h = facts["rarely_used_provisions"][0]
    assert h["earlier_items"] == 2 and h["earlier_by_other_actors"] == 2 and h["first_use"] == "2005-01-01"
    assert "commonly_cited_provisions" not in facts


def test_common_provisions_carry_no_counts():
    from itn.analyze import data
    hist = [{"section": "usc/8/1182", "earlier_items": 30, "earlier_by_other_actors": 29},
            {"section": "usc/10/2808", "earlier_items": 1, "earlier_by_other_actors": 1}]
    got = data.statute_facts(hist)
    assert [h["section"] for h in got["rarely_used_provisions"]] == ["usc/10/2808"]
    assert got["commonly_cited_provisions"]["sections"] == ["usc/8/1182"]
    assert "29" not in str(got["commonly_cited_provisions"])


def test_express_overruling_rule_sets_precedent():
    from itn.analyze.stages import apply_rules

    case = _item(id="scotus/597-us-215", type="scotus_case", subtype=None)
    dims = {k: {"rating": 3, "rating_independent": 3, "final": 3, "evidence": ["x"], "reason": ""}
            for k in RUBRIC["types"]["scotus_case"]["dimensions"]}
    out, raw, _ = apply_rules(RUBRIC, case, dims, 0.75, 1.0, {"expressly_overrules": ["Roe and Casey are overruled."]})
    assert out["precedent"]["final"] == 0 and out["precedent"]["rule"] == "express_overruling"
    assert raw < 0.40   # with the worst-plus-one cap: novel at most
    same, raw2, _ = apply_rules(RUBRIC, case, dict(dims), 0.75, 1.0, {"expressly_overrules": False})
    assert raw2 == 0.75


def test_passage_path_from_votes():
    from itn.analyze import data
    v = lambda ch, d, y, n: {"chamber": ch, "date": d, "yeas": y, "nays": n}
    assert data.passage_path([]) == "voice"
    assert data.passage_path([v("House", "2017-05-23", 418, 0)]) == "near_unanimous"
    assert data.passage_path([v("House", "2025-04-09", 217, 211), v("Senate", "2025-03-27", 52, 48)]) == "narrow"
    assert data.passage_path([v("House", "2015-05-21", 284, 133)]) is None
    # Only each chamber's last vote counts: an earlier version passed by one vote.
    assert data.passage_path([v("House", "2021-05-20", 213, 212), v("House", "2021-07-29", 416, 11),
                              v("Senate", "2021-07-29", 98, 0)]) == "near_unanimous"


def test_law_process_rule_raises_ordinary_and_lowers_on_narrow_votes():
    from itn.analyze.stages import apply_rules
    law = Item(id="pl/115-55", type="public_law", subtype=None, title="An act",
               effective_date=dt.date(2017, 8, 23), summary="", metadata={})
    names = RUBRIC["types"]["public_law"]["dimensions"]
    for start, kind, want, ruled in ((3, "voice", 4, True), (3, "near_unanimous", 4, True), (2, "voice", 2, False),
                                     (4, "narrow", 2, True), (3, "narrow", 2, True), (1, "narrow", 1, False),
                                     (-1, "voice", 4, True)):
        dims = {k: {"rating": 3, "rating_independent": 3, "final": 3, "evidence": ["x"], "reason": "r"} for k in names}
        dims["process"]["final"] = start
        out, _, _ = apply_rules(RUBRIC, law, dims, 0.7, 1.0, {"passage_path": {"kind": kind, "description": "d"}})
        assert out["process"]["final"] == want
        assert (out["process"].get("rule") == "law_process") == ruled
