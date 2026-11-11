"""Model calls for the paid stages: one request list in, parsed JSON out, with spend
tracked against a hard budget. Uses the Message Batches API when the rubric asks for it."""

import json
import time
from dataclasses import dataclass

from itn.analyze.estimate import BATCH, CACHE_READ, CHARS_PER_TOKEN, EFFORT, HAIKU_LONG, HAIKU_LONG_FROM, PRICES

CACHE_WRITE = 1.25
CHUNK = 200                 # requests per batch; the budget is checked before each one is sent
POLL_SECONDS = 30
MAX_TOKENS = {"summary": 4_000, "authority_digest": 4_000, "citation_check": 2_000,
              "type_assessment": 16_000, "verification": 16_000}
# Upper-end output guesses for reserving budget before batches are sent. Observed economy
# outputs are about 1,200 tokens for assessments; half of MAX_TOKENS was far too high to
# send a whole stage at once.
EXPECTED_OUT = {"summary": 1_500, "authority_digest": 1_500, "citation_check": 600,
                "type_assessment": 2_500, "verification": 2_500}


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Req:
    custom_id: str
    stage: str
    tier: str
    system: str
    user: str
    schema: dict
    effort: str | None = None


@dataclass
class Result:
    data: dict | None
    error: str | None
    cost_usd: float
    model: str
    usage: dict


def tier_of(stage_tier: str, max_tier: str) -> str:
    order = ["haiku", "sonnet", "opus"]
    return order[min(order.index(stage_tier), order.index(max_tier))]


def usage_cost(tier: str, usage: dict, batch: bool) -> float:
    p_in, p_out = PRICES[tier]
    fresh = usage.get("input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0) or 0
    write = usage.get("cache_creation_input_tokens", 0) or 0
    if tier == "haiku" and fresh + read + write > HAIKU_LONG_FROM:
        p_in, p_out = HAIKU_LONG
    usd = (fresh * p_in + read * p_in * CACHE_READ + write * p_in * CACHE_WRITE
           + usage.get("output_tokens", 0) * p_out) / 1e6
    return usd * (BATCH if batch else 1.0)


def rough_cost(req: Req, batch: bool) -> float:
    """Upper-end guess for one request, used to stop before the budget is crossed."""
    tokens_in = (len(req.system) + len(req.user)) // CHARS_PER_TOKEN
    return usage_cost(req.tier, {"input_tokens": tokens_in, "output_tokens": EXPECTED_OUT[req.stage]}, batch)


class Spend:
    def __init__(self, budget: float):
        self.budget, self.spent = budget, 0.0
        self.tokens = {"input": 0, "cached": 0, "output": 0}

    def add(self, cost: float, usage: dict) -> None:
        self.spent += cost
        self.tokens["input"] += usage.get("input_tokens", 0) + (usage.get("cache_creation_input_tokens") or 0)
        self.tokens["cached"] += usage.get("cache_read_input_tokens") or 0
        self.tokens["output"] += usage.get("output_tokens", 0)

    def check(self, upcoming: float) -> None:
        if self.spent + upcoming > self.budget:
            raise BudgetExceeded(f"Stopping: ${self.spent:.2f} spent, next step needs about ${upcoming:.2f},"
                                 f" budget ${self.budget:.2f}")


def _params(req: Req, models: dict, caching: bool) -> dict:
    system = [{"type": "text", "text": req.system, **({"cache_control": {"type": "ephemeral"}} if caching else {})}]
    return {"model": models[req.tier], "max_tokens": MAX_TOKENS[req.stage], "system": system,
            "messages": [{"role": "user", "content": req.user}],
            "output_config": {"effort": req.effort or EFFORT[req.stage],
                              "format": {"type": "json_schema", "schema": req.schema}}}


def _parse(msg) -> tuple[dict | None, str | None]:
    if msg.stop_reason == "refusal":
        return None, "refusal"
    if msg.stop_reason == "max_tokens":
        return None, "max_tokens"
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "invalid_json"


def _usage(msg) -> dict:
    u = msg.usage
    return {k: getattr(u, k, 0) or 0 for k in
            ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


def run(client, reqs: list[Req], models: dict, spend: Spend, batch: bool, caching: bool) -> dict[str, Result]:
    out: dict[str, Result] = {}
    for i in range(0, len(reqs), CHUNK if batch else 1):
        chunk = reqs[i:i + (CHUNK if batch else 1)]
        spend.check(sum(rough_cost(r, batch) for r in chunk))
        by_id = {r.custom_id: r for r in chunk}
        if batch:
            b = client.messages.batches.create(requests=[
                {"custom_id": r.custom_id, "params": _params(r, models, caching)} for r in chunk])
            while client.messages.batches.retrieve(b.id).processing_status != "ended":
                time.sleep(POLL_SECONDS)
            for res in client.messages.batches.results(b.id):
                r = by_id[res.custom_id]
                if res.result.type == "succeeded":
                    msg = res.result.message
                    usage = _usage(msg)
                    data, err = _parse(msg)
                else:
                    usage, data, err = {}, None, res.result.type
                cost = usage_cost(r.tier, usage, True)
                spend.add(cost, usage)
                out[r.custom_id] = Result(data, err, cost, models[r.tier], usage)
        else:
            r = chunk[0]
            msg = client.messages.create(**_params(r, models, caching))
            usage = _usage(msg)
            data, err = _parse(msg)
            cost = usage_cost(r.tier, usage, False)
            spend.add(cost, usage)
            out[r.custom_id] = Result(data, err, cost, models[r.tier], usage)
    return out


# ---- batches that outlive a run ---------------------------------------------------
# A batch can sit in Anthropic's queue for hours. Callers record each batch when it is
# sent and collect it whenever it ends, in this run or a later one, so a job timeout
# never throws away results that were paid for.

def submit_batch(client, reqs: list[Req], models: dict, caching: bool) -> str:
    b = client.messages.batches.create(requests=[
        {"custom_id": r.custom_id, "params": _params(r, models, caching)} for r in reqs])
    return b.id


def batch_ended(client, batch_id: str) -> bool:
    return client.messages.batches.retrieve(batch_id).processing_status == "ended"


def collect_batch(client, batch_id: str, tiers: dict[str, str], models: dict, spend: Spend) -> dict[str, Result]:
    """Results of an ended batch by custom_id; `tiers` maps custom_id to the tier it used."""
    out: dict[str, Result] = {}
    for res in client.messages.batches.results(batch_id):
        tier = tiers.get(res.custom_id)
        if tier is None:
            continue
        if res.result.type == "succeeded":
            msg = res.result.message
            usage = _usage(msg)
            data, err = _parse(msg)
        else:
            usage, data, err = {}, None, res.result.type
        cost = usage_cost(tier, usage, True)
        spend.add(cost, usage)
        out[res.custom_id] = Result(data, err, cost, models[tier], usage)
    return out
