from itn import ids
from itn.extract.citations import extract
from itn.ingest.courtlistener_scotus import (
    artifact_id, cluster_from_hit, opinion_text, split_names, to_artifact, to_components,
)

CLUSTER = {
    "id": 6375233, "absolute_url": "/opinion/6375233/dobbs-v-jackson-womens-health-organization/",
    "case_name": "Dobbs v. Jackson Women's Health Organization", "date_filed": "2022-06-24",
    "citations": [{"volume": 142, "reporter": "S. Ct.", "page": "2228", "type": 1},
                  {"volume": 597, "reporter": "U.S.", "page": "215", "type": 1}],
    "syllabus": "<p>Mississippi's Gestational Age Act ...</p>", "judges": "Alito, Thomas, ...",
    "docket": "https://www.courtlistener.com/api/rest/v4/dockets/1/",
    "scdb_votes_majority": 6, "scdb_votes_minority": 3,
}


def test_id_prefers_us_reports_cite():
    assert artifact_id(CLUSTER, None) == "scotus/597-us-215"
    no_cite = {**CLUSTER, "citations": [{"volume": 142, "reporter": "S. Ct.", "page": "2228"}]}
    assert artifact_id(no_cite, "19-1392") == "scotus/docket-19-1392"
    assert artifact_id(no_cite, None) is None


def test_to_artifact():
    a = to_artifact(CLUSTER, "scotus/597-us-215", None)
    assert a["type"] == "scotus_case" and a["in_scope"] is True
    assert a["official_summary"] == "Mississippi's Gestational Age Act ..."
    assert a["metadata"]["us_cite"] == "597 U.S. 215"
    assert a["sources"][0]["url"].startswith("https://www.courtlistener.com/opinion/6375233/")


def test_pre_2000_case_is_reference_only():
    a = to_artifact({**CLUSTER, "date_filed": "1973-01-22"}, "scotus/410-us-113", None)
    assert a["in_scope"] is False


def test_components_keep_order_and_kinds():
    ops = [
        {"id": 3, "type": "040dissent", "author_str": "Breyer", "joined_by_str": "Sotomayor, Kagan",
         "ordering_key": 3, "plain_text": "We dissent."},
        {"id": 1, "type": "020lead", "author_str": "Alito", "joined_by_str": "", "ordering_key": 1,
         "plain_text": "", "html_with_citations": "<p>Roe v. Wade, 410 U.&nbsp;S. 113 (1973).</p>"},
    ]
    comps = to_components("scotus/597-us-215", ops)
    assert [c["id"] for c, _ in comps] == ["scotus/597-us-215#0-majority", "scotus/597-us-215#1-dissent"]
    assert comps[1][0]["joined_by"] == ["Sotomayor", "Kagan"]
    assert comps[0][1] == "Roe v. Wade, 410 U. S. 113 (1973)."
    assert ids.component_text_key(comps[0][0]["id"]) == "text/scotus/597-us-215/0-majority.txt"


def test_opinion_text_falls_back_through_formats():
    assert opinion_text({"plain_text": "", "html_lawbox": "<div>Held.</div>"}) == "Held."
    assert opinion_text({}) == ""


def test_split_names():
    assert split_names("Thomas, Gorsuch and Kavanaugh") == ["Thomas", "Gorsuch", "Kavanaugh"]
    assert split_names(None) == []


def test_extract_citations():
    text = ("Roe v. Wade, 410 U.S. 113, 153 (1973), and Planned Parenthood v. Casey, 505 U. S. 833 "
            "(1992). Id., at 846. See 42 U.S.C. § 1983; Pub. L. No. 111-148, 124 Stat. 119; "
            "Brown v. Plata, 563 F.3d 1 (2009).")
    rows = extract(text, "2022-06-24")
    targets = [r["to_artifact"] for r in rows]
    assert targets == ["scotus/410-us-113", "scotus/505-us-833", "usc/42/1983", "pl/111-148"]
    assert rows[0]["pinpoint"] == "153"
    assert rows[0]["citing_date"] == "2022-06-24"
    assert "Roe v. Wade" in rows[0]["context_snippet"]


def test_cluster_from_search_hit():
    hit = {"cluster_id": 6375233, "caseName": "Dobbs v. Jackson Women's Health Organization",
           "dateFiled": "2022-06-24T00:00:00-07:00", "citation": ["597 U.S. 215", "142 S. Ct. 2228"],
           "docketNumber": "19-1392", "absolute_url": "/opinion/6375233/dobbs/", "status": "Published"}
    c = cluster_from_hit(hit)
    assert c["date_filed"] == "2022-06-24"
    assert artifact_id(c, c["docket_number"]) == "scotus/597-us-215"
    assert c["citations"][1] == {"volume": "142", "reporter": "S. Ct.", "page": "2228"}


def test_cap_case_mapping():
    import io
    import json
    import zipfile

    from itn.ingest import cap_scotus

    case = {"id": 1, "name_abbreviation": "Bush v. Gore", "decision_date": "2000-12-12",
            "docket_number": "00-949", "citations": [{"type": "official", "cite": "531 U.S. 98"}],
            "casebody": {"opinions": [
                {"type": "per-curiam", "author": None, "text": "Per Curiam. " + "x" * 3000},
                {"type": "dissent", "author": "Justice Stevens, with whom Justice Ginsburg and "
                                              "Justice Breyer join, dissenting.", "text": "I dissent."}]}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("json/0098-01.json", json.dumps(case))
        z.writestr("531/CasesMetadata.json", "[]")
    [loaded] = list(cap_scotus.cases_in_zip(buf.getvalue()))
    assert loaded["file_name"] == "0098-01"
    a = cap_scotus.to_artifact(loaded, *cap_scotus.us_cite(loaded))
    assert a["id"] == "scotus/531-us-98" and a["in_scope"] is True
    comps = cap_scotus.to_components(a["id"], loaded)
    assert [c["kind"] for c, _ in comps] == ["majority", "dissent"]
    assert comps[1][0]["author"] == "Stevens"


def test_cap_author_cleanup():
    from itn.ingest.cap_scotus import clean_author

    assert clean_author("Chief Justice Rehnquist delivered the opinion of the Court.") == "Rehnquist"
    assert clean_author("Justice O'Connor, concurring.") == "O'Connor"
    assert clean_author(None) is None


def test_cap_orders_are_not_decisions():
    from itn.ingest.cap_scotus import is_decision

    order = {"casebody": {"opinions": [{"type": "majority", "text": "Certiorari denied."}]}}
    assert not is_decision(order)
    assert is_decision({"casebody": {"opinions": [{"type": "majority", "text": "x" * 5000}]}})
    denial_dissent = {"casebody": {"opinions": [{"type": "dissent", "text": "x" * 9000}]}}
    assert not is_decision(denial_dissent)


def test_extract_ignores_blank_slip_cites():
    rows = extract("Dobbs v. Jackson Women's Health Org., 597 U. S. ___ (2022); 410 U.S. 113.")
    assert [r["to_artifact"] for r in rows] == ["scotus/410-us-113"]


def test_hourly_wait():
    import httpx

    from itn.ingest.courtlistener_scotus import hourly_wait

    def r(text, after):
        return httpx.Response(429, text=text, headers={"Retry-After": str(after)})

    hourly = '{"detail":"Rate limit exceeded: 50/hour. Expected available in 2993 seconds."}'
    daily = '{"detail":"Rate limit exceeded: 125/day. Expected available in 5747 seconds."}'
    assert hourly_wait(r(hourly, 2993), None, 0) == 2998
    assert hourly_wait(r(hourly, 2993), 1000, 0) is None  # run would end first
    assert hourly_wait(r(daily, 5747), None, 0) is None


def test_full_date():
    from itn.ingest.cited_scotus import full_date
    assert full_date("1786-03") == "1786-03-01"
    assert full_date("1801") == "1801-01-01"
    assert full_date("1965-06-07") == "1965-06-07"
    assert full_date(None) is None


def test_usc_page_text_and_heading():
    from itn.ingest.cited_usc import heading, page_text, to_artifact
    page = ("<html><head><title>x</title></head><body><h3>§1983. Civil action for deprivation of rights</h3>"
            "<p>Every person who, under color of any statute&hellip;</p></body></html>")
    text = page_text(page)
    assert text.startswith("§1983. Civil action") and "Every person who" in text
    assert heading(text, "42", "1983") == "42 U.S.C. §1983. Civil action for deprivation of rights"
    a = to_artifact("usc/42/1983", "42", "1983", text,
                    "https://www.govinfo.gov/content/pkg/USCODE-2023-title42/html/USCODE-2023-title42-chap21-subchapI-sec1983.htm")
    assert a["metadata"]["edition"] == "2023" and a["type"] == "usc_section" and not a["in_scope"]


def test_best_per_page_prefers_decision_then_length():
    from itn.ingest.cited_scotus import best_per_page

    def case(page, kind, text):
        return {"citations": [{"cite": f"538 U.S. {page}"}],
                "casebody": {"opinions": [{"type": kind, "text": text}]}}

    order = case(901, "majority", "Certiorari denied.")
    long_order = case(901, "dissent", "x" * 5000)
    decision = case(901, "majority", "y" * 3000)
    other = case(902, "majority", "short")
    skipped = case(903, "majority", "z" * 3000)
    best = best_per_page([order, long_order, decision, other, skipped], {901, 902})
    assert set(best) == {901, 902}
    assert best[901] is decision
    assert best[902] is other


def test_extract_handles_empty_text():
    from itn.extract import citations
    assert citations.extract("") == []
    assert citations.extract("   \n") == []
    assert citations.extract(None) == []


def test_courtlistener_docket_and_orders_filter():
    from itn.ingest.courtlistener_scotus import clean_docket, is_decision
    assert clean_docket("No. 13-1352") == "13-1352"
    assert clean_docket("Nos. 14-556") == "14-556"
    assert clean_docket("14-5142") == "14-5142"
    assert clean_docket("") is None
    order = [({"kind": "opinion"}, "Petition for writ of certiorari denied.")]
    dissent_from_denial = [({"kind": "dissent"}, "x" * 8000)]
    ruling = [({"kind": "majority"}, "y" * 9000), ({"kind": "dissent"}, "z" * 4000)]
    assert not is_decision(order)
    assert not is_decision(dissent_from_denial)
    assert is_decision(ruling)


def test_denials_of_certiorari_are_not_rulings():
    from itn.ingest.cap_scotus import is_decision
    from itn.ingest.scotus_repair import is_denial
    statement = ("Ct. App. Ohio, Lucas County. Certiorari denied.\nStatement of Justice Breyer, with whom Justice Stevens "
                 "joins, respecting the denial of the petition for writ of certiorari.\n" + "x" * 5000)
    assert is_denial(statement)
    assert is_denial("Justice Alito, with whom Justice Thomas joins, dissenting from the denial of certiorari.")
    assert not is_decision({"casebody": {"opinions": [{"type": "majority", "text": statement}]}})
    assert not is_denial("Justice Breyer delivered the opinion of the Court. The court affirmed, cert. denied, "
                         "531 U. S. 1, and we granted certiorari.")
    assert not is_denial("Per Curiam. After this Court denied certiorari, respondent filed a second petition.")
    assert not is_denial("Chief Justice Rehnquist, Circuit Justice. This case is before me on an application.")


def test_drop_denials_marks_orders():
    from itn.ingest import scotus_repair

    class Conn:
        def __init__(self):
            self.updates = []

        def execute(self, q, p=()):
            if q.startswith("SELECT DISTINCT"):
                return type("R", (), {"fetchall": lambda s: [("scotus/534-us-982", "Overton v. Ohio", "k1"),
                                                             ("scotus/528-us-62", "Kimel", "k2")]})()
            self.updates.append(p)

        def commit(self):
            pass

    class R2:
        def get_text(self, key):
            return {"k1": "Ct. App. Ohio. Certiorari denied. Statement of Justice Breyer", "k2": "Justice O'Connor delivered"}[key]

    conn = Conn()
    assert scotus_repair.drop_denials(conn, R2(), log=lambda m: None) == 1
    assert conn.updates == [(["scotus/534-us-982"],)]


def test_caption_names():
    from itn.ingest.scotus_repair import caption_name
    assert caption_name("OCTOBER TERM, 2019 423\nREPUBLICAN NATIONAL COMMITTEE et al. v.\nDEMOCRATIC NATIONAL COMMITTEE "
                        "et al.\non application for stay\nNo. 19A1016.") == "Republican National Committee v. Democratic National Committee"
    assert caption_name("OCTOBER TERM, 2019 979\nBARR, ATTORNEY GENERAL, et al. v. LEE et al.\non application for stay "
                        "or vacatur\nNo. 20A8.") == "Barr v. Lee"
    assert caption_name("OCTOBER TERM, 2017 29\nIN RE UNITED STATES et al.\non petition for writ of mandamus") == "In re United States"
    assert caption_name("616 OCTOBER TERM, 2013\nSyl\nlabus\nHARRIS et al. v. QUINN, GOVERNOR OF ILLINOIS,\net al.\n"
                        "certiorari to the united states court of appeals") == "Harris v. Quinn"
    # The PDF text often loses part of the caption; a partial one is not a name.
    assert caption_name("OCTOBER TERM, 2019 197\nPROTECTION BUREAU\ncertiorari to the united states") is None
    assert caption_name("OCTOBER TERM, 2019 345\non petition for writ of certiorari to the united") is None


def test_retitle_prefers_listed_names(monkeypatch):
    import datetime as dt
    from itn.ingest import scotus_repair

    rows = [("scotus/590-us-83", "No. 18-5924", dt.date(2020, 4, 20), "18-5924", "OCTOBER TERM, 2019 83\ncertiorari to", None),
            ("scotus/591-us-979", "No. 20A8", dt.date(2020, 7, 14), "20A8",
             "OCTOBER TERM, 2019 979\nBARR, ATTORNEY GENERAL, et al. v. LEE et al.\non application for stay", None),
            ("scotus/589-us-999", "No. 19-1", dt.date(2020, 3, 2), "19-1", "OCTOBER TERM, 2019 999\ncertiorari to", None)]

    class Conn:
        def __init__(self):
            self.updates = []

        def execute(self, q, p=()):
            if q.startswith("SELECT"):
                return type("R", (), {"fetchall": lambda s: rows})()
            self.updates.append(p)

        def commit(self):
            pass

    monkeypatch.setattr(scotus_repair, "listed_names", lambda client, terms, log: {"18-5924": "Ramos v. Louisiana"})
    conn, tried = Conn(), set()
    assert scotus_repair.retitle(conn, None, None, tried, log=lambda m: None) == 2
    assert conn.updates == [("Ramos v. Louisiana", "scotus/590-us-83"), ("Barr v. Lee", "scotus/591-us-979")]
    assert tried == {r[0] for r in rows}
    assert scotus_repair.retitle(conn, None, None, tried, log=lambda m: None) == 0   # not retried every day
