import contextlib
import json
from types import SimpleNamespace as NS

import pytest

from itn.analyze import llm, stages
from itn.analyze.rubric import load_rubric

RUBRIC = {**load_rubric(), "run": {"batch": True, "prompt_caching": False}}


class DB:
    """Just enough of Postgres for Pipeline.call: analysis cache, model_batch, run."""
    def __init__(self):
        self.analysis, self.batches = {}, {}

    def execute(self, q, p=()):
        rows = []
        if q.startswith("SELECT id, input_hash, output FROM analysis"):
            rows = [(i, h, o) for h, (i, o) in self.analysis.items() if h in p[1]]
        elif q.startswith("INSERT INTO analysis"):
            h = p[3]
            self.analysis.setdefault(h, (len(self.analysis) + 1, None))
            self.analysis[h] = (self.analysis[h][0], p[6].obj)
            return NS(fetchone=lambda: (self.analysis[h][0],))
        elif q.startswith("SELECT id, requests FROM model_batch"):
            rows = [(b, v["requests"]) for b, v in self.batches.items() if v["status"] == "submitted"]
        elif q.startswith("INSERT INTO model_batch"):
            self.batches[p[0]] = {"requests": p[3].obj, "status": "submitted"}
        elif q.startswith("UPDATE model_batch"):
            self.batches[p[0]]["status"] = "collected"
        return NS(fetchall=lambda: rows, fetchone=lambda: None)

    def commit(self):
        pass


class Client:
    def __init__(self):
        self.sent, self.ended = {}, set()
        outer = self

        class Batches:
            def create(self, requests):
                bid = f"b{len(outer.sent)}"
                outer.sent[bid] = requests
                return NS(id=bid)

            def retrieve(self, bid):
                return NS(processing_status="ended" if bid in outer.ended else "in_progress")

            def results(self, bid):
                for r in outer.sent[bid]:
                    msg = NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"ok": r["custom_id"]}))],
                             usage=NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                                      cache_creation_input_tokens=0))
                    yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))
        self.messages = NS(batches=Batches())


def _pipe(monkeypatch, db, client, wait):
    monkeypatch.setattr(stages.db, "connect", lambda: contextlib.nullcontext(db))
    monkeypatch.setattr(llm, "POLL_SECONDS", 0)
    monkeypatch.setattr(stages.time, "sleep", lambda s: None)
    monkeypatch.setattr(stages, "WAIT_MINUTES", wait)
    p = stages.Pipeline(None, client, RUBRIC, 10, "haiku", profile="economy", record_run=False)
    p._save_run = lambda c, status: None
    return p


def test_batch_survives_a_timeout_and_is_collected_without_paying_again(monkeypatch):
    db, client = DB(), Client()
    jobs = [(f"eo/{n}", "2017-01-01", f"prompt {n}", {}) for n in range(150)]
    with pytest.raises(stages.BatchesPending):
        _pipe(monkeypatch, db, client, wait=0).call("type_assessment", jobs)
    assert list(client.sent) == ["b0"] and len(client.sent["b0"]) == 150
    assert db.batches["b0"]["status"] == "submitted"

    client.ended.add("b0")
    out = _pipe(monkeypatch, db, client, wait=5).call("type_assessment", jobs)
    assert list(client.sent) == ["b0"]                     # nothing sent twice
    assert len(out) == 150 and all(v[0] for v in out.values())
    assert db.batches["b0"]["status"] == "collected"

    again = _pipe(monkeypatch, db, client, wait=5).call("type_assessment", jobs)
    assert len(again) == 150 and list(client.sent) == ["b0"]   # now served from the cache


def test_whole_stage_is_sent_at_once_in_chunks(monkeypatch):
    db, client = DB(), Client()
    client.ended.update({"b0", "b1"})
    jobs = [(f"eo/{n}", "2017-01-01", f"prompt {n}", {}) for n in range(llm.CHUNK + 50)]
    out = _pipe(monkeypatch, db, client, wait=5).call("type_assessment", jobs)
    assert [len(v) for v in client.sent.values()] == [llm.CHUNK, 50] and len(out) == llm.CHUNK + 50


def test_dropped_connection_is_retried(monkeypatch):
    import psycopg
    calls = []

    def fn(c, part):
        calls.append(part)
        if len(calls) == 1:
            raise psycopg.OperationalError("SSL connection has been closed unexpectedly")
    monkeypatch.setattr(stages.db, "connect", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(stages.time, "sleep", lambda s: None)
    stages._with_retry(fn, [1, 2])
    assert calls == [[1, 2], [1, 2]]
