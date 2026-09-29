from pathlib import Path


def test_dashboard_uses_same_origin_api_base():
    """The deployed dashboard must not resolve API calls on the visitor's device."""
    dashboard = Path(__file__).resolve().parents[2] / "web" / "index.html"
    html = dashboard.read_text(encoding="utf-8")

    assert 'const API = "/api";' in html
    assert "localhost:8000" not in html
