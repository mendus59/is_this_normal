from itn.ingest.congress_laws import (
    contentious, counts_from_text, counts_from_xml, current_congress, is_final_passage, to_artifact,
)

BILL = {"congress": 117, "type": "HR", "number": "5376", "title": "Inflation Reduction Act of 2022"}


def test_current_congress():
    assert current_congress(2000) == 106
    assert current_congress(2026) == 119


def test_final_passage_detection():
    assert is_final_passage("On passage Passed by the Yeas and Nays: 220 - 207 (Roll no. 420).")
    assert is_final_passage("Passed Senate with an amendment by Yea-Nay Vote. 51 - 50. Record Vote Number: 325.")
    assert is_final_passage("On motion to suspend the rules and pass the bill Agreed to by the Yeas and Nays: (2/3 required): 402 - 15 (Roll no. 88).")
    assert is_final_passage("Senate agreed to conference report by Yea-Nay Vote. 51 - 48.")
    assert is_final_passage("Resolving differences -- Senate actions: Senate concurred in House amendment by Yea-Nay Vote. 60 - 39.")
    assert is_final_passage("On motion that the House agree to the Senate amendments Agreed to by recorded vote: 219 - 212 (Roll no. 165).")
    assert is_final_passage("On motion to suspend the rules and agree to the Senate amendment Agreed to by the Yeas and Nays: (2/3 required): 300 - 100 (Roll no. 12).")
    assert is_final_passage("On motion that the House agree with an amendment to the Senate amendment Agreed to by recorded vote: 220 - 205 (Roll no. 40).")
    assert is_final_passage("Senate agreed to the House amendment to the Senate amendment by Yea-Nay Vote. 60 - 39.")
    assert not is_final_passage("Cloture motion on the motion to proceed invoked in Senate by Yea-Nay Vote. 60 - 40.")
    assert not is_final_passage("On motion to recommit with instructions Failed by the Yeas and Nays: 200 - 220.")


def test_counts_from_text():
    assert counts_from_text("On passage Passed by recorded vote: 220 - 207 (Roll no. 420).") == (220, 207)
    assert counts_from_text("Agreed to by the Yeas and Nays: (2/3 required): 402 - 15 (Roll no. 88).") == (402, 15)
    assert counts_from_text("Passed Senate without amendment by Voice Vote.") is None


def test_counts_from_xml():
    house = "<rollcall-vote><vote-metadata><vote-totals><totals-by-vote><yea-total>220</yea-total><nay-total>207</nay-total></totals-by-vote></vote-totals></vote-metadata></rollcall-vote>"
    senate = "<roll_call_vote><count><yeas>51</yeas><nays>50</nays></count></roll_call_vote>"
    assert counts_from_xml(house, "House") == (220, 207)
    assert counts_from_xml(senate, "Senate") == (51, 50)


def test_contentious_threshold():
    assert contentious([{"yeas": 220, "nays": 207}, {"yeas": 51, "nays": 50}])
    assert not contentious([{"yeas": 402, "nays": 15}, {"yeas": 98, "nays": 0}])
    assert contentious([{"yeas": 402, "nays": 15}, {"yeas": 60, "nays": 39}])  # 60.6% in one chamber
    assert not contentious([])  # voice vote or unanimous consent


def test_contentious_law_is_in_scope():
    votes = [{"chamber": "Senate", "yeas": 51, "nays": 50}, {"chamber": "House", "yeas": 220, "nays": 207}]
    a = to_artifact(BILL, "117-169", "2022-08-16", "Summary text", votes)
    assert a["id"] == "pl/117-169"
    assert a["in_scope"] is True and a["contentious"] is True
    assert a["metadata"]["bill"] == "hr5376-117"
    assert a["sources"][0]["url"] == "https://www.congress.gov/bill/117th-congress/house-bill/5376"


def test_post_office_naming_is_reference_only():
    bill = {"congress": 118, "type": "HR", "number": "1060", "title": "To designate the facility of the United States Postal Service..."}
    a = to_artifact(bill, "118-60", "2024-05-01", None, [])
    assert a["contentious"] is False and a["in_scope"] is False


def test_pre_2000_law_not_in_scope():
    a = to_artifact({**BILL, "congress": 106}, "106-50", "1999-08-17", None, [{"yeas": 51, "nays": 49}])
    assert a["in_scope"] is False


def test_ordinal():
    from itn.ingest.congress_laws import ordinal
    assert [ordinal(n) for n in (111, 112, 113, 119, 121, 122, 123)] == ["111th", "112th", "113th", "119th", "121st", "122nd", "123rd"]
