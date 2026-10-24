"""Intent-based workflow drafting endpoint."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas import IntentRequest, IntentResponse
from app.services.rag_service import RAGService
from app.services.llm_service import LLMService

router = APIRouter(tags=["intents"])


@router.post("/draft", response_model=IntentResponse)
async def draft_from_intent(
    request: IntentRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Generate a workflow draft from natural language intent.
    
    Steps:
    1. Query RAG for relevant documents
    2. Call LLM with intent + context
    3. Return structured workflow definition
    """
    rag_service = RAGService()
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
    llm_result = await llm_service.draft_workflow(
        intent=request.intent,
        domain=request.domain,
        retrieved_context=context_text
    )
    
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
    
    return IntentResponse(
        workflow_draft=workflow_draft,
        retrieved_context=context_text
    )
