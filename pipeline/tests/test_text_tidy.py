from itn import ligatures
from itn.ingest import congress_laws, tidy_texts

PRINT = """Because of Lau's pending charge for a crime, a border offcer declined
to regard Lau as admitted upon his return. The Board of Immigration Appeals affrmed.
This Court frst considered the confict. Briefs of amici curiae urging reversal were
fled for the State of Illinois. The suspect fled to Mexico. See F. 3d 42; FRE 403."""


def test_ligatures_put_back_in_pdf_text():
    out = ligatures.fix(PRINT)
    assert "border officer declined" in out
    assert "Appeals affirmed." in out
    assert "first considered the conflict" in out
    assert "were\nfiled for the State" in out
    # "fled" stays where it means fled; abbreviations are left alone.
    assert "The suspect fled to Mexico" in out
    assert "F. 3d 42; FRE 403" in out


def test_ligatures_keep_case():
    assert ligatures.fix("Frst, the offcer. SPECIFC FINDINGS. Confict.") == \
        "First, the officer. SPECIFIC FINDINGS. Conflict."


def test_clean_text_is_untouched():
    text = "The officer filed a brief. The fat cat sat on the flat mat; a fee to flee. Fres v. Smith."
    assert ligatures.fix(text) == text
    # One odd word is not enough evidence to change anything.
    assert ligatures.fix("A specifc rule.") == "A specifc rule."


GOVINFO = """<html><body><pre>
[115th Congress Public Law 331]
[From the U.S. Government Publishing Office]


[[Page 4483]]

SEC. 2. &lt;&lt;NOTE: 6 USC 126.&gt;&gt;  DEPARTMENT OF HOMELAND SECURITY DATA \r
                    FRAMEWORK.

    (a) In General.--
            (1) Development.--The Secretary of Homeland Security shall
        develop a data framework&nbsp;to integrate ``existing'' datasets.
</pre></body></html>"""


def test_law_text_keeps_lines_and_decodes():
    text = congress_laws.law_text(GOVINFO)
    assert text.startswith("[115th Congress Public Law 331]\n")
    assert "SEC. 2. <<NOTE: 6 USC 126.>>  DEPARTMENT OF HOMELAND SECURITY DATA\n                    FRAMEWORK." in text
    assert "            (1) Development.--The Secretary of Homeland Security shall\n" in text
    assert "framework to integrate ``existing'' datasets." in text
    assert "<" not in text.replace("<<NOTE", "").replace(">>", "")
    assert "\r" not in text


def test_summaries_decoded():
    raw = "This bill expands the authority of&nbsp;CBP.&nbsp; It is &quot;effective&quot; &amp; final&#150;mostly."
    assert congress_laws._strip_html(f"<p>{raw}</p>") == 'This bill expands the authority of CBP. It is "effective" & final–mostly.'
    assert tidy_texts.clean_summary(raw) == congress_laws._strip_html(raw)


class FakeR2:
    def __init__(self, texts):
        self.texts = dict(texts)
        self.puts = []

    def get_text(self, key):
        return self.texts.get(key)

    def put_text(self, key, text):
        self.texts[key] = text
        self.puts.append(key)
        return "sha-" + key


class FakeConn:
    def __init__(self, keys):
        self.keys = keys

    def execute(self, sql, params=None):
        class R:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows
        return R([(k,) for k in self.keys])

    def commit(self):
        pass


def test_rulings_repairs_only_broken_texts():
    r2 = FakeR2({"a": PRINT, "b": "A clean opinion that the officer filed."})
    n = tidy_texts.rulings(FakeConn(["a", "b"]), r2, deadline=float("inf"), log=lambda *a: None)
    assert n == 1 and r2.puts == ["a"]
    assert "border officer" in r2.texts["a"]
