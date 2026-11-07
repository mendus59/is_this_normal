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
