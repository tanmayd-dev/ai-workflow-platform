"""
Workflow Compiler Service
Converts WorkflowDefinition to executable LangGraph StateGraph.
Supports pausing at HUMAN_APPROVAL nodes and resuming with human verdicts.
"""
from typing import Dict, Any, List, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph import StateGraph, END
from pydantic import BaseModel

from app.core.config import get_settings
from app.models.workflow import WorkflowDefinition, WorkflowStep, StepType
from app.services.llm_service import LLMService
from app.services.rule_engine import RuleEngine

settings = get_settings()


class WorkflowPaused(Exception):
    """Raised when workflow reaches a HUMAN_APPROVAL step that requires user input."""

    def __init__(self, step: WorkflowStep, state: Any):
        self.step = step
        if isinstance(state, WorkflowState):
            self.state = state
        elif isinstance(state, dict):
            self.state = WorkflowState(**state)
        else:
            self.state = WorkflowState()
        super().__init__(f"Workflow paused at step '{step.name}' awaiting human approval")


class WorkflowState(BaseModel):
    """State object passed between workflow nodes."""
    messages: List[BaseMessage] = []
    input_data: Dict[str, Any] = {}
    # Holds whatever the last node produced (AI content string, rule result
    # scalar, approval dict) — deliberately Any, it serializes into run.output_data.
    current_output: Any = {}
    context: Dict[str, Any] = {}
    ai_outputs: Dict[str, Any] = {}
    rule_outputs: Dict[str, Any] = {}
    human_approvals: Dict[str, Any] = {}
    
    class Config:
        arbitrary_types_allowed = True


class WorkflowCompiler:
    """Compiles workflow definitions into executable LangGraph graphs."""
    
    def __init__(self):
        self.llm_service = LLMService()
        self.rule_engine = RuleEngine()
    
    def compile(
        self,
        workflow: WorkflowDefinition,
        rule_logics: Optional[Dict[str, Dict[str, Any]]] = None,
        start_step_index: int = 0,
        human_verdicts: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> StateGraph:
        """
        Convert a WorkflowDefinition to an executable LangGraph.

        Args:
            workflow: The workflow definition to compile
            rule_logics: Resolved rule logic keyed by rule_id (loaded from DB
                by the executor). Steps referencing a rule_id absent from this
                map raise ValueError at compile time.
            start_step_index: Starting step order_index for compiling a graph
                (used when resuming from a paused step).
            human_verdicts: Pre-supplied verdicts keyed by step name for
                human approval steps.

        Returns:
            StateGraph ready for execution
        """
        rule_logics = rule_logics or {}
        human_verdicts = human_verdicts or {}
        graph = StateGraph(WorkflowState)

        # Sort steps by order_index
        sorted_steps = sorted(workflow.steps, key=lambda s: s.order_index)
        steps_to_run = [s for s in sorted_steps if s.order_index >= start_step_index]

        if not steps_to_run:
            raise ValueError(f"No steps to compile starting from index {start_step_index}")

        # Add nodes for each step to run
        for step in steps_to_run:
            node_name = f"step_{step.order_index}_{step.name.replace(' ', '_')}"

            if step.step_type == StepType.AI_ANALYSIS:
                graph.add_node(node_name, self._create_ai_node(step, workflow))
            elif step.step_type == StepType.DOMAIN_RULE:
                rule_id = (step.config_json or {}).get("rule_id")
                if rule_id and rule_id not in rule_logics:
                    raise ValueError(
                        f"Rule '{rule_id}' referenced by step '{step.name}' "
                        f"was not resolved from the rule library"
                    )
                graph.add_node(node_name, self._create_rule_node(step, rule_logics.get(rule_id)))
            elif step.step_type == StepType.HUMAN_APPROVAL:
                verdict = human_verdicts.get(step.name)
                graph.add_node(node_name, self._create_human_node(step, verdict=verdict))
            elif step.step_type == StepType.CONDITIONAL:
                graph.add_node(node_name, self._create_conditional_node(step))
        
        # Define edges (linear flow for now)
        first_node = f"step_{steps_to_run[0].order_index}_{steps_to_run[0].name.replace(' ', '_')}"
        graph.set_entry_point(first_node)
        
        for i in range(len(steps_to_run) - 1):
            current_node = f"step_{steps_to_run[i].order_index}_{steps_to_run[i].name.replace(' ', '_')}"
            next_node = f"step_{steps_to_run[i+1].order_index}_{steps_to_run[i+1].name.replace(' ', '_')}"
            graph.add_edge(current_node, next_node)
        
        # Last node goes to END
        last_node = f"step_{steps_to_run[-1].order_index}_{steps_to_run[-1].name.replace(' ', '_')}"
        graph.add_edge(last_node, END)
        
        return graph.compile()
    
    def _create_ai_node(self, step, workflow: Optional[WorkflowDefinition] = None):
        """Create an AI analysis node."""
        async def ai_node(state: WorkflowState) -> WorkflowState:
            """Execute AI analysis with (best-effort) RAG context."""
            config = step.config_json
            rag_query = config.get("rag_query", "")
            prompt_template = config.get("prompt_template", "")

            # Retrieve context from the knowledge base when available.
            # Failures fall back to an empty context so AI nodes still run
            # (e.g. no embeddings model locally, empty collection).
            context_text = ""
            domain = workflow.domain if workflow else getattr(step, "domain", "")
            if rag_query and settings.ENABLE_RAG and domain:
                try:
                    from app.services.rag_service import RAGService
                    rag_service = RAGService()
                    retrieved_docs = await rag_service.query_similar(
                        query_text=rag_query,
                        domain=domain,
                        top_k=3
                    )
                    context_text = "\n\n".join(doc.get("text", "") for doc in retrieved_docs)
                except Exception as e:
                    state.context.setdefault("rag_warnings", []).append(
                        f"RAG retrieval failed for step '{step.name}': {e}"
                    )

            # Build user prompt from template and input data
            user_prompt = f"{prompt_template}\n\nInput Data:\n{state.input_data}"
            
            # Call LLM with proper async and parameters
            result = await self.llm_service.generate_completion(
                system_prompt="You are an AI assistant for fraud detection and compliance analysis. Provide structured, professional analysis.",
                user_prompt=user_prompt,
                context=context_text if context_text else None
            )
            
            # Store output
            state.ai_outputs[step.name] = result
            state.current_output = {"content": result['content']}
            state.context["last_ai_output"] = result['content']
            state.context["retrieved_context"] = context_text
            state.context["last_prompt"] = result['prompt_snapshot']
            
            return state
        
        return ai_node
    
    def _create_rule_node(self, step, rule_logic: Optional[Dict[str, Any]]):
        """Create a rule evaluation node (logic resolved from the rule library)."""
        def rule_node(state: WorkflowState) -> WorkflowState:
            """Execute deterministic rule evaluation."""
            # No rule reference declared: pass-through, record absence explicitly
            if rule_logic is None:
                result = None
                state.rule_outputs[step.name] = result
                state.current_output = {"rule": "not-referenced"}
                state.context["last_rule_result"] = result
                return state

            rule_id = (step.config_json or {}).get("rule_id")

            # Prepare data for evaluation
            eval_data = {
                **state.input_data,
                **state.ai_outputs,
                "current_output": state.current_output
            }

            result = self.rule_engine.evaluate(rule_logic, eval_data)

            # Store output
            state.rule_outputs[step.name] = result
            state.current_output = result
            state.context["last_rule_result"] = result
            state.context["last_rule_id"] = rule_id

            return state

        return rule_node
    
    def _create_human_node(self, step, verdict: Optional[Dict[str, Any]] = None):
        """Create a human approval node."""
        def human_node(state: WorkflowState) -> WorkflowState:
            """Await human approval or apply human verdict."""
            st = state if isinstance(state, WorkflowState) else WorkflowState(**state)
            v = verdict or st.human_approvals.get(step.name)
            if v is not None:
                st.human_approvals[step.name] = v
                st.current_output = v
                st.context["last_approval"] = v
                if isinstance(v, dict) and "approved" in v:
                    st.context["approved"] = v["approved"]
                return st

            raise WorkflowPaused(step=step, state=st)
        
        return human_node
    
    def _create_conditional_node(self, step):
        """Create a conditional branching node."""
        def conditional_node(state: WorkflowState) -> WorkflowState:
            """Evaluate condition and set next step."""
            config = step.config_json
            condition_field = config.get("condition_field", "approved")
            
            # Evaluate condition from context
            condition_met = state.context.get(condition_field, False)
            
            state.context["condition_result"] = condition_met
            state.current_output = {"condition_met": condition_met}
            
            return state
        
        return conditional_node
