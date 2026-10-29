"""Focused tests for intent-based workflow drafting (issue #15).

``LLMService.draft_workflow`` only builds the prompt; parsing happens in
``POST /api/v1/intents/draft``. These tests cover both layers with a fake
transport, so no OpenRouter/network call is made.

The endpoint currently validates the LLM output for JSON *syntax* (with a
markdown-fence fallback) but not against the workflow schema; the strict
xfail at the bottom records that gap (see issue #15 report).
"""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.llm_service import LLMService

from conftest import grant_role

INTENT = "Draft a review workflow for high-value fraud transactions"
DOMAIN = "fraud"

VALID_DRAFT = {
    "name": "High Value Fraud Review",
    "description": "Analyze and approve high-value transactions",
    "steps": [
        {
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Risk Analysis",
            "description": "Assess transaction risk",
            "config_json": {
                "rag_query": "fraud escalation policy",
                "prompt_template": "Analyze this transaction for fraud",
            },
        },
        {
            "order_index": 1,
            "step_type": "HUMAN_APPROVAL",
            "name": "Manager Approval",
            "description": "Human review before settlement",
            "config_json": {},
        },
        {
            "order_index": 2,
            "step_type": "DOMAIN_RULE",
            "name": "Threshold Check",
            "description": "Apply the high-risk rule",
            "config_json": {"rule_id": "high_risk_threshold"},
        },
    ],
}

DOC_TEXT = "Fraud policy: transactions above 10000 USD require manual approval."


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _author_headers(client: TestClient) -> dict:
    email = f"intent_author_{uuid4().hex[:8]}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Intent Author",
    })
    assert register.status_code == 201, register.text
    grant_role(email, "author")

    login = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "testpass123",
    })
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_provider_rejection_is_mapped_not_a_500(client, monkeypatch):
    """An upstream provider failure (e.g. a bad OPENROUTER_API_KEY producing
    401, or quota/network errors) must surface as a clear 502 — never a raw
    500 — so the console can tell the user what to fix."""
    from app.services.llm_service import LLMProviderError

    async def fake_query_similar(self, query_text, domain, top_k=3):
        return []

    async def raising_generate_completion(self, *args, **kwargs):
        # generate_completion itself maps AuthenticationError to
        # LLMProviderError; simulate the post-mapping state at the service
        # boundary the endpoint actually sees.
        raise LLMProviderError(
            "the LLM provider rejected the API key (401 Unauthorized); "
            "check OPENROUTER_API_KEY"
        )

    monkeypatch.setattr(
        "app.services.rag_service.RAGService.query_similar", fake_query_similar
    )
    monkeypatch.setattr(
        "app.services.llm_service.LLMService.generate_completion",
        raising_generate_completion,
    )

    response = client.post(
        "/api/v1/intents/draft",
        headers=_author_headers(client),
        json={"intent": INTENT, "domain": DOMAIN},
    )
    assert response.status_code == 502, response.text
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def test_generate_completion_maps_auth_error_to_provider_error(monkeypatch):
    """The OpenAI SDK's 401 AuthenticationError must be translated into
    LLMProviderError (message mentions OPENROUTER_API_KEY, raw error hidden)."""
    import asyncio
    import openai

    from app.services.llm_service import LLMProviderError

    class FakeCompletions:
        async def create(self, **kwargs):
            # openai's 401 error: httpx.Response + message
            request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
            response = httpx.Response(401, request=request)
            raise openai.AuthenticationError(
                "invalid api key", response=response, body=None
            )

    class FakeClient:
        def __init__(self):
            self.chat = type("Chat", (), {})()
            self.chat.completions = FakeCompletions()

    service = LLMService()
    service.client = FakeClient()

    with pytest.raises(LLMProviderError) as excinfo:
        asyncio.run(service.generate_completion("system", "user"))

    assert "OPENROUTER_API_KEY" in str(excinfo.value)
    assert "invalid api key" not in str(excinfo.value)  # upstream text never leaks


def test_rag_disabled_gates_heavy_paths():
    """With ENABLE_RAG=false the service must answer fast paths without ever
    constructing the chroma client or loading the embedding model — a native
    failure during that load can kill the whole process."""
    import asyncio as _asyncio

    from app.services.rag_service import RAGService

    import app.services.rag_service as rag_module

    original = rag_module.settings.ENABLE_RAG
    try:
        rag_module.settings.ENABLE_RAG = False
        service = RAGService()
        assert _asyncio.run(service.query_similar("q", "fraud")) == []
        assert _asyncio.run(
            service.ingest_document("text", "id-1", "f.txt", "fraud")
        ) == 0
        assert _asyncio.run(service.delete_document("fraud", "id-1")) == 0
        # The heavy machinery was never touched.
        assert service._client is None
        assert service._embeddings is None
    finally:
        rag_module.settings.ENABLE_RAG = original


def _install_intent_fakes(monkeypatch, content, *, docs, captured):
    """Fake RAG retrieval and the LLM transport for the endpoint under test."""

    async def fake_query_similar(self, query_text, domain, top_k=3):
        captured["rag"] = {
            "query_text": query_text,
            "domain": domain,
            "top_k": top_k,
        }
        return docs

    async def fake_generate_completion(
        self,
        system_prompt,
        user_prompt,
        context=None,
        temperature=0.7,
        max_tokens=2000,
    ):
        captured["llm"] = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "context": context,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        return {
            "content": content,
            "prompt_snapshot": "SYSTEM/USER snapshot",
            "model_name": "mock-model",
            "temperature": temperature,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    monkeypatch.setattr(
        "app.services.rag_service.RAGService.query_similar", fake_query_similar
    )
    monkeypatch.setattr(
        "app.services.llm_service.LLMService.generate_completion",
        fake_generate_completion,
    )


async def test_draft_workflow_sends_expected_prompt_contract(monkeypatch):
    """Unit-level prompt contract for LLMService.draft_workflow."""
    captured: dict = {}

    async def fake_generate_completion(
        self,
        system_prompt,
        user_prompt,
        context=None,
        temperature=0.7,
        max_tokens=2000,
    ):
        captured.update({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "context": context,
            "temperature": temperature,
        })
        return {"content": json.dumps(VALID_DRAFT), "model_name": "mock-model"}

    monkeypatch.setattr(LLMService, "generate_completion", fake_generate_completion)

    result = await LLMService().draft_workflow(
        intent=INTENT,
        domain=DOMAIN,
        retrieved_context=DOC_TEXT,
    )
    assert result == {"content": json.dumps(VALID_DRAFT), "model_name": "mock-model"}

    system_prompt = captured["system_prompt"]
    assert "expert workflow designer" in system_prompt
    assert f"Domain: {DOMAIN}" in system_prompt
    # The expected output schema is spelled out for the model.
    for key in ('"name"', '"description"', '"steps"', '"order_index"',
                '"step_type"', '"config_json"', '"rag_query"', '"rule_id"',
                '"prompt_template"'):
        assert key in system_prompt
    assert "MUST include at least one HUMAN_APPROVAL step" in system_prompt

    user_prompt = captured["user_prompt"]
    assert f"User Intent: {INTENT}" in user_prompt
    assert f"Regulatory Context:\n{DOC_TEXT}" in user_prompt
    assert "Design a compliant workflow." in user_prompt

    # Structured-output temperature; RAG context is embedded in the user prompt,
    # so no separate ``context`` argument is passed to the transport.
    assert captured["temperature"] == 0.3
    assert captured["context"] is None


def test_intent_draft_returns_parsed_draft_and_retrieved_context(client, monkeypatch):
    """Valid JSON from the model becomes IntentResponse.workflow_draft."""
    captured: dict = {}
    docs = [
        {
            "text": DOC_TEXT,
            "metadata": {"filename": "fraud_policy.md"},
            "distance": 0.12,
        }
    ]
    _install_intent_fakes(
        monkeypatch, json.dumps(VALID_DRAFT), docs=docs, captured=captured
    )
    headers = _author_headers(client)

    response = client.post(
        "/api/v1/intents/draft",
        headers=headers,
        json={"intent": INTENT, "domain": DOMAIN},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_draft"] == VALID_DRAFT
    assert body["retrieved_context"] == f"[Doc: fraud_policy.md]\n{DOC_TEXT}"

    # Retrieval used the request values; the LLM saw the formatted context.
    assert captured["rag"] == {"query_text": INTENT, "domain": DOMAIN, "top_k": 3}
    assert captured["llm"]["temperature"] == 0.3
    assert captured["llm"]["context"] is None
    assert DOC_TEXT in captured["llm"]["user_prompt"]
    assert f"User Intent: {INTENT}" in captured["llm"]["user_prompt"]


def test_intent_draft_accepts_markdown_fenced_json(client, monkeypatch):
    """The documented fallback: ```json-fenced output is still parsed."""
    captured: dict = {}
    fenced = "```json\n" + json.dumps(VALID_DRAFT) + "\n```"
    _install_intent_fakes(monkeypatch, fenced, docs=[], captured=captured)
    headers = _author_headers(client)

    response = client.post(
        "/api/v1/intents/draft",
        headers=headers,
        json={"intent": INTENT, "domain": DOMAIN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["workflow_draft"] == VALID_DRAFT
    assert response.json()["retrieved_context"] == ""


def test_intent_draft_malformed_json_returns_clear_error(client, monkeypatch):
    """Non-JSON model output is a 500 with a specific, stable message."""
    captured: dict = {}
    _install_intent_fakes(
        monkeypatch,
        "I am sorry, I cannot produce a workflow for that request.",
        docs=[],
        captured=captured,
    )
    headers = _author_headers(client)

    response = client.post(
        "/api/v1/intents/draft",
        headers=headers,
        json={"intent": INTENT, "domain": DOMAIN},
    )
    assert response.status_code == 500, response.text
    assert response.json()["detail"] == (
        "Failed to parse workflow draft from LLM response"
    )


def test_intent_draft_rejects_missing_required_keys(client, monkeypatch):
    """A schema-incomplete draft must not be handed back as a valid workflow."""
    captured: dict = {}
    _install_intent_fakes(
        monkeypatch,
        json.dumps({"name": "Incomplete Draft"}),
        docs=[],
        captured=captured,
    )
    headers = _author_headers(client)

    response = client.post(
        "/api/v1/intents/draft",
        headers=headers,
        json={"intent": INTENT, "domain": DOMAIN},
    )
    assert response.status_code in (400, 422), (
        f"schema-invalid draft accepted with {response.status_code}: {response.text}"
    )
