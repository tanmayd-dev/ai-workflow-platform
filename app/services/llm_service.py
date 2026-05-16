"""
LLM Service - Interface to OpenRouter
Handles AI interactions with audit logging.
"""
from typing import Dict, Any, Optional, List
from openai import AsyncOpenAI
from app.core.config import get_settings

settings = get_settings()


class LLMService:
    """Service for interacting with LLM via OpenRouter."""
    
    def __init__(self):
        """Initialize OpenAI client pointing to OpenRouter."""
        self.client = AsyncOpenAI(
            base_url=settings.OPENROUTER_BASE_URL,
            api_key=settings.OPENROUTER_API_KEY
        )
        self.model = settings.OPENROUTER_MODEL
    
    async def generate_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        context: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000
    ) -> Dict[str, Any]:
        """
        Generate a completion from the LLM.
        
        Args:
            system_prompt: System instruction
            user_prompt: User query
            context: Optional RAG context to inject
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            
        Returns:
            dict with 'content', 'prompt_snapshot', 'model_name'
        """
        messages = [{"role": "system", "content": system_prompt}]
        
        # Inject RAG context if provided
        if context:
            messages.append({
                "role": "system",
                "content": f"**Relevant Context from Knowledge Base:**\n{context}"
            })
        
        messages.append({"role": "user", "content": user_prompt})
        
        # Call OpenRouter
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )
        
        # Extract completion
        content = response.choices[0].message.content
        
        # Prepare audit snapshot
        return {
            "content": content,
            "prompt_snapshot": str(messages),  # Full prompt for replay
            "model_name": self.model,
            "temperature": temperature,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens
            }
        }
    
    async def draft_workflow(
        self,
        intent: str,
        domain: str,
        retrieved_context: str
    ) -> Dict[str, Any]:
        """
        Generate a workflow definition from natural language intent.
        
        Args:
            intent: User's natural language intent
            domain: Domain (fraud, kyc, etc.)
            retrieved_context: Relevant regulatory/policy documents
            
        Returns:
            Structured workflow definition as dict
        """
        system_prompt = f"""You are an expert workflow designer for regulated banking operations.
Your task is to design a workflow definition based on user intent and regulatory context.

Domain: {domain}

Output a JSON object with the following structure:
{{
  "name": "Workflow Name",
  "description": "Brief description",
  "steps": [
    {{
      "order_index": 0,
      "step_type": "AI_ANALYSIS" | "DOMAIN_RULE" | "HUMAN_APPROVAL",
      "name": "Step Name",
      "description": "What this step does",
      "config_json": {{
        "rag_query": "optional query for context retrieval",
        "rule_id": "optional rule reference",
        "prompt_template": "template for AI nodes"
      }}
    }}
  ]
}}

Constraints:
- MUST include at least one HUMAN_APPROVAL step
- AI_ANALYSIS steps should specify rag_query for retrieving relevant regulations
- DOMAIN_RULE steps should reference specific rule names
"""
        
        user_prompt = f"""User Intent: {intent}

Regulatory Context:
{retrieved_context}

Design a compliant workflow."""
        
        result = await self.generate_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3  # Lower for structured output
        )
        
        return result
