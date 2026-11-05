"""Cache writes must come from reported counters, never a previous read hit."""
import pytest

from core.metering import cache_tokens, resolve_usage


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens_details": {"cached_tokens": 32000, "cache_write_tokens": 179}}, (32000, 179)),
    ({"input_tokens_details": {"cached_tokens": 32000, "cache_write_tokens": 179}}, (32000, 179)),
    ({"cache_read_input_tokens": 32000, "cache_creation_input_tokens": 179}, (32000, 179)),
    ({"prompt_tokens_details": {"cached_tokens": 32000}}, (32000, None)),
    ({"prompt_tokens_details": {"cache_write_tokens": 0}}, (None, 0)),
    ({"input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}, (0, 0)),
    ({"cache_read_input_tokens": 0, "prompt_tokens_details": {"cached_tokens": 9}}, (0, None)),
    ({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, (0, 0)),
    ({"prompt_tokens": 100}, (None, None)),
    (None, (None, None)),
])
def test_reported_cache_fields(usage, expected):
    assert cache_tokens(usage) == expected


def test_cache_only_anthropic_usage_is_not_mistaken_for_unknown_usage():
    assert resolve_usage("upstream", {"input_tokens": 0, "output_tokens": 0,
                                     "cache_creation_input_tokens": 100}, [], None) == (100, 0, "upstream")
