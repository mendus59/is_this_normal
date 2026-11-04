from itn.ingest.scotus_bound import page_number, page_offset, to_row, volume_pdfs


def test_to_row():
    case = {"name": "Obergefell v. Hodges", "docket_number": "14-556", "href": "https://api.oyez.org/cases/2014/14-556",
            "citation": {"volume": "576", "page": "644", "year": "2015"},
            "timeline": [{"event": "Granted", "dates": [1421366400]},
                         {"event": "Decided", "dates": [1435276800]}]}
    row = to_row(case)
    assert row["us_cite"] == ("576", "644") and row["date"] == "2015-06-26"
    assert row["docket"] == "14-556"
    assert to_row({**case, "citation": {"volume": "576", "page": None}}) is None


def test_volume_pdfs():
    page = ('<a href="boundvolumes/576bv.pdf">576</a> <a href="/opinions/preliminaryprint/585US1PP.pdf">x</a>'
            '<a href="../publicinfo/x.pdf">y</a>')
    out = volume_pdfs(page, "https://www.supremecourt.gov/opinions/USReports.aspx")
    assert out == {"576": ["https://www.supremecourt.gov/opinions/boundvolumes/576bv.pdf"],
                   "585": ["https://www.supremecourt.gov/opinions/preliminaryprint/585US1PP.pdf"]}


def test_page_numbers_and_offset():
    assert page_number("644 OCTOBER TERM, 2014\nSyllabus") == 644
    assert page_number("Cite as: 576 U. S. 644 (2015) 645\nOpinion of the Court") == 645
    assert page_number("OBERGEFELL v. HODGES\n\nno number here") is None
    numbers = [None, None] + list(range(1, 40))
    assert page_offset(numbers) == 2
    assert page_offset([None] * 30) is None
