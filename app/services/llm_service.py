"""
LLM Service - Interface to OpenRouter
Handles AI interactions with audit logging.
"""
from typing import Dict, Any, Optional, List
import openai
from openai import AsyncOpenAI
from app.core.config import get_settings

settings = get_settings()


class LLMProviderError(RuntimeError):
    """The upstream LLM provider call failed (auth, model, quota, network).

    Derived messages are safe to show to users: they name the failure mode
    but never include the caller's key or upstream internals.
    """


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
        
        # Call OpenRouter; map provider failures to LLMProviderError so callers
        # (and their HTTP surfaces) can answer with a clear message instead of
        # an opaque 500. Only exception *types* are mapped — the user-facing
        # message never embeds the raw upstream error.
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
        except openai.AuthenticationError as exc:
            raise LLMProviderError(
                "the LLM provider rejected the API key (401 Unauthorized); "
                "check OPENROUTER_API_KEY"
            ) from exc
        except openai.NotFoundError as exc:
            raise LLMProviderError(
                f"the LLM provider does not offer model '{self.model}'; "
                "check OPENROUTER_MODEL"
            ) from exc
        except openai.RateLimitError as exc:
            raise LLMProviderError(
                "the LLM provider rate limit or quota was hit; try again shortly"
            ) from exc
        except openai.APIConnectionError as exc:
            raise LLMProviderError(
                "could not reach the LLM provider (network error or timeout)"
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMProviderError(
                f"the LLM provider returned an error (HTTP {exc.status_code})"
            ) from exc
        
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
        retrieved_context: str,
        available_rules: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate a workflow definition from natural language intent.

        Args:
            intent: User's natural language intent
            domain: Domain (fraud, kyc, etc.)
            retrieved_context: Relevant regulatory/policy documents
            available_rules: Exact rule names with an active version in the
                rule library for this domain. Referencing anything else fails
                approval validation, so the model must be told what exists.

        Returns:
            Structured workflow definition as dict
        """
        if available_rules is None:
            rule_guidance = "The rule library has not been queried — do not invent rule names; prefer AI_ANALYSIS or HUMAN_APPROVAL steps over DOMAIN_RULE."
        elif available_rules:
            names = ", ".join(f'"{name}"' for name in available_rules)
            rule_guidance = (
                "RULE LIBRARY (these are the ONLY rule names that exist for this domain):\n"
                f"[{names}]\n"
                "A DOMAIN_RULE step's config_json.rule_id MUST be one of these exact names. "
                "Never invent, abbreviate, or modify a rule name. If none of them matches "
                "the intent, do not use a DOMAIN_RULE step — express the check via an "
                "AI_ANALYSIS step or fold it into the HUMAN_APPROVAL step's description."
            )
        else:
            rule_guidance = "No rules exist in the rule library for this domain. Do not use DOMAIN_RULE steps — express the check via an AI_ANALYSIS step or fold it into the HUMAN_APPROVAL step's description."
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
{rule_guidance}
- If the intent describes an action that should apply once approved (e.g.
  suspend a card, block a transaction, notify a team), add one final step
  AFTER the HUMAN_APPROVAL step that records the applied action: an
  AI_ANALYSIS step whose prompt_template instructs the executor to emit the
  action record and echo the run input, e.g. prompt_template: "Record the
  applied action for this decision and echo the account and transaction
  identifiers from the input data." No action may apply before the human
  approval step has completed.
- EVERY AI_ANALYSIS step's prompt_template MUST demand a compact JSON decision
  contract with exactly these keys: "action" (short machine-readable label,
  e.g. "SUSPEND_CARD", "BLOCK_TX", "REVIEW", "NO_ACTION"), "risk_tier"
  ("LOW"|"MEDIUM"|"HIGH"|"CRITICAL"), "risk_score" (numeric when available),
  "summary" (max 160 chars), "applied" (true only for post-approval action
  steps). Example directive to embed in prompt_template: 'Respond ONLY with
  compact JSON: {{"action": "...", "risk_tier": "...", "risk_score": <number>,
  "summary": "...", "applied": <bool>}}'. The console renders this verdict as
  a colored banner; freeform paragraphs cannot be displayed there.
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
