from itn.ingest.federal_register import to_artifact

EO = {
    "document_number": "2021-10460",
    "title": "Improving the Nation's Cybersecurity",
    "type": "Presidential Document",
    "subtype": "Executive Order",
    "executive_order_number": 14028,
    "signing_date": "2021-05-12",
    "publication_date": "2021-05-17",
    "html_url": "https://www.federalregister.gov/documents/2021/05/17/2021-10460/improving-the-nations-cybersecurity",
    "pdf_url": "https://www.govinfo.gov/content/pkg/FR-2021-05-17/pdf/2021-10460.pdf",
    "raw_text_url": "https://www.federalregister.gov/documents/full_text/text/2021/05/17/2021-10460.txt",
    "abstract": None,
    "citation": "86 FR 26633",
    "president": {"name": "Joseph R. Biden Jr.", "identifier": "joe-biden"},
    "disposition_notes": None,
}


def test_executive_order_keyed_by_number():
    a = to_artifact(EO)
    assert a["id"] == "eo/14028"
    assert a["subtype"] == "executive_order"
    assert a["effective_date"] == "2021-05-12"
    assert a["in_scope"] is True
    assert a["metadata"]["president"] == "Joseph R. Biden Jr."
    assert [s["label"] for s in a["sources"]] == ["Federal Register", "PDF"]


def test_proclamation_keyed_by_document_number():
    doc = {**EO, "subtype": "Proclamation", "executive_order_number": None}
    a = to_artifact(doc)
    assert a["id"] == "fr/2021-10460"
    assert a["subtype"] == "proclamation"


def test_pre_2000_is_reference_only():
    a = to_artifact({**EO, "signing_date": "1998-06-01"})
    assert a["in_scope"] is False


def test_get_retries_after_rate_limit(monkeypatch):
    import httpx

    from itn import http

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        resp = http.get(client, "https://example.test/doc.txt")
    assert resp.status_code == 200
    assert len(calls) == 2
