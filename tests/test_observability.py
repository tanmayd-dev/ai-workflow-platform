"""Observability: request-id middleware, readiness probe, structured logging.

Covers:
- X-Request-ID generation/echo/uniqueness on the middleware (app.main).
- GET /ready: real DB probe (mandatory) and the chroma check whose status is
  either ok or "skipped" when no client exists (never force-loads the model).
- JSON log formatter keys and request-id injection via the contextvar.
- configure_logging() idempotence (no duplicate handlers).
"""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    """HTTP client with lifespan (wires DB init + startup sweep as usual)."""
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# Request id middleware
# --------------------------------------------------------------------------

def test_request_id_header_generated(client):
    """Every response carries an X-Request-ID when none was supplied."""
    response = client.get("/health")
    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert request_id  # a minted uuid4 hex token
    assert len(request_id) == 32  # uuid4().hex


def test_request_id_behavior_incoming_echoed_and_unique(client):
    """A provided X-Request-ID is echoed verbatim; generated ids are unique."""
    with_token = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert with_token.headers["X-Request-ID"] == "trace-abc-123"

    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]
    assert first != second


# --------------------------------------------------------------------------
# /ready readiness probe
# --------------------------------------------------------------------------

def test_ready_reports_db_check(client):
    """/ready runs a real SELECT 1 and reports the db check."""
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["db"] == "ok"
    # chroma is attempted only when a client already exists; both outcomes are
    # acceptable here ("skipped" when the service is lazy and unloaded).
    assert body["checks"]["chroma"] in ("ok", "skipped")


def test_ready_returns_503_when_db_probe_raises(monkeypatch):
    """When the mandatory DB probe fails, readiness degrades to 503."""
    from app import main

    async def broken_database() -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main, "_check_database", broken_database)
    with TestClient(main.app) as test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["db"].startswith("fail:")
    assert "connection refused" in body["checks"]["db"]


def test_ready_returns_503_when_existing_chroma_client_fails(monkeypatch):
    """A live client that fails its heartbeat marks readiness unavailable."""
    from app import main

    monkeypatch.setattr(main, "_probe_chroma", lambda: "fail: heartbeat exploded")
    with TestClient(main.app) as test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["chroma"] == "fail: heartbeat exploded"


def test_ready_chroma_probe_skips_without_building_client(monkeypatch):
    """When no client exists yet, the probe reports "skipped" without building one."""
    from app import main
    from app.main import _probe_chroma

    calls: list[str] = []

    class _LazyStub:
        """Emulates a lazy RAGService: no client built, accessor on demand."""

        _client = None

        def _get_client(self):
            calls.append("client_built")
            return object()

    monkeypatch.setattr(main, "_get_cached_rag_service", lambda: None)
    assert _probe_chroma() == "skipped"

    monkeypatch.setattr(main, "_get_cached_rag_service", lambda: _LazyStub())
    assert _probe_chroma() == "skipped"
    assert calls == []  # the on-demand accessor was never invoked


# --------------------------------------------------------------------------
# Structured logging
# --------------------------------------------------------------------------

def _make_record(**kwargs) -> logging.LogRecord:
    defaults = dict(
        name="app.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="payload %(value)s",
        args=({"value": "x"},),
        exc_info=None,
    )
    defaults.update(kwargs)
    return logging.LogRecord(**defaults)


def test_json_formatter_emits_required_keys():
    """A record formats to a JSON line with the required keys."""
    from app.core.logging import JsonFormatter, RequestIdFilter

    record = _make_record()
    assert RequestIdFilter().filter(record)
    formatted = JsonFormatter().format(record)

    data = json.loads(formatted)
    assert set(data) >= {"ts", "level", "logger", "request_id", "message"}
    assert data["level"] == "ERROR"
    assert data["logger"] == "app.test"
    assert data["message"] == "payload x"
    assert data["request_id"] == "-"  # no request id in this context


def test_json_formatter_includes_assigned_request_id():
    """Once a request id is set on the contextvar, it lands in the record."""
    from app.core.logging import (
        JsonFormatter,
        RequestIdFilter,
        reset_request_id,
        set_request_id,
    )

    token = set_request_id("req-4815162342")
    try:
        record = _make_record()
        assert RequestIdFilter().filter(record)
        data = json.loads(JsonFormatter().format(record))
        assert data["request_id"] == "req-4815162342"
    finally:
        reset_request_id(token)


def test_configure_logging_is_idempotent(monkeypatch):
    """configure_logging() adds exactly one root handler across calls."""
    from app.core import logging as core_logging

    core_logging._configured = False
    monkeypatch.setattr(core_logging, "_configured", False)

    root = logging.getLogger()
    before = len(root.handlers)
    core_logging.configure_logging()
    after_first = len(root.handlers)
    core_logging.configure_logging()
    after_second = len(root.handlers)

    assert after_second == after_first, "guard flag must prevent double handlers"
    # The first call adds (at most) one handler; a prior test's TestClient
    # lifespan may have configured logging already, in which case none.
    assert after_first in (before, before + 1)
