"""
Workflow Compiler Service
Converts WorkflowDefinition to executable LangGraph StateGraph.
Supports pausing at HUMAN_APPROVAL nodes, resuming with human verdicts, and
condition-based branching at CONDITIONAL nodes.
"""
import asyncio
from typing import Dict, Any, List, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph import StateGraph, END
from pydantic import BaseModel

from app.core.config import get_settings
from app.models.workflow import WorkflowDefinition, WorkflowStep, StepType
from app.services.llm_service import LLMService
from app.services.rule_engine import RuleEngine

settings = get_settings()


def _tag_step_error(exc: Exception, step_name: str) -> None:
    """Attach the failing step name to an exception without masking it."""
    try:
        if not getattr(exc, "workflow_step", None):
            exc.workflow_step = step_name
    except Exception:  # pragma: no cover - exotic exception types
        pass


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
    conditional_outputs: Dict[str, Any] = {}
    # Steps that actually ran. Conditional branches skip steps; the executor
    # uses this to persist SKIPPED audit rows instead of fabricating COMPLETED
    # rows with stale state.
    executed_steps: List[str] = []
    # Per-step lineage snapshots (the last global value is not enough when a
    # workflow has more than one AI/rule node).
    ai_prompts: Dict[str, Any] = {}
    ai_models: Dict[str, Any] = {}
    retrieved_contexts: Dict[str, Any] = {}
    
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

        # Conditional targets are step names, so names must be unambiguous.
        step_names = [step.name for step in sorted_steps]
        duplicate_names = sorted({name for name in step_names if step_names.count(name) > 1})
        if duplicate_names:
            raise ValueError(
                f"Workflow step names must be unique; duplicates: {duplicate_names}"
            )

        node_names = {step.name: self._node_name(step) for step in steps_to_run}
        name_to_index = {step.name: step.order_index for step in sorted_steps}
        index_to_name = {step.order_index: step.name for step in steps_to_run}
        indices = [step.order_index for step in steps_to_run]

        # Resolve and validate every conditional step before touching the graph,
        # so bad config fails at compile time (unknown target, missing condition,
        # backwards target), not halfway through an execution.
        conditional_specs: Dict[int, Dict[str, Any]] = {}
        for step in steps_to_run:
            if step.step_type == StepType.CONDITIONAL:
                conditional_specs[step.order_index] = self._resolve_conditional_spec(
                    step, name_to_index, node_names
                )

        # Linear successors, then override the tail of bounded branch regions.
        # A conditional with two targets partitions [earlier_target, later_target)
        # as the earlier branch, which rejoins after the later target. A
        # conditional with one target treats the steps between itself and the
        # target as the fall-through branch, which rejoins after the target.
        successors: Dict[int, Optional[int]] = {}
        for position, step in enumerate(steps_to_run):
            successors[step.order_index] = (
                steps_to_run[position + 1].order_index
                if position + 1 < len(steps_to_run)
                else None
            )

        regions = []
        immediate_fallthroughs = []
        for step in steps_to_run:
            spec = conditional_specs.get(step.order_index)
            if spec is None:
                continue
            target_indices = sorted({
                name_to_index[name]
                for name in (spec["on_true"], spec["on_false"])
                if name is not None
            })
            if len(target_indices) == 2:
                regions.append((target_indices[0], target_indices[1]))
            elif len(target_indices) == 1:
                target_index = target_indices[0]
                if step.order_index + 1 < target_index:
                    regions.append((step.order_index + 1, target_index))
                else:
                    # Target is the immediate next step: the fall-through branch
                    # must skip it and rejoin after it.
                    immediate_fallthroughs.append((step.order_index, target_index))

        # Wider (outer) regions first; narrower nested regions may override them.
        # The region exit is the *successor of the region's end step* — not a
        # naive index+1 — so a nested branch that ends inside an outer branch
        # follows the outer branch's rejoin point instead of leaking into the
        # sibling branch.
        regions.sort(key=lambda region: region[1] - region[0], reverse=True)
        for start, end in regions:
            region_indices = [i for i in indices if start <= i < end]
            if region_indices:
                successors[region_indices[-1]] = successors.get(end)
        for conditional_index, target_index in immediate_fallthroughs:
            successors[conditional_index] = successors.get(target_index)

        # Add nodes for each step to run
        for step in steps_to_run:
            node_name = node_names[step.name]

            if step.step_type == StepType.AI_ANALYSIS:
                graph.add_node(node_name, self._guard_node(step, self._create_ai_node(step, workflow)))
            elif step.step_type == StepType.DOMAIN_RULE:
                rule_id = (step.config_json or {}).get("rule_id")
                if rule_id and rule_id not in rule_logics:
                    raise ValueError(
                        f"Rule '{rule_id}' referenced by step '{step.name}' "
                        f"was not resolved from the rule library"
                    )
                graph.add_node(
                    node_name,
                    self._guard_node(step, self._create_rule_node(step, rule_logics.get(rule_id))),
                )
            elif step.step_type == StepType.HUMAN_APPROVAL:
                verdict = human_verdicts.get(step.name)
                graph.add_node(
                    node_name,
                    self._guard_node(step, self._create_human_node(step, verdict=verdict)),
                )
            elif step.step_type == StepType.CONDITIONAL:
                spec = conditional_specs[step.order_index]
                graph.add_node(
                    node_name,
                    self._guard_node(
                        step,
                        self._create_conditional_node(
                            step, on_true=spec["on_true"], on_false=spec["on_false"]
                        ),
                    ),
                )

        # Define edges. Ordinary steps flow linearly; conditional steps replace
        # their unconditional edge with a runtime branch.
        first_node = node_names[steps_to_run[0].name]
        graph.set_entry_point(first_node)

        for step in steps_to_run:
            node_name = node_names[step.name]
            if step.step_type == StepType.CONDITIONAL:
                fallthrough = successors[step.order_index]
                graph.add_conditional_edges(
                    node_name,
                    self._create_conditional_router(
                        step,
                        fallthrough_node=(
                            node_names[index_to_name[fallthrough]]
                            if fallthrough is not None
                            else END
                        ),
                        node_names=node_names,
                    ),
                )
            else:
                successor = successors[step.order_index]
                graph.add_edge(
                    node_name,
                    node_names[index_to_name[successor]] if successor is not None else END,
                )

        return graph.compile()

    @staticmethod
    def _node_name(step: WorkflowStep) -> str:
        return f"step_{step.order_index}_{step.name.replace(' ', '_')}"

    def _resolve_conditional_spec(
        self,
        step: WorkflowStep,
        name_to_index: Dict[str, int],
        node_names: Dict[str, str],
    ) -> Dict[str, Any]:
        """Validate a CONDITIONAL step's config and resolve its branch targets."""
        config = step.config_json or {}
        condition = config.get("condition")
        if not isinstance(condition, dict) or not condition:
            raise ValueError(
                f"Conditional step '{step.name}' requires a non-empty "
                f"config_json.condition object"
            )

        resolved: Dict[str, Any] = {"condition": condition, "on_true": None, "on_false": None}
        for key in ("on_true", "on_false"):
            target = config.get(key)
            if target is None:
                continue
            if target not in name_to_index:
                raise ValueError(
                    f"Conditional step '{step.name}' references unknown target "
                    f"'{target}' in config_json.{key}"
                )
            if target not in node_names:
                raise ValueError(
                    f"Conditional step '{step.name}' routes to '{target}', which is "
                    f"not part of the compiled step range"
                )
            if name_to_index[target] <= step.order_index:
                raise ValueError(
                    f"Conditional step '{step.name}' can only branch forward; "
                    f"target '{target}' is not after it"
                )
            resolved[key] = target
        return resolved

    def _guard_node(self, step: WorkflowStep, node_fn):
        """Tag exceptions with the failing step so the executor can audit it."""
        if asyncio.iscoroutinefunction(node_fn):
            async def async_wrapper(state):
                try:
                    return await node_fn(state)
                except WorkflowPaused:
                    raise
                except Exception as exc:  # noqa: BLE001 - re-raised unchanged
                    _tag_step_error(exc, step.name)
                    raise
            return async_wrapper

        def sync_wrapper(state):
            try:
                return node_fn(state)
            except WorkflowPaused:
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised unchanged
                _tag_step_error(exc, step.name)
                raise
        return sync_wrapper
    
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
            state.ai_prompts[step.name] = result.get("prompt_snapshot")
            state.ai_models[step.name] = result.get("model_name")
            state.retrieved_contexts[step.name] = context_text
            state.current_output = {"content": result['content']}
            state.context["last_ai_output"] = result['content']
            state.context["retrieved_context"] = context_text
            state.context["last_prompt"] = result['prompt_snapshot']
            if step.name not in state.executed_steps:
                state.executed_steps.append(step.name)
            
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
                state.context["last_rule_step"] = step.name
                if step.name not in state.executed_steps:
                    state.executed_steps.append(step.name)
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
            state.context["last_rule_step"] = step.name
            if step.name not in state.executed_steps:
                state.executed_steps.append(step.name)

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
                if step.name not in st.executed_steps:
                    st.executed_steps.append(step.name)
                return st

            raise WorkflowPaused(step=step, state=st)
        
        return human_node
    
    def _create_conditional_node(
        self,
        step,
        on_true: Optional[str] = None,
        on_false: Optional[str] = None,
    ):
        """Create a conditional branching node.

        The condition uses the same JSON DSL as rules and is evaluated against
        the run input plus the outputs produced by earlier steps. The chosen
        target is a step *name* (``None`` means fall through linearly).
        """
        def conditional_node(state: WorkflowState) -> WorkflowState:
            config = step.config_json or {}
            condition = config.get("condition")

            eval_data = {
                **state.input_data,
                **state.ai_outputs,
                **state.rule_outputs,
                **state.human_approvals,
                "current_output": state.current_output,
            }
            condition_met = bool(
                self.rule_engine._evaluate_condition(condition, eval_data)
            )

            target = on_true if condition_met else on_false
            state.conditional_outputs[step.name] = {
                "condition_met": condition_met,
                "next": target,
            }
            state.context["condition_result"] = condition_met
            state.current_output = {"condition_met": condition_met}
            if step.name not in state.executed_steps:
                state.executed_steps.append(step.name)

            return state
        
        return conditional_node

    def _create_conditional_router(
        self,
        step,
        fallthrough_node: str,
        node_names: Dict[str, str],
    ):
        """Route a conditional node to the target chosen by its node function."""
        def route(state) -> str:
            info = (state.conditional_outputs or {}).get(step.name) or {}
            target_name = info.get("next")
            if target_name is None:
                return fallthrough_node
            return node_names[target_name]

        return route
