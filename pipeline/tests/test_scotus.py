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
