"""Focused tests for RAG ingestion/retrieval and the compiler RAG path (issue #15).

These exercise the real ``RAGService`` against the throwaway Chroma directory
configured by ``conftest.py`` (``CHROMA_DB_DIR``). Embeddings run locally from
the cached sentence-transformers model: no API calls are made.

Every test uses a fresh random domain, because ``RAGService`` keeps one
process-wide in-memory Chroma client (``chromadb.Client``) for the test run.
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.compiler as compiler_module
from app.core.config import get_settings
from app.models.workflow import (
    StepType,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowStep,
)
from app.services.compiler import WorkflowCompiler, WorkflowState
from app.services.rag_service import RAGService


def _unique_domain(prefix: str = "rag") -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _policy_text() -> str:
    clauses = [
        (
            f"Policy clause {i}: high value transactions above 10000 USD require "
            "manual approval by the fraud desk before settlement."
        )
        for i in range(40)
    ]
    return " ".join(clauses)


def _ai_workflow(domain: str, rag_query: str) -> WorkflowDefinition:
    workflow = WorkflowDefinition(
        id=uuid4(),
        name=f"RAG Workflow {uuid4().hex[:8]}",
        description="RAG compiler test workflow",
        domain=domain,
        version=1,
        status=WorkflowStatus.DRAFT,
        created_by=uuid4(),
    )
    workflow.steps = [
        WorkflowStep(
            id=uuid4(),
            order_index=0,
            step_type=StepType.AI_ANALYSIS,
            name="Risk Analysis",
            description="AI analysis with knowledge-base context",
            config_json={
                "rag_query": rag_query,
                "prompt_template": "Assess the transaction for fraud risk",
            },
        )
    ]
    return workflow


def test_rag_backend_is_configured_to_persist():
    """CHROMA_DB_DIR is meant to keep ingested documents across restarts."""
    service = RAGService()
    assert service.client.get_settings().is_persistent is True


async def test_ingest_document_chunks_and_query_similar():
    """Ingest splits/embeds a document; query returns scored, tagged chunks."""
    settings = get_settings()  # keeps the throwaway-dir assertion honest
    assert settings.CHROMA_DB_DIR  # configured by conftest before app import

    service = RAGService()
    domain = _unique_domain("rag_ingest")
    document_id = str(uuid4())
    content = _policy_text()

    chunk_count = await service.ingest_document(
        content=content,
        document_id=document_id,
        filename="fraud_policy.md",
        domain=domain,
    )
    assert chunk_count >= 2, "a >4000 character document must be split into chunks"

    results = await service.query_similar(
        query_text="manual approval for high value transactions",
        domain=domain,
        top_k=chunk_count,
    )

    assert len(results) == chunk_count
    assert {result["metadata"]["chunk_index"] for result in results} == set(
        range(chunk_count)
    )
    for result in results:
        assert set(result) == {"text", "metadata", "distance"}
        assert result["text"] in content
        assert result["metadata"]["document_id"] == document_id
        assert result["metadata"]["filename"] == "fraud_policy.md"
        assert result["metadata"]["domain"] == domain
        assert isinstance(result["distance"], (int, float))
        assert result["distance"] >= 0

    # Chroma returns nearest-first, so distances are non-decreasing.
    distances = [result["distance"] for result in results]
    assert distances == sorted(distances)
    assert any("manual approval" in result["text"] for result in results)


async def test_query_empty_collection_returns_no_results():
    """Querying a domain with no ingested documents is an empty list, not an error."""
    service = RAGService()
    results = await service.query_similar(
        query_text="anything at all",
        domain=_unique_domain("rag_empty"),
        top_k=3,
    )
    assert results == []


async def test_compile_time_ai_node_injects_retrieved_context(monkeypatch):
    """With ENABLE_RAG=true, retrieved chunks reach the LLM prompt."""
    domain = _unique_domain("rag_compile")
    marker = f"RAG_CONTEXT_MARKER_{uuid4().hex}"
    service = RAGService()
    await service.ingest_document(
        content=(
            f"Regulatory policy {marker}: accounts with a risk score above 0.8 "
            "must be escalated to a human reviewer."
        ),
        document_id=str(uuid4()),
        filename="regulatory_policy.md",
        domain=domain,
    )

    # The compiler reads its module-level settings object.
    monkeypatch.setattr(compiler_module.settings, "ENABLE_RAG", True)

    compiler = WorkflowCompiler()
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="analysis"))],
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, total_tokens=15
            ),
        )

    # Keep the real LLMService message assembly, stub only the network call.
    compiler.llm_service.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    graph = compiler.compile(_ai_workflow(domain, f"{marker} human review"))
    result = await graph.ainvoke(WorkflowState(input_data={"risk_score": 0.9}))

    messages = captured["messages"]
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "user"
    context_messages = [
        message
        for message in messages
        if message["role"] == "system" and marker in message["content"]
    ]
    assert context_messages, f"retrieved context missing from prompt: {messages}"
    assert "Relevant Context from Knowledge Base" in context_messages[0]["content"]

    # The same context is persisted in state/lineage, not just sent to the model.
    assert marker in result["retrieved_contexts"]["Risk Analysis"]
    assert marker in result["ai_prompts"]["Risk Analysis"]
