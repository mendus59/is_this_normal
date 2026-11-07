"""Prompts and output schemas for each paid stage. The system prompt is the same for
every call of a stage, so it is cached; the item-specific material goes in the user turn."""

import json

VERDICTS = ["supports", "partially", "not_on_point", "contradicts", "mischaracterized"]
INSUFFICIENT = -1


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}}
_RATING = {"type": "integer", "enum": [INSUFFICIENT, 0, 1, 2, 3, 4]}


def system_prompt(rubric: dict, stage: str) -> str:
    scale = "\n".join(f"{k} = {v['band']}: {v['meaning']}" for k, v in rubric["scale"].items())
    return f"""You analyze U.S. Supreme Court rulings, presidential actions, and federal laws for
IsThisNormal, a nonpartisan project that measures how far each one departed from how such
things were usually done. Normal does not mean right or wrong: a correct ruling that
overrules precedent is still a departure, and a harmful action can be routine.

Rules that apply to every task:
- Judge only from the material provided. It contains nothing dated after the item's
  as-of date (the day before it took effect). Do not use anything you know about later
  events, later rulings, or how the item was received.
- Cite evidence by the artifact ids given in the material. Do not invent ids.
- If the material is not enough to judge, say so (rating -1) rather than guess.
- Be even-handed across parties, presidents, and courts. Describe; do not editorialize.

Rating scale (rubric version {rubric['version']}):
{scale}

Same actor: the same Court membership, the same president, or the same Congress cannot
make its own actions normal by repeating them. Material marked same_actor counts for at
most a quarter of independent evidence, and every rating also needs rating_independent:
the rating you would give using only other actors' history.

Current task: {stage}. Answer only with the requested JSON."""


def summary(item, text: str) -> tuple[str, dict]:
    user = (f"Item {item.id} ({item.type}), effective {item.effective_date}: {item.title}\n"
            f"Official summary (may be empty): {item.summary or ''}\n\nText:\n{text}\n\n"
            "Summarize what it does or holds and what it relies on, in at most 200 words.")
    return user, _obj({"summary": _STR, "relies_on": {"type": "array", "items": _STR}})


def authority_digest(aid: str, title: str, date, text: str) -> tuple[str, dict]:
    user = (f"Authority {aid}: {title} ({date or 'undated'})\n\nText:\n{text}\n\n"
            "Write a digest a later analyst can rely on: what it held or provides, the rule or "
            "test it states, and its limits. At most 250 words.")
    return user, _obj({"digest": _STR, "rules": {"type": "array", "items": _STR}})


def citation_check(item, cite: dict, cited_material: str) -> tuple[str, dict]:
    user = (f"Citing item {item.id}: {item.title} (as of {item.as_of})\n"
            f"Passage citing {cite['to']}:\n{cite['context']}\n\n"
            f"What {cite['to']} ({cite['title']}) says:\n{cited_material}\n\n"
            "Does the citing passage use the authority accurately?")
    return user, _obj({"verdict": {"type": "string", "enum": VERDICTS}, "reason": _STR})


def type_assessment(rubric: dict, item, item_summary: str, digests: dict, checks: dict,
                    candidates: list[dict]) -> tuple[str, dict]:
    dims = rubric["types"][item.type]["dimensions"]
    questions = "\n".join(f"- {k}: {q}" for k, q in dims.items())
    user = (f"Item {item.id} ({item.type}) by {item.actor}, as of {item.as_of}: {item.title}\n"
            f"Summary: {item_summary}\n\n"
            f"Authorities it relies on (digests):\n{json.dumps(digests, indent=1)}\n\n"
            f"Citation checks:\n{json.dumps(checks, indent=1)}\n\n"
            f"Comparable earlier items (same_actor marks the same Court, president or Congress):\n"
            f"{json.dumps(candidates, indent=1)}\n\n"
            "First decide which of these did the same kind of thing and how close the closest one "
            "from a different actor is. Then rate each dimension 0-4 (or -1 if insufficient):\n"
            f"{questions}")
    dim = _obj({"rating": _RATING, "rating_independent": _RATING, "evidence": _IDS, "reason": _STR})
    return user, _obj({"dimensions": _obj({k: dim for k in dims}), "note": _STR})


def verification(rubric: dict, item, assessment_user: str, first: dict) -> tuple[str, dict]:
    dims = rubric["types"][item.type]["dimensions"]
    user = (f"{assessment_user}\n\nA first analyst rated it:\n{json.dumps(first, indent=1)}\n\n"
            "Independently re-rate every dimension from the material. Agreeing is fine when "
            "the material supports it; do not defer to the first analyst.")
    dim = _obj({"rating": _RATING, "rating_independent": _RATING, "evidence": _IDS, "reason": _STR})
    return user, _obj({"dimensions": _obj({k: dim for k in dims}), "note": _STR})
