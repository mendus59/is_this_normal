import datetime as dt

from itn.ingest.supremecourt_gov import (
    author_of, docket_summary, kind_of, parse_term_table, print_pages, print_ranges, split_opinions,
    term_of, to_artifact,
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


OLD_TABLE = """<table><tr>
<td style="text-align: center;">76</td><td style="text-align: center;">6/28/18</td>
<td style="text-align: center; white-space: nowrap;">17-1364</td>
<td><a href='/opinions/preliminaryprint/585US2PP_final.pdf#page=474' target='_blank' title="x">North Carolina v. Covington</a></td>
<td style="text-align: center;">PC</td><td style="text-align: center;"><span>585 U.S. 969</span></td></tr>
<tr><td>1</td><td>10/14/2026</td><td>26A476</td>
<td><a href='/opinions/26pdf/26a476_k537.pdf' target='_blank'>FCC v. Brown</a></td><td>PC</td><td></td></tr>
<tr><td>2</td><td>Links</td><td><a href='../../publicinfo/PIOServices.pdf'>x</a></td><td></td></tr>
</table>"""


def test_parse_preliminary_print_rows_and_four_digit_years():
    old, new = parse_term_table(OLD_TABLE, "https://www.supremecourt.gov/opinions/slipopinion/17")
    assert old["pdf"] == "https://www.supremecourt.gov/opinions/preliminaryprint/585US2PP_final.pdf"
    assert old["page"] == 474 and old["us_cite"] == ("585", "969")
    assert old["date"] == "2018-06-28"
    assert new["date"] == "2026-10-14" and "page" not in new
    a = to_artifact(old, None, None)
    assert a["id"] == "scotus/585-us-969"
    assert a["sources"][0]["url"].endswith("585US2PP_final.pdf#page=474")


def test_print_ranges_and_pages():
    rows = [{"pdf": "p", "page": 10}, {"pdf": "p", "page": 3}, {"pdf": "q", "page": 5},
            {"pdf": "r"}]
    assert print_ranges(rows) == {("p", 3): 10, ("p", 10): None, ("q", 5): None}

    class Page:
        def __init__(self, t):
            self.t = t

        def extract_text(self):
            return self.t

    class Reader:
        pages = [Page(f"page {n}") for n in range(1, 8)] + [Page("ORDERS FROM JUNE 2018\n...")]

    assert print_pages(Reader, 3, 6) == ["page 3", "page 4", "page 5"]
    assert print_pages(Reader, 6, None) == ["page 6", "page 7"]


def test_split_opinions_bound_volume_mixed_case():
    pages = [
        "86 OCTOBER TERM, 2014\nSyllabus\nKERRY v. DIN\nNo. 13–1402. Argued February 23, 2015—Decided June 15, 2015",
        "87\nCite as: 576 U. S. 86 (2015)\nSyllabus\nJustice Scalia announced the judgment of the Court.",
        "88 KERRY v. DIN\nJustice Scalia announced the judgment of the Court and delivered an opinion.",
        "89\nCite as: 576 U. S. 86 (2015)\nOpinion of Scalia, J.\nThe plurality reasons.",
        "90 KERRY v. DIN\nKennedy, J., concurring in judgment\nJustice Kennedy, concurring.",
        "91 KERRY v. DIN\nBreyer, J., dissenting\nJustice Breyer dissents.",
    ]
    syllabus, parts = split_opinions(pages)
    assert "No. 13–1402" in syllabus
    assert [label for label, _ in parts] == [
        "Opinion of Scalia, J.", "Kennedy, J., concurring in judgment", "Breyer, J., dissenting"]
    assert "delivered an opinion" in parts[0][1]
    assert kind_of(parts[0][0]) == "plurality" and author_of(parts[0][0]) == "Scalia"
    assert kind_of(parts[2][0]) == "dissent" and author_of(parts[2][0]) == "Breyer"


def test_split_opinions_short_per_curiam():
    pages = ["610 OCTOBER TERM, 2019\nSyllabus\nCOLORADO DEPT. OF STATE v. BACA\nNo. 19–518. Decided July 6, 2020\n"
             "Per Curiam.\nThe judgment is reversed."]
    _, parts = split_opinions(pages)
    assert len(parts) == 1 and "reversed" in parts[0][1]
