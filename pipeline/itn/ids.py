"""Stable, human-readable artifact IDs. See the architecture doc, section 5."""

import re


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def executive_order(number: int | str) -> str:
    return f"eo/{int(number)}"


def presidential_document(fr_document_number: str) -> str:
    """Proclamations, memoranda, etc. are keyed by Federal Register document number."""
    return f"fr/{fr_document_number}"


def public_law(congress: int | str, number: int | str) -> str:
    return f"pl/{int(congress)}-{int(number)}"


def scotus_case(volume: int | str, page: int | str) -> str:
    return f"scotus/{int(volume)}-us-{int(page)}"


def scotus_docket(docket_number: str) -> str:
    """Used until a U.S. Reports citation exists."""
    return f"scotus/docket-{_slug(docket_number)}"


def usc_section(title: int | str, section: str, edition_year: int | None = None) -> str:
    base = f"usc/{int(title)}/{_slug(section)}"
    return f"{base}@{edition_year}" if edition_year else base


def component(artifact_id: str, ord: int, kind: str) -> str:
    """One opinion within a case: majority, concurrence, dissent, ..."""
    return f"{artifact_id}#{int(ord)}-{_slug(kind)}"


def text_key(artifact_id: str) -> str:
    """R2 key for an artifact's cleaned text."""
    return f"text/{artifact_id}.txt"


def component_text_key(component_id: str) -> str:
    return text_key(component_id.replace("#", "/"))
