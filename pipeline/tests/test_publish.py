from itn import ids
from itn.publish.export import band, percentiles


def test_percentiles_span_0_to_100():
    p = percentiles({"a": 0.1, "b": 0.5, "c": 0.9})
    assert p == {"a": 0.0, "b": 50.0, "c": 100.0}


def test_single_score_is_midpoint():
    assert percentiles({"a": 0.3}) == {"a": 50.0}


def test_bands():
    assert band(95) == "routine"
    assert band(60) == "ordinary"
    assert band(45) == "unusual"
    assert band(20) == "novel"
    assert band(3) == "unprecedented"


def test_ids():
    assert ids.public_law(117, 169) == "pl/117-169"
    assert ids.scotus_case(597, 215) == "scotus/597-us-215"
    assert ids.usc_section(42, "1983", 2003) == "usc/42/1983@2003"


def test_r2_settings_lists_every_missing_name(monkeypatch):
    import pytest
    from itn.config import MissingSetting, R2Settings

    monkeypatch.setenv("R2_BUCKET", "b")
    for n in ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]:
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(MissingSetting, match="R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"):
        R2Settings.from_env()
