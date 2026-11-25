import datetime as dt

from itn.analyze import data, llm, overview as ov, prompts
from itn.analyze.rubric import load_rubric


def _item(summary=None, typ="scotus_case"):
    return data.Item("scotus/505-us-833", typ, "opinion", "Planned Parenthood v. Casey",
                     dt.date(1992, 6, 29), summary, {})


def test_syllabus_that_starts_the_text_is_sent_once():
    syl = "Syllabus. The Court reaffirmed the essential holding of an earlier case. " * 5
    text = syl + " Opinion of the Court. " + "x" * 30_000
    m = ov.material(_item(syl), text)
    assert m.count("reaffirmed the essential holding") == 5
    assert "Official summary" not in m
    assert len(m) <= ov.ITEM_CHARS + 50


def test_official_summary_comes_first_and_text_fills_the_rest():
    m = ov.material(_item("CRS summary: creates a grant program.", "public_law"), "Be it enacted " + "y" * 30_000)
    assert m.startswith("Official summary:\nCRS summary")
    assert "Text (start):\nBe it enacted" in m
    assert len(m) <= ov.ITEM_CHARS + 60
    assert ov.material(_item(), "") == "(no summary or text available)"


def test_prompt_and_schema():
    u = ov.user_prompt(_item(), "text")
    assert "Planned Parenthood v. Casey" in u and "Supreme Court ruling, opinion" in u and "1992-06-29" in u
    assert ov.schema()["required"] == ["summary"]
    assert prompts.system_prompt(load_rubric(), "overview") == ov.SYSTEM
    assert "later" in ov.SYSTEM and "Neutral" in ov.SYSTEM


def test_clean():
    assert ov.clean({"summary": "  The Court   held\nthat X. "}) == "The Court held that X."
    assert ov.clean({"summary": ""}) is None
    assert ov.clean(None) is None


def test_stage_is_configured():
    r = load_rubric()
    assert r["stages"]["overview"] == "haiku" and r["economy"]["stages"]["overview"] == "haiku"
    assert "overview" in llm.MAX_TOKENS and "overview" in llm.EXPECTED_OUT


def test_estimate_is_cents_per_thousand():
    e = ov.estimate([("scotus_case", ov.ITEM_CHARS)] * 1000 + [("public_law", 2_000)] * 10)
    assert e["items"] == 1010 and set(e["by_type"]) == {"scotus_case", "public_law"}
    assert 0.05 < e["usd"] < 1.0


def test_every_stage_is_an_allowed_analysis_kind():
    import re
    from itn.config import migrations_dir
    last = None
    for f in sorted(migrations_dir().glob("*.sql")):
        for m in re.finditer(r"analysis_kind_check CHECK \(kind IN \(([^)]*)\)", f.read_text()):
            last = set(re.findall(r"'(\w+)'", m.group(1)))
    if last is None:
        sql = (migrations_dir() / "001_init.sql").read_text()
        last = set(re.findall(r"'(\w+)'", sql.split("kind           text NOT NULL CHECK (kind IN (", 1)[1].split(")", 1)[0]))
    assert set(llm.MAX_TOKENS) <= last, set(llm.MAX_TOKENS) - last


def test_ruling_material_has_facts_holding_and_ending():
    syl = ("Syllabus. Petitioner challenged a federal ban on corporate spending. " * 60
           + "Held: The ban violates the First Amendment; Austin is overruled. " + "z" * 9_000)
    opinion = "Opinion of the Court. " + "w" * 50_000 + " The judgment is reversed. It is so ordered."
    item = _item(syl[:4000])
    m = ov.ruling_material(item, [syl, opinion])  # official summary cut before "Held:" -> from the text
    assert "Syllabus or summary (start):\nSyllabus. Petitioner" in m
    assert "Holding (from the syllabus):\nHeld: The ban violates" in m
    assert m.count("It is so ordered.") == 1 and "End of the Court's opinion" in m
    assert "Say what the Court decided" in m
    assert len(m) < 14_500
    u = ov.user_prompt(item, [syl, opinion])
    assert "Held: The ban violates" in u


def test_ruling_without_a_held_part_or_summary_falls_back_to_the_start():
    m = ov.ruling_material(_item(), ["Opinion text " * 10])
    assert m.startswith("Text (start):\nOpinion text")


def test_short_syllabus_with_held_is_sent_whole():
    m = ov.ruling_material(_item("Facts. Held: Affirmed."), ["Opinion short."])
    assert m.startswith("Syllabus or summary:\nFacts. Held: Affirmed.")
    assert "End of the Court's opinion" not in m
