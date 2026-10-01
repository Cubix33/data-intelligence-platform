from types import SimpleNamespace

from .entity_resolution import resolve_entity_key
from .fetcher import _decode
from .llm import relax_query
from .pipeline import _value_in_quote


def test_relax_query_strips_operators():
    q = 'site:inc42.com "seed" "edtech startup" "India" 2024 OR funding -jobs'
    assert relax_query(q) == "seed edtech startup India 2024 funding"


def test_relax_query_plain_unchanged():
    assert relax_query("open source vision language models 2025") == "open source vision language models 2025"


def test_entity_key_merges_taglines_and_parentheticals():
    base = resolve_entity_key("Smart India Hackathon 2025")
    assert resolve_entity_key("Smart India Hackathon (SIH) 2025") == base
    assert resolve_entity_key("Smart India Hackathon 2025: Where Bold Student Ideas Become National Solutions") == base
    assert resolve_entity_key("Django (web framework)") == resolve_entity_key("Django")


def test_value_in_quote_is_whole_token():
    assert not _value_in_quote("61.2", "InternVL2-40B achieved SOTA performance on Video-MME")
    assert not _value_in_quote("2", "scored 12 points")
    assert _value_in_quote("73.6", "R1V2 reaches 73.6 on MMMU")


def test_decode_cp1252_no_replacement_char():
    resp = SimpleNamespace(content="Education’s".encode("cp1252"), charset_encoding=None)
    assert "�" not in _decode(resp) and "Education" in _decode(resp)


def test_value_in_quote_numeric_magnitudes():
    assert _value_in_quote("4000000", "raised $4 million in a seed round")
    assert _value_in_quote("$4M", "Sparkl raised 4 mn dollars")
    assert not _value_in_quote("5000000", "raised $4 million")


def test_match_existing_key_merges_spacing_and_year_variants():
    from .entity_resolution import match_existing_key
    seen = {resolve_entity_key("Smart India Hackathon 2025"), resolve_entity_key("Qwen 2.5 VL")}
    assert match_existing_key(resolve_entity_key("Smart India Hackathon"), seen) == resolve_entity_key("Smart India Hackathon 2025")
    assert match_existing_key(resolve_entity_key("Qwen2.5-VL"), seen) == resolve_entity_key("Qwen 2.5 VL")
    assert match_existing_key(resolve_entity_key("InternVL 2.5 78B"), seen) == resolve_entity_key("InternVL 2.5 78B")


def test_kbt_one_vote_per_url():
    from .truth import resolve_conflicts
    page = "https://www.djangoproject.com/download/"
    claims = [
        {"value_raw": "6.1.1", "value_norm": "6.1.1", "source_url": page, "support_score": 0.9},
        {"value_raw": "3.0.14", "value_norm": "3.0.14", "source_url": page, "support_score": 0.5},
        {"value_raw": "2.2.28", "value_norm": "2.2.28", "source_url": page, "support_score": 0.5},
    ]
    out = resolve_conflicts(claims)
    assert max(out, key=lambda c: c["kbt_prob"])["value_raw"] == "6.1.1"


def test_match_existing_key_keeps_different_years_apart():
    from .entity_resolution import match_existing_key
    seen = {resolve_entity_key("HackWave 2024")}
    assert match_existing_key(resolve_entity_key("HackWave 2025"), seen) == resolve_entity_key("HackWave 2025")
