"""
Workflow Compiler Service
Converts WorkflowDefinition to executable LangGraph StateGraph.
Supports pausing at HUMAN_APPROVAL nodes, resuming with human verdicts, and
condition-based branching at CONDITIONAL nodes.
"""
import asyncio
from typing import Dict, Any, List, Optional, Tuple
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
        human_verdicts: Optional[Dict[str, Dict[str, Any]]] = None,
        recorded_outputs: Optional[Dict[str, Dict[str, Any]]] = None,
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
            recorded_outputs: Optional {step_name: {status, output, input, ...}}
                map of the ORIGINAL run's recorded node outputs (built by the
                executor). When given, the graph runs in recorded-replay mode:
                AI nodes return their recorded output verbatim (no LLM call),
                DOMAIN_RULE nodes reuse the recorded result (no re-evaluation,
                the pinned rule version is still recorded on the node row), and
                CONDITIONAL nodes route exactly along the recorded gate
                outcome. Human approval nodes are unaffected (verdicts were
                already reused via ``human_verdicts``). Costs determinism:
                the replay leg is bit-identical to the original execution.

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
                    step, name_to_index, node_names, sorted_steps
                )

        # Linear successors first, then apply the conditional wiring overrides.
        #
        # CONDITIONAL semantics:
        #
        #  Entry-target form (on_true/on_false are single step names):
        #    Two targets: the earlier branch owns the contiguous region
        #    [earlier_target, later_target); the later branch is the *single*
        #    step at later_target; every step after it is shared by both
        #    paths (this is what makes nested gate regions compose). When the
        #    earlier region spans more than one step AND more steps follow the
        #    later target, the layout is ambiguous — those trailing steps could
        #    be later-branch body steps or shared steps — so compile with list
        #    bodies instead (see below) rather than guessing a wiring that can
        #    silently route one branch into the other's steps.
        #    One target: the fall-through branch is [conditional, target) and
        #    rejoins after the target.
        #
        #  Explicit-body form (on_true/on_false are lists of step names):
        #    Both branch bodies are stated in full; the shared join is the
        #    first step after the conditional that belongs to neither body.
        #    Wiring is explicit and unambiguous by construction.
        successors: Dict[int, Optional[int]] = {}
        for position, step in enumerate(steps_to_run):
            successors[step.order_index] = (
                steps_to_run[position + 1].order_index
                if position + 1 < len(steps_to_run)
                else None
            )
        index_set = set(indices)

        regions = []
        immediate_fallthroughs: List[Tuple[int, int]] = []
        explicit_bodies: List[Tuple[int, List[int], List[int]]] = []
        for step in steps_to_run:
            spec = conditional_specs.get(step.order_index)
            if spec is None:
                continue
            true_body = [name_to_index[name] for name in spec["true_body"]]
            false_body = [name_to_index[name] for name in spec["false_body"]]
            target_indices = sorted(
                {index for body in (true_body, false_body) for index in body}
            )
            if true_body != [target_indices[0]] or false_body != (
                [target_indices[-1]] if len(target_indices) == 2 else []
            ):
                # At least one side lists a full branch body: wire it explicitly.
                explicit_bodies.append((step.order_index, true_body, false_body))
            elif len(target_indices) == 2:
                earlier_target, later_target = target_indices
                steps_after_later = [
                    i for i in indices if i > later_target
                ]
                earlier_region_width = later_target - earlier_target
                if earlier_region_width > 1 and len(steps_after_later) > 1:
                    raise ValueError(
                        f"Conditional step '{step.name}' has multi-step branch "
                        f"bodies but only entry-point targets; steps after "
                        f"'{spec['on_false']}' are ambiguous (branch body or "
                        f"shared?). Use explicit list targets instead: "
                        f"config_json.on_true/[on_false] as [\"<step name>\", ...]."
                    )
                # The earlier branch's tail exits at the successor of the later
                # branch's single step — the shared join. Never swallow the last
                # step of the compile range; it stays the common rejoin when it
                # is the only step after the later target.
                exit_anchor = later_target
                regions.append((earlier_target, later_target, exit_anchor))
            elif len(target_indices) == 1:
                target_index = target_indices[0]
                if step.order_index + 1 < target_index:
                    regions.append(
                        (step.order_index + 1, target_index, target_index)
                    )
                else:
                    # Target is the immediate next step: the fall-through branch
                    # must skip it and rejoin after it.
                    immediate_fallthroughs.append((step.order_index, target_index))

        # Wider (outer) regions first; narrower nested regions may override them.
        # A region's tail exits at the successor of its own end anchor — not a
        # naive index+1 — so the earlier branch jumps past the later branch,
        # and a nested branch that ends inside an outer branch follows the
        # outer branch's rejoin point instead of leaking into the sibling
        # branch.
        regions.sort(key=lambda region: region[1] - region[0], reverse=True)
        for start, end, exit_anchor in regions:
            region_indices = [i for i in indices if start <= i < end]
            if region_indices:
                successors[region_indices[-1]] = successors.get(exit_anchor)
        for conditional_index, target_index in immediate_fallthroughs:
            successors[conditional_index] = successors.get(target_index)

        # Explicit branch bodies wire after the regional overrides so an
        # explicitly stated body always wins over the inference above.
        for conditional_index, true_body, false_body in explicit_bodies:
            unlisted = [
                i for i in indices
                if i > conditional_index
                and i not in true_body
                and i not in false_body
            ]
            join = min(unlisted) if unlisted else None
            if join is not None:
                past_join = [
                    i for i in (*true_body, *false_body) if i > join
                ]
                if past_join:
                    raise ValueError(
                        f"Conditional step '{index_to_name[conditional_index]}' "
                        f"has explicit branch bodies that reach past the first "
                        f"shared step '{index_to_name[join]}'; move shared "
                        f"steps after both branches."
                    )
            for body in (true_body, false_body):
                for position, index in enumerate(body):
                    next_index = (
                        body[position + 1] if position + 1 < len(body) else join
                    )
                    if successors[index] != next_index:
                        successors[index] = next_index

        # Add nodes for each step to run
        for step in steps_to_run:
            node_name = node_names[step.name]

            if step.step_type == StepType.AI_ANALYSIS:
                graph.add_node(
                    node_name,
                    self._guard_node(
                        step, self._create_ai_node(step, workflow, recorded_outputs)
                    ),
                )
            elif step.step_type == StepType.DOMAIN_RULE:
                rule_id = (step.config_json or {}).get("rule_id")
                if rule_id and rule_id not in rule_logics:
                    raise ValueError(
                        f"Rule '{rule_id}' referenced by step '{step.name}' "
                        f"was not resolved from the rule library"
                    )
                graph.add_node(
                    node_name,
                    self._guard_node(
                        step,
                        self._create_rule_node(
                            step, rule_logics.get(rule_id), recorded_outputs
                        ),
                    ),
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
                            step,
                            on_true=spec["on_true"],
                            on_false=spec["on_false"],
                            recorded_outputs=recorded_outputs,
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
        sorted_steps: List[WorkflowStep],
    ) -> Dict[str, Any]:
        """Validate a CONDITIONAL step's config and resolve its branch targets.

        ``on_true``/``on_false`` each accept a step *name* (entry-point form,
        whose wiring is inferred) or a non-empty list of step names (explicit
        branch body, wired exactly as listed). The spec carries the route
        entries (first body element) in ``on_true``/``on_false`` and the full
        bodies in ``true_body``/``false_body``.
        """
        config = step.config_json or {}
        condition = config.get("condition")
        if not isinstance(condition, dict) or not condition:
            raise ValueError(
                f"Conditional step '{step.name}' requires a non-empty "
                f"config_json.condition object"
            )

        resolved: Dict[str, Any] = {
            "condition": condition,
            "on_true": None,
            "on_false": None,
            "true_body": [],
            "false_body": [],
        }
        step_types = {s.name: s.step_type for s in sorted_steps}
        for key in ("on_true", "on_false"):
            body_key = "true_body" if key == "on_true" else "false_body"
            target = config.get(key)
            if target is None:
                continue
            if isinstance(target, (list, tuple)):
                body = list(target)
            else:
                # A single entry-point target is a one-element body.
                body = [target]
            if not body or not all(isinstance(name, str) for name in body):
                raise ValueError(
                    f"Conditional step '{step.name}' config_json.{key} must be "
                    f"a step name or a non-empty list of step names"
                )
            if len(set(body)) != len(body):
                raise ValueError(
                    f"Conditional step '{step.name}' config_json.{key} "
                    f"repeats a step name"
                )
            indices_in_order = []
            for name in body:
                if name not in name_to_index:
                    raise ValueError(
                        f"Conditional step '{step.name}' references unknown "
                        f"target '{name}' in config_json.{key}"
                    )
                if name not in node_names:
                    raise ValueError(
                        f"Conditional step '{step.name}' routes to a branch "
                        f"step '{name}' that is not part of the compiled "
                        f"step range"
                    )
                if name_to_index[name] <= step.order_index:
                    raise ValueError(
                        f"Conditional step '{step.name}' can only branch "
                        f"forward; branch target '{name}' is not after it"
                    )
                if len(body) > 1 and step_types[name] == StepType.CONDITIONAL:
                    raise ValueError(
                        f"Conditional step '{step.name}' lists nested "
                        f"conditional '{name}' inside an explicit multi-step "
                        f"branch body, which is not supported; branches start "
                        f"at their entry step, place the nested gate before "
                        f"the branching step instead"
                    )
                indices_in_order.append(name_to_index[name])
            if indices_in_order != sorted(indices_in_order):
                raise ValueError(
                    f"Conditional step '{step.name}' config_json.{key} branch "
                    f"body must be listed in forward execution order"
                )
            resolved[key] = body[0]
            resolved[body_key] = body
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
    
    def _create_ai_node(
        self,
        step,
        workflow: Optional[WorkflowDefinition] = None,
        recorded_outputs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """Create an AI analysis node.

        With ``recorded_outputs`` the node is a recorded-replay stub: it
        replays the original leg's recorded output verbatim and never calls
        the LLM. A visited step without a recorded entry fails loudly —
        recorded mode is deterministic-only, it must not silently fall back
        to a fresh (nondeterministic) LLM call.
        """
        async def ai_node(state: WorkflowState) -> WorkflowState:
            """Execute AI analysis with (best-effort) RAG context."""
            if recorded_outputs is not None:
                recorded = recorded_outputs.get(step.name)
                if recorded is None:
                    raise ValueError(
                        f"Recorded replay: AI step '{step.name}' has no recorded "
                        f"node output from the original run; recorded mode "
                        f"never re-calls the LLM"
                    )
                output = recorded.get("output")
                prompt_snapshot = (
                    output.get("prompt_snapshot")
                    if isinstance(output, dict)
                    else None
                )
                content = output.get("content") if isinstance(output, dict) else output
                context_text = recorded.get("retrieved_context") or ""

                state.ai_outputs[step.name] = output
                state.ai_prompts[step.name] = prompt_snapshot
                # Mark the replay leg: the model did not run again here.
                state.ai_models[step.name] = "recorded-replay"
                state.retrieved_contexts[step.name] = context_text
                state.current_output = {"content": content}
                state.context["last_ai_output"] = content
                state.context["retrieved_context"] = context_text
                state.context["last_prompt"] = prompt_snapshot
                if step.name not in state.executed_steps:
                    state.executed_steps.append(step.name)
                return state

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
                    from app.services.rag_service import get_rag_service
                    rag_service = get_rag_service()
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
                system_prompt=(
                    "You are an AI assistant for fraud detection and compliance "
                    "analysis. Provide structured, professional analysis. "
                    "Respond with a COMPACT JSON object (no markdown fences, no "
                    "prose around it) using exactly these keys:\n"
                    '- "action": short machine-readable label of the decision '
                    '(e.g. "SUSPEND_CARD", "BLOCK_TX", "REVIEW", "NO_ACTION")\n'
                    '- "risk_tier": "LOW", "MEDIUM", "HIGH" or "CRITICAL"\n'
                    '- "risk_score": the numeric score this step evaluated (when input provides one)\n'
                    '- "summary": the decision reason in at most 160 characters\n'
                    '- "applied": true only when this step APPLIES an action after '
                    'human approval, otherwise false'
                ),
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
    
    def _create_rule_node(
        self,
        step,
        rule_logic: Optional[Dict[str, Any]],
        recorded_outputs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """Create a rule evaluation node (logic resolved from the rule library).

        With ``recorded_outputs`` the node reuses the original leg's recorded
        result instead of re-evaluating (deterministic replay). The executor
        still records the pinned rule_version_id on the node row.
        """
        def rule_node(state: WorkflowState) -> WorkflowState:
            """Execute deterministic rule evaluation."""
            if recorded_outputs is not None:
                recorded = recorded_outputs.get(step.name)
                if recorded is None:
                    raise ValueError(
                        f"Recorded replay: rule step '{step.name}' has no "
                        f"recorded node output from the original run; "
                        f"recorded mode never re-evaluates rules"
                    )
                result = recorded.get("output")
                rule_id = (step.config_json or {}).get("rule_id")
                state.rule_outputs[step.name] = result
                state.current_output = result
                state.context["last_rule_result"] = result
                state.context["last_rule_id"] = rule_id
                state.context["last_rule_step"] = step.name
                if step.name not in state.executed_steps:
                    state.executed_steps.append(step.name)
                return state

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
        recorded_outputs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """Create a conditional branching node.

        The condition uses the same JSON DSL as rules and is evaluated against
        the run input plus the outputs produced by earlier steps. The chosen
        target is a step *name* (``None`` means fall through linearly).

        With ``recorded_outputs`` the gate replays the recorded routing
        outcome (``{"condition_met": ..., "next": ...}`` from the original
        leg's gate output) instead of re-evaluating the condition, so a
        re-executed AI output that drifted cannot flip the branch.
        """
        def conditional_node(state: WorkflowState) -> WorkflowState:
            config = step.config_json or {}
            condition = config.get("condition")

            if recorded_outputs is not None:
                recorded = recorded_outputs.get(step.name)
                if recorded is None:
                    raise ValueError(
                        f"Recorded replay: conditional step '{step.name}' has "
                        f"no recorded gate output from the original run; "
                        f"recorded mode cannot re-derive routing"
                    )
                gate = recorded.get("output")
                if not isinstance(gate, dict) or "next" not in gate:
                    raise ValueError(
                        f"Recorded replay: conditional step '{step.name}' has "
                        f"no recorded routing outcome (its gate output was "
                        f"never completed in the original run)"
                    )
                state.conditional_outputs[step.name] = dict(gate)
                state.context["condition_result"] = gate.get("condition_met")
                state.current_output = {"condition_met": gate.get("condition_met")}
                if step.name not in state.executed_steps:
                    state.executed_steps.append(step.name)
                return state

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
