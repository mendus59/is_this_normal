import datetime as dt

from itn.analyze import estimate as est
from itn.analyze.actors import actor, final_rating, natural_court
from itn.analyze.presort import routine_reason
from itn.analyze.rubric import load_anchors, load_rubric
from itn.publish.export import percentiles_by_type


def test_rubric_and_anchors_load():
    r = load_rubric()
    assert r["version"] == 1 and r["percentile_pool"] == "within_type"
    assert set(r["types"]) == {"scotus_case", "executive_document", "public_law"}
    a = load_anchors()
    assert all(len(a[t]) == 10 for t in ("scotus_case", "executive_document", "public_law"))


def test_natural_courts():
    assert natural_court("2022-06-24") == "court/2020-10-27"   # Dobbs, before Jackson joined
    assert natural_court(dt.date(2023, 6, 29)) == "court/2022-06-30"
    assert natural_court("2016-06-27") == "court/2016-02-13"


def test_actor_by_type():
    assert actor("public_law", "pl/117-169", None, {}) == "congress/117"
    assert actor("executive_document", "eo/13780", None, {"president": "Donald Trump"}) == "president/Donald Trump"


def test_same_actor_chain_inherits_unprecedented():
    # First order unprecedented (0); the next two rely on it and rate themselves routine.
    first = final_rating(0, 0, [])
    second = final_rating(4, 0, [first])
    third = final_rating(4, 0, [first, second])
    assert (first, second, third) == (0, 0, 0)


def test_same_actor_rule_keeps_independent_support():
    # Other presidents did similar things: the actor's own history does not drag it down.
    assert final_rating(3, 3, [0]) == 3
    assert final_rating(4, 2, [1]) == 2


def test_presort():
    assert routine_reason("proclamation", "National Farm Safety and Health Week, 2015")
    assert routine_reason("notice", "Continuation of the National Emergency With Respect to Iran")
    assert routine_reason("proclamation", "Adjusting Imports of Steel Into the United States") is None
    assert routine_reason("proclamation", "Declaring a National Emergency Concerning the Southern Border of the United States") is None
    assert routine_reason("executive_order", "Improving the Nation's Cybersecurity") is None
    assert routine_reason("memorandum", "Presidential Determination on Refugee Admissions for Fiscal Year 2016")
    assert routine_reason("executive_order", "Adjustments of Certain Rates of Pay")
    assert routine_reason("executive_order", "Providing for the Closing of Executive Departments and Agencies of the Federal Government on December 24, 2019")
    assert routine_reason("proclamation", "To Implement the United States-Korea Free Trade Agreement")
    assert routine_reason("proclamation", "To Modify Duty-Free Treatment Under the Generalized System of Preferences")
    assert routine_reason("executive_order", "Regulating Imports With a Reciprocal Tariff To Rectify Trade Practices") is None
    assert routine_reason("proclamation", "Adjusting Imports of Aluminum Into the United States") is None


def test_plan_counts_shared_digests_once():
    items = [est.Item("a", "scotus_case", "A", 10_000, ["x", "y"]),
             est.Item("b", "scotus_case", "B", 10_000, ["x", "z"]),
             est.Item("c", "executive_document", "C", 500, [], presort="ceremonial proclamation")]
    e = est.plan(items, {"x": 5_000, "y": 5_000}, load_rubric()["stages"], "opus")
    assert e.digests == 2 and e.unloaded_cited == 1
    assert e.calls["summary"] == 2 and e.calls["citation_check"] == 4
    assert e.total > 0


def test_max_tier_caps_models():
    assert est.cap_tier("opus", "sonnet") == "sonnet"
    assert est.cap_tier("haiku", "sonnet") == "haiku"


def test_percentiles_ranked_within_type():
    raw = {"s1": 0.1, "s2": 0.9, "e1": 0.5, "e2": 0.6}
    types = {"s1": "scotus_case", "s2": "scotus_case", "e1": "executive_document", "e2": "executive_document"}
    p = percentiles_by_type(raw, types)
    assert p["s2"] == 100 and p["e2"] == 100 and p["e1"] == 0


def test_bare_usc_citations_found_most_cited_first():
    from itn.extract.citations import extract_statutes
    text = ("by section 212(f) of the INA, 8 U.S.C. 1182(f), and 8 U.S.C. 1185(a), and "
            "section 301 of title 3, United States Code; see 8 U.S.C. 1182(f).")
    assert extract_statutes(text) == ["usc/8/1182", "usc/8/1185"]


def test_digests_limited_to_most_cited_per_item():
    many = [f"x{i}" for i in range(40)]
    e = est.plan([est.Item("a", "scotus_case", "A", 1_000, many)], {c: 1_000 for c in many},
                 load_rubric()["stages"], "opus")
    assert e.digests == est.DIGESTS_IN_ASSESSMENT


def test_batch_and_caching_lower_cost():
    items = [est.Item("a", "scotus_case", "A", 20_000, ["x"])]
    stages = load_rubric()["stages"]
    base = est.plan(items, {"x": 5_000}, stages, "opus").total
    batch = est.plan(items, {"x": 5_000}, stages, "opus", batch=True).total
    both = est.plan(items, {"x": 5_000}, stages, "opus", batch=True, caching=True).total
    assert abs(batch - base / 2) < 1e-9 and both < batch


def test_rubric_cost_settings():
    r = load_rubric()
    assert r["run"] == {"batch": True, "prompt_caching": True}
    assert r["compare_stages"] == {"authority_digest": "haiku", "verification": "sonnet", "type_assessment": "haiku"}
    assert r["effort"]["type_assessment"] == "medium" and "analog_assessment" not in r["stages"]


def test_escalation_adds_cost_and_medium_effort_saves():
    items = [est.Item("a", "scotus_case", "A", 20_000, ["x"])]
    stages = {**load_rubric()["stages"], "type_assessment": "haiku"}
    plain = est.plan(items, {"x": 5_000}, stages, "opus").total
    esc = est.plan(items, {"x": 5_000}, stages, "opus", escalate_to="sonnet").total
    assert esc > plain
    high = est.plan(items, {"x": 5_000}, load_rubric()["stages"], "opus", effort={"type_assessment": "high"}).total
    med = est.plan(items, {"x": 5_000}, load_rubric()["stages"], "opus", effort={"type_assessment": "medium"}).total
    assert med < high


def test_economy_plan_is_one_cheap_pass():
    from itn.analyze import estimate as est
    from itn.analyze.rubric import load_rubric

    rubric = load_rubric()
    items = [est.Item(id=f"scotus/{n}", type="scotus_case", title="x", text_tokens=40_000,
                      cited=[f"scotus/c{k}" for k in range(30)]) for n in range(10)]
    sizes = {f"scotus/c{k}": 20_000 for k in range(30)}
    standard = est.plan(items, sizes, rubric["stages"], "opus", batch=True, caching=True, effort=rubric["effort"])
    economy = est.plan_economy(items, rubric["economy"])
    assert "summary" not in economy.calls and "authority_digest" not in economy.calls
    assert economy.calls["type_assessment"] == 10
    assert economy.total < standard.total / 20
    assert set(rubric["economy"]["stages"].values()) == {"haiku"}
