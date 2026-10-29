"""Intent-based workflow drafting endpoint."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rbac import require_roles
from app.models.user import User
from app.schemas import IntentRequest, IntentResponse, WorkflowDraft
from app.services.rag_service import get_rag_service
from app.services.llm_service import LLMProviderError, LLMService

router = APIRouter(tags=["intents"])


@router.post("/draft", response_model=IntentResponse)
async def draft_from_intent(
    request: IntentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("author"))
):
    """
    Generate a workflow draft from natural language intent.

    Requires the author role (admin bypasses).

    Steps:
    1. Query RAG for relevant documents
    2. Call LLM with intent + context
    3. Return structured workflow definition
    """
    rag_service = get_rag_service()
    llm_service = LLMService()
    
    # Retrieve relevant context from knowledge base
    similar_docs = await rag_service.query_similar(
        query_text=request.intent,
        domain=request.domain,
        top_k=3
    )
    
    # Format context
    context_text = "\n\n".join([
        f"[Doc: {doc['metadata'].get('filename', 'Unknown')}]\n{doc['text']}"
        for doc in similar_docs
    ])
    
    # Generate workflow draft
    try:
        llm_result = await llm_service.draft_workflow(
            intent=request.intent,
            domain=request.domain,
            retrieved_context=context_text
        )
    except LLMProviderError as exc:
        # Provider failures (bad key, quota, network) are client-actionable
        # configuration problems, not server crashes: answer 502 with the
        # readable reason so the console can display it.
        raise HTTPException(status_code=502, detail=f"LLM provider error: {exc}") from exc
    
    # Parse JSON from LLM response
    import json
    try:
        workflow_draft = json.loads(llm_result["content"])
    except json.JSONDecodeError:
        # If LLM returns markdown-wrapped JSON, try to extract
        content = llm_result["content"]
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
        
        try:
            workflow_draft = json.loads(content.strip())
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=500,
                detail="Failed to parse workflow draft from LLM response"
            )

    # A syntactically valid draft is not necessarily a usable one: reject
    # schema-incomplete output instead of handing it back as a workflow.
    try:
        draft = WorkflowDraft.model_validate(workflow_draft)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "LLM returned an invalid workflow draft",
                "errors": exc.errors(include_url=False, include_context=False),
            },
        )

    return IntentResponse(
        workflow_draft=draft.model_dump(mode="json"),
        retrieved_context=context_text
    )
