import contextlib
import datetime as dt
import json
from types import SimpleNamespace as NS

from itn.analyze import data, review, stages
from itn.analyze.data import Item
from itn.analyze.rubric import load_rubric

RUBRIC = load_rubric()
DIMS = RUBRIC["types"]["executive_document"]["dimensions"]
ITEM = Item(id="eo/13780", type="executive_document", subtype="executive_order", title="T",
            effective_date=dt.date(2017, 3, 6), summary="", metadata={"president": "Donald Trump"})


def _packet():
    return {"id": ITEM.id, "type": ITEM.type, "as_of": ITEM.as_of.isoformat(), "allowed": [ITEM.id, "usc/8/1182"],
            "facts": {}, "prompt_hash": "h1"}


def _answer(**kw):
    a = {"id": ITEM.id, "prompt_hash": "h1", "note": "",
         "dimensions": {k: {"rating": 1, "rating_independent": 1, "evidence": [ITEM.id], "reason": "r"} for k in DIMS}}
    return {**a, **kw}


class Conn:
    def __init__(self):
        self.scores, self.analyses = [], []

    def execute(self, q, p=()):
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


def _run(tmp_path, monkeypatch, answers):
    conn = Conn()
    monkeypatch.setattr(review.db, "connect", lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(data, "load_items", lambda c, ids: [ITEM])
    monkeypatch.setattr(data, "same_actor_finals", lambda c, i, ids: {})
    pk, an = tmp_path / "p.jsonl", tmp_path / "a.jsonl"
    pk.write_text(json.dumps(_packet()) + "\n")
    an.write_text("".join(json.dumps(a) + "\n" for a in answers))
    n = review.import_answers(RUBRIC, [pk], [an], "reviewer-x", log=lambda m: None)
    return n, conn


def test_import_writes_current_score(tmp_path, monkeypatch):
    n, conn = _run(tmp_path, monkeypatch, [_answer()])
    assert n == 1 and len(conn.scores) == 1
    artifact, aid, _, raw, conf, current = conn.scores[0]
    assert artifact == ITEM.id and aid == 7 and current is True and raw == 0.25
    assert conn.analyses[0][4] == "reviewer-x" and conn.analyses[0][3].endswith("-economy-review")


def test_import_rejects_wrong_hash_bad_rating_and_unknown_evidence(tmp_path, monkeypatch):
    bad_rating = _answer()
    bad_rating["dimensions"]["scope"]["rating"] = 7
    invented = _answer(dimensions={k: {"rating": 1, "rating_independent": 1, "evidence": ["eo/99999"], "reason": "r"}
                                   for k in DIMS})
    n, conn = _run(tmp_path, monkeypatch, [_answer(prompt_hash="other"), bad_rating, invented])
    assert n == 0 and conn.scores == []


def test_review_pipeline_logs_no_run(monkeypatch):
    monkeypatch.setattr(stages.db, "connect", lambda: (_ for _ in ()).throw(AssertionError("no db")))
    p = stages.Pipeline(None, None, RUBRIC, 0, "haiku", profile="economy", record_run=False)
    assert p.run_id is None


def _ruled_dims(sdims, **over):
    d = {k: {"rating": 3, "rating_independent": 3, "final": 3, "evidence": ["x"], "reason": "r"} for k in sdims}
    for k, v in over.items():
        d[k] = {**d[k], **v}
    return d


def test_rescore_rebuilds_score_when_rule_no_longer_fires(monkeypatch):
    case = Item(id="scotus/545-us-75", type="scotus_case", subtype=None, title="Alaska v. United States",
                effective_date=dt.date(2005, 6, 6), summary="", metadata={})
    sdims = RUBRIC["types"]["scotus_case"]["dimensions"]
    out = {"dimensions": {k: {"rating": 3, "rating_independent": 3, "evidence": [case.id], "reason": "r"} for k in sdims}}
    ruled = _ruled_dims(sdims, precedent={"final": 0, "rule": "express_overruling"})
    updates = []

    class C(Conn):
        def execute(self, q, p=()):
            if q.startswith("SELECT s.artifact_id, s.analysis_id, an.kind"):
                return NS(fetchall=lambda: [(case.id, 5, "type_assessment", ruled),
                                            ("scotus/1-us-1", 6, "type_assessment", ruled)])
            if q.startswith("SELECT output FROM analysis WHERE id"):
                return NS(fetchone=lambda: (out,))
            if q.startswith("UPDATE score"):
                updates.append(p)
            return NS(fetchone=lambda: None, fetchall=lambda: [])

    other = Item(id="scotus/1-us-1", type="scotus_case", subtype=None, title="X", effective_date=dt.date(2005, 6, 6),
                 summary="", metadata={})
    monkeypatch.setattr(review.db, "connect", lambda: contextlib.nullcontext(C()))
    monkeypatch.setattr(data, "load_items", lambda c, ids: [case, other])
    monkeypatch.setattr(data, "same_actor_finals", lambda c, i, ids: {})
    monkeypatch.setattr(stages.Pipeline, "economy_inputs", lambda self, items: {
        "facts": {case.id: {"expressly_overrules": False}, other.id: {"expressly_overrules": ["X is overruled."]}},
        "allowed": {case.id: {case.id}, other.id: {other.id, case.id}}})
    changed, kept = review.rescore_rules(None, RUBRIC, log=lambda m: None)
    assert (changed, kept) == (1, 1)
    assert len(updates) == 1                      # the ruling that still overrules is left as it is
    dims, raw, conf, artifact, analysis_id = updates[0]
    assert artifact == case.id and analysis_id == 5 and raw == 0.75
    assert "rule" not in dims.obj["precedent"]


def _new_rules_run(monkeypatch, texts, summaries=None, dry_run=False):
    sdims = RUBRIC["types"]["scotus_case"]["dimensions"]
    items = []
    for k, key in enumerate(texts):
        it = Item(id=f"scotus/600-us-{k + 1}", type="scotus_case", subtype=None, title=key,
                  effective_date=dt.date(2018, 6, 21), summary=(summaries or {}).get(key, ""), metadata={})
        it.text_keys = [key]
        items.append(it)
    updates = []

    class C(Conn):
        def execute(self, q, p=()):
            if q.startswith("SELECT s.artifact_id, s.analysis_id, s.dimensions"):
                return NS(fetchall=lambda: [(i.id, 5, _ruled_dims(sdims, procedure={"final": 1}), 0.6, 1.0) for i in items])
            if q.startswith("UPDATE score"):
                updates.append(p)
            return NS(fetchone=lambda: None, fetchall=lambda: [])

    monkeypatch.setattr(review.db, "connect", lambda: contextlib.nullcontext(C()))
    monkeypatch.setattr(data, "load_items", lambda c, ids: items)
    monkeypatch.setattr(data, "text_of", lambda r2, keys, n: texts[keys[0]])
    n = review.apply_new_rules(None, RUBRIC, log=lambda m: None, dry_run=dry_run)
    return n, {u[3]: u[0].obj for u in updates}


def test_rescore_applies_rules_to_rulings_they_used_to_miss(monkeypatch):
    n, got = _new_rules_run(monkeypatch, {
        "wayfair": "Justice Kennedy delivered the opinion of the Court. Quill and Bellas Hess should be, and now are, overruled.",
        "plain": "Justice Kagan delivered the opinion of the Court. We decline to overrule Smith.",
        "summary": "Per Curiam.\nThe petition for a writ of certiorari is granted. The judgment is reversed.",
        "stay": "Per Curiam.\nThe application for stay presented to Justice Alito and referred to the Court is granted."})
    assert n == 3 and set(got) == {"scotus/600-us-1", "scotus/600-us-3", "scotus/600-us-4"}
    assert got["scotus/600-us-1"]["precedent"]["rule"] == "express_overruling" and got["scotus/600-us-1"]["precedent"]["final"] == 0
    assert "rule" not in got["scotus/600-us-1"]["procedure"]
    assert got["scotus/600-us-3"]["procedure"]["final"] == 2 and got["scotus/600-us-3"]["procedure"]["rule"] == "ruling_procedure"
    assert got["scotus/600-us-4"]["procedure"]["final"] == 1 and "emergency application" in got["scotus/600-us-4"]["procedure"]["reason"]


def test_rescore_dry_run_writes_nothing(monkeypatch):
    n, got = _new_rules_run(monkeypatch, {"summary": "Per Curiam.\nThe petition for a writ of certiorari is granted."},
                            dry_run=True)
    assert n == 1 and got == {}


def test_import_ignores_the_ruling_path_fact():
    facts = review._facts({"ruling_path": {"kind": "summary_disposition", "description": "d"}, "expressly_overrules": False})
    assert "ruling_path" not in facts
