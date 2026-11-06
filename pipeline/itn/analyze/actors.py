"""Who acted: the Supreme Court's membership on the decision date, the president who
signed, or the Congress that passed. The same-actor rule uses this so an actor's own
earlier items cannot make its later ones look normal."""

import datetime as dt
from bisect import bisect_right

# Start date of each natural court (a period with unchanged membership) since 2000.
NATURAL_COURTS = [
    ("1994-08-03", "Rehnquist (Breyer joins)"),
    ("2005-09-29", "Roberts (O'Connor remains)"),
    ("2006-01-31", "Roberts (Alito joins)"),
    ("2009-08-08", "Roberts (Sotomayor joins)"),
    ("2010-08-07", "Roberts (Kagan joins)"),
    ("2016-02-13", "Roberts (eight, after Scalia)"),
    ("2017-04-10", "Roberts (Gorsuch joins)"),
    ("2018-10-06", "Roberts (Kavanaugh joins)"),
    ("2020-09-18", "Roberts (eight, after Ginsburg)"),
    ("2020-10-27", "Roberts (Barrett joins)"),
    ("2022-06-30", "Roberts (Jackson joins)"),
]
_STARTS = [dt.date.fromisoformat(s) for s, _ in NATURAL_COURTS]


def _date(d) -> dt.date:
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)[:10])


def natural_court(decided) -> str:
    i = bisect_right(_STARTS, _date(decided)) - 1
    if i < 0:
        return "before-1994"
    return f"court/{NATURAL_COURTS[i][0]}"


def congress_of(law_id: str) -> str:
    """pl/117-169 -> congress/117."""
    return "congress/" + law_id.split("/", 1)[1].split("-", 1)[0]


def actor(artifact_type: str, artifact_id: str, effective_date, metadata: dict | None) -> str | None:
    if artifact_type == "scotus_case" and effective_date:
        return natural_court(effective_date)
    if artifact_type == "executive_document":
        name = (metadata or {}).get("president")
        return f"president/{name}" if name else None
    if artifact_type == "public_law" and artifact_id.startswith("pl/"):
        return congress_of(artifact_id)
    return None


def final_rating(rating: int, rating_independent: int | None, same_actor_ratings: list[int]) -> int:
    """Apply the same-actor rule to one dimension.

    same_actor_ratings are this dimension's final ratings for the actor's own earlier,
    already scored items that the rating relied on. Without them the rating stands.
    With them the rating cannot exceed the lowest of them, unless other actors'
    history alone (rating_independent) supports more."""
    if not same_actor_ratings:
        return rating
    floor = min(same_actor_ratings)
    independent = rating_independent if rating_independent is not None else 0
    return min(rating, max(floor, independent))
