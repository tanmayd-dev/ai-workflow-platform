"""Unit tests for app.core.config helpers."""
from app.core.config import parse_cors_origins


def test_parse_cors_origins_empty_string_yields_no_origins():
    """Empty/unset CORS_ORIGINS disables the CORS middleware path entirely."""
    assert parse_cors_origins("") == []
    # Whitespace-only input is treated the same as unset.
    assert parse_cors_origins("   ") == []


def test_parse_cors_origins_csv_split_and_trim():
    """CSV input is split on commas and surrounding whitespace is trimmed."""
    assert parse_cors_origins("http://a,http://b") == ["http://a", "http://b"]
    assert parse_cors_origins(" http://a , http://b ") == ["http://a", "http://b"]


def test_parse_cors_origins_rejects_wildcard():
    """Wildcard origins are rejected (dropped), not passed through.

    Rationale: CORSMiddleware forbids allow_origins=["*"] together with
    allow_credentials=True, so the wildcard would be either rejected at runtime
    or silently broaden credentialed access. We treat wildcard input as
    "no CORS with credentials" — the origin is dropped and, when it was the
    only entry, no CORS middleware is added at all.
    """
    assert parse_cors_origins("*") == []
    # Mixed list keeps only the explicit origins.
    assert parse_cors_origins("*,http://a") == ["http://a"]
