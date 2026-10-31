import datetime as dt

from itn.ingest.supremecourt_gov import (
    author_of, docket_summary, kind_of, parse_term_table, split_opinions, term_of, to_artifact,
)

TABLE = """
<table class="table table-bordered"><tr><th>R-</th><th>Date</th><th>Docket</th><th>Name</th><th>J.</th><th>Pt.</th></tr>
<tr><td>89</td><td>6/30/23</td><td>22-506</td>
<td><a href="/opinions/22pdf/22-506_nmcp.pdf" title="">Biden v. Nebraska</a></td><td>R</td><td>600/2</td></tr>
<tr><td>R-1</td><td>7/1/23</td><td>Revised</td><td>no link</td></tr>
</table>"""


def test_term_of():
    assert term_of(dt.date(2023, 6, 30)) == 2022
    assert term_of(dt.date(2023, 10, 2)) == 2023


def test_parse_term_table():
    [row] = parse_term_table(TABLE)
    assert row == {"date": "2023-06-30", "docket": "22-506", "name": "Biden v. Nebraska",
                   "pdf": "https://www.supremecourt.gov/opinions/22pdf/22-506_nmcp.pdf"}


def test_labels():
    assert kind_of("Opinion of the Court") == "majority"
    assert kind_of("KAGAN, J., dissenting") == "dissent"
    assert kind_of("BARRETT, J., concurring") == "concurrence"
    assert kind_of("THOMAS, J., concurring in part and dissenting in part") == "concurrence_in_part"
    assert kind_of("Opinion of ALITO, J.") == "plurality"
    assert author_of("ROBERTS, C. J., dissenting") == "Roberts"
    assert author_of("Per Curiam") == "Per Curiam"


def test_split_opinions_uses_running_headers():
    pages = [
        "(Slip Opinion) OCTOBER TERM, 2022\nSyllabus\nNOTE: ...\nHeld: The Secretary lacks authority.",
        "Cite as: 600 U. S. ____ (2023)\nSyllabus\nmore syllabus",
        "SUPREME COURT OF THE UNITED STATES\nCHIEF JUSTICE ROBERTS delivered the opinion of the Court.",
        "Cite as: 600 U. S. ____ (2023)\n3\nOpinion of the Court\nSee 20 U.S.C. § 1098bb.",
        "SUPREME COURT OF THE UNITED STATES\nJUSTICE KAGAN, with whom ..., dissenting.",
        "Cite as: 600 U. S. ____ (2023)\nKAGAN, J., dissenting\nThe majority overreaches.",
    ]
    syllabus, parts = split_opinions(pages)
    assert "Held: The Secretary lacks authority." in syllabus
    assert [label for label, _ in parts] == ["Opinion of the Court", "KAGAN, J., dissenting"]
    assert "CHIEF JUSTICE ROBERTS delivered" in parts[0][1] and "1098bb" in parts[0][1]
    assert "Cite as" not in parts[0][1]
    assert "JUSTICE KAGAN, with whom" in parts[1][1]


def test_to_artifact_keys_by_docket():
    row = {"date": "2023-06-30", "docket": "22-506", "name": "Biden v. Nebraska",
           "pdf": "https://www.supremecourt.gov/opinions/22pdf/22-506_nmcp.pdf"}
    docket = docket_summary({"DocketedDate": "Nov 21 2022", "LowerCourt": "8th Cir.",
                             "ProceedingsandOrder": [{"Date": "Nov 18 2022", "Text": "Application"}]})
    a = to_artifact(row, "Held: ...", docket)
    assert a["id"] == "scotus/docket-22-506"
    assert a["metadata"]["docket"]["lower_court"] == "8th Cir."
    assert a["official_summary"] == "Held: ..."
