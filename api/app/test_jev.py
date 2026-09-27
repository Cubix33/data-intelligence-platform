"""Unit tests for the Jev decision layer, using a fake HTTP transport.

Run: pytest api/app/test_jev.py -v
"""
from __future__ import annotations

import httpx
import pytest

from app import jev, config


def _fake_transport(answers_by_call):
    """Returns an httpx.MockTransport cycling through canned answer dicts."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(answers_by_call) - 1)
        calls["n"] += 1
        return httpx.Response(200, json={"answers": answers_by_call[i], "model": "jev-latest",
                                          "usage": {"input_tokens": 100}})

    return httpx.MockTransport(handler), calls


@pytest.fixture(autouse=True)
def _enable_jev(monkeypatch):
    monkeypatch.setattr(config, "JEV_API_KEY", "fake-key")
    monkeypatch.setattr(config, "SCOUT_JEV_ENABLED", True)
    yield


def test_page_gate_below_threshold_reports_low_noul(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return httpx.Response(200, json={"answers": {"lists_entities": {"type": "noul", "noul": 0.12}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    noul = jev.page_gate("Indian edtech startups", ["seed funding"], "https://x.com", "irrelevant nav text")
    assert noul == pytest.approx(0.12)


def test_page_gate_raises_jev_unavailable_on_5xx(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(jev.JevUnavailable):
        jev.page_gate("x", [], "https://x.com", "text")


def test_page_gate_raises_jev_unavailable_on_connection_error(monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(jev.JevUnavailable):
        jev.page_gate("x", [], "https://x.com", "text")


def test_claim_support_batch_handles_list_formatting():
    """Regression test for the exact FinX investors bug from ADDITION.md."""
    def fake_post(url, json, headers, timeout):
        return httpx.Response(200, json={"answers": {
            "c0": {"type": "noul", "noul": 0.90},
            "c1": {"type": "noul", "noul": 0.45},
        }})

    import app.jev as jev_mod
    orig_post = httpx.post
    httpx.post = fake_post
    try:
        scores = jev_mod.claim_support_batch([
            {"entity": "FinX", "field": "investors", "field_description": "investors",
             "value": "Gokul Rajaram, Amit Singhal", "quote": "Gokul Rajaram\nAmit Singhal"},
            {"entity": "Monster Energy", "field": "company_name", "field_description": "sponsor name",
             "value": "Monster Energy", "quote": "Monster Energy"},
        ])
    finally:
        httpx.post = orig_post
    assert scores[0] == pytest.approx(0.90)
    assert scores[1] == pytest.approx(0.45)


def test_filter_check_returns_per_record_per_filter_grid(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return httpx.Response(200, json={"answers": {
            "r0_f0": {"type": "noul", "noul": 0.9},
            "r0_f1": {"type": "noul", "noul": 0.1},
        }})

    monkeypatch.setattr(httpx, "post", fake_post)
    rows = jev.filter_check([{"name": "FinX", "evidence": "..."}], ["seed", "edtech"])
    assert rows == [[0.9, 0.1]]


def test_jev_disabled_short_circuits(monkeypatch):
    monkeypatch.setattr(config, "JEV_API_KEY", None)
    with pytest.raises(jev.JevUnavailable):
        jev.page_gate("x", [], "u", "t")
