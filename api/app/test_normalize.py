
from . import config, db, llm, pipeline
from .models import DataSpec, FieldSpec
from .normalize import normalize_date, normalize_number, normalize_value, single_year


def test_normalize_date_common_formats():
    assert normalize_date("10 July 2025") == "2025-07-10"
    assert normalize_date("July 10, 2025") == "2025-07-10"
    assert normalize_date("10th Jul 2025") == "2025-07-10"
    assert normalize_date("May 2024") == "2024-05"
    assert normalize_date("2025-12-01") == "2025-12-01"
    assert normalize_date("2025-12-01T10:00:00Z") == "2025-12-01"
    assert normalize_date("2024") == "2024"


def test_normalize_date_never_invents_a_year():
    assert normalize_date("Jan 20") == "Jan 20"
    assert normalize_date("Jan 20", year_hint=2025) == "2025-01-20"
    # Ambiguous numeric dates and impossible dates are left untouched.
    assert normalize_date("01/02/2025") == "01/02/2025"
    assert normalize_date("Feb 30 2025") == "Feb 30 2025"
    assert normalize_date("sometime soon") == "sometime soon"


def test_normalize_number_magnitudes():
    assert normalize_number("671B") == 671_000_000_000
    assert normalize_number("671 billion parameters") == 671_000_000_000
    assert normalize_number("1.5T") == 1_500_000_000_000
    assert normalize_number("$4 million") == 4_000_000
    assert normalize_number("₹2.5 crore") == 25_000_000
    assert normalize_number("1,250") == 1250
    assert normalize_number("0.5") == 0.5
    assert normalize_number(42) == 42


def test_normalize_number_leaves_ambiguous_values_alone():
    for v in ("7B/70B", "7B, 70B", "3.0.14", "45%", "Qwen 2.5", "about a dozen"):
        assert normalize_number(v) == v


def test_normalize_value_dispatches_on_field_type():
    assert normalize_value("671B", "number") == 671_000_000_000
    assert normalize_value("May 2024", "date") == "2024-05"
    assert normalize_value("671B", "string") == "671B"
    assert normalize_value(None, "date") is None


def test_single_year():
    assert single_year("release_year:2025") == 2025
    assert single_year("released in 2024 or 2025") is None
    assert single_year("no year here") is None


def test_differently_written_dates_now_corroborate(tmp_path, monkeypatch):
    """"Jan 20" and "2025-01-20" used to look like a conflict; normalised they agree."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delattr(db._local, "conn", raising=False)
    db.create_run("r1", "p")
    a = normalize_date("Jan 20", 2025)
    b = normalize_date("2025-01-20")
    db.upsert_record("r1", "deepseek r1", {"release_date": a}, {"release_date": {"url": "u1"}}, 1.0)
    db.upsert_record("r1", "deepseek r1", {"release_date": b}, {"release_date": {"url": "u2"}}, 1.0)
    rec = db.list_records("r1")[0]
    assert rec["provenance"]["release_date"].get("corroborated_by") == "u1"
    assert not rec["provenance"]["release_date"].get("conflicts")


def test_empty_run_fails_loudly(tmp_path, monkeypatch):
    """A run that finds nothing must end `failed` with a readable error, not `done`."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delattr(db._local, "conn", raising=False)
    spec = DataSpec(
        entity="thing", summary="things",
        fields=[FieldSpec(name="name", description="name")],
        search_queries=["q1", "q2"],
    )
    monkeypatch.setattr(llm, "parse_intent", lambda prompt: spec)
    monkeypatch.setattr(llm, "discover_urls_for_query", lambda *a, **k: [])
    monkeypatch.setattr(llm, "replan_queries", lambda *a, **k: [])
    db.create_run("r2", "find things")
    pipeline.run_pipeline("r2", "find things")
    run = db.get_run("r2")
    assert run["status"] == "failed"
    assert "no usable records" in run["error"] or "no pages" in run["error"]


def test_conflict_resolution_keeps_numbers_numeric(tmp_path, monkeypatch):
    from . import truth
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delattr(db._local, "conn", raising=False)
    db.create_run("r3", "p")
    db.upsert_record("r3", "m", {"params": 7_000_000_000},
                     {"params": {"url": "u1", "raw_value": "7B", "conflicts": [{"url": "u2", "value": 8_000_000_000}]}}, 1.0)
    for url, val, score in (("u1", 7_000_000_000, 0.2), ("u2", 8_000_000_000, 0.9)):
        db.insert_claim(run_id="r3", entity_id="m", field="params", value_raw=str(val), value_norm=str(val),
                        source_url=url, quote="q", support_score=score, capture_id="c", extractor="t")
    truth.resolve_run_conflicts("r3")
    rec = db.list_records("r3")[0]
    assert isinstance(rec["fields"]["params"], int)
