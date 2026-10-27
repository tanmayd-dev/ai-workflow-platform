"""Unit tests for the deterministic JSON-DSL rule engine (issue #15).

No database, no async, no network: ``RuleEngine`` is pure in-memory logic.
The condition DSL is shared by ``DOMAIN_RULE`` steps and ``CONDITIONAL``
branching, so these tests pin its exact semantics (including failure modes).
"""
import pytest

from app.services.rule_engine import RuleEngine

engine = RuleEngine()


def _logic(op, value, *, field="risk_score", result="MATCH"):
    return {"condition": {"field": field, "op": op, "value": value}, "result": result}


@pytest.mark.parametrize(
    ("op", "actual", "threshold", "expected"),
    [
        (">", 0.81, 0.8, True),
        (">", 0.8, 0.8, False),
        (">=", 0.8, 0.8, True),
        (">=", 0.79, 0.8, False),
        ("<", 0.79, 0.8, True),
        ("<", 0.8, 0.8, False),
        ("<=", 0.8, 0.8, True),
        ("<=", 0.81, 0.8, False),
        ("==", "HIGH", "HIGH", True),
        ("==", "HIGH", "LOW", False),
        ("!=", "HIGH", "LOW", True),
        ("!=", "HIGH", "HIGH", False),
    ],
)
def test_comparison_operators(op, actual, threshold, expected):
    """Every scalar operator returns the result only when the comparison holds."""
    logic = _logic(op, threshold)
    assert engine.evaluate(logic, {"risk_score": actual}) == ("MATCH" if expected else None)


@pytest.mark.parametrize(
    ("op", "actual", "operand", "expected"),
    [
        ("in", "high", ["high", "critical"], True),
        ("in", "low", ["high", "critical"], False),
        ("not_in", "low", ["high", "critical"], True),
        ("not_in", "high", ["high", "critical"], False),
        # Membership over strings uses normal Python substring semantics.
        ("in", "fraud", "fraud-detection", True),
        ("not_in", "kyc", "fraud-detection", True),
    ],
)
def test_membership_operators(op, actual, operand, expected):
    logic = _logic(op, operand, field="category")
    assert engine.evaluate(logic, {"category": actual}) == ("MATCH" if expected else None)


def test_nested_all_any_not():
    """Logical combinators compose recursively, including nested groups."""
    logic = {
        "condition": {
            "all": [
                {"field": "risk_score", "op": ">", "value": 0.8},
                {
                    "any": [
                        {"field": "country", "op": "in", "value": ["IR", "KP"]},
                        {"field": "amount", "op": ">=", "value": 10000},
                    ]
                },
                {"not": {"field": "verified", "op": "==", "value": True}},
            ]
        },
        "result": "ESCALATE",
    }

    matches = {"risk_score": 0.9, "country": "IR", "amount": 50, "verified": False}
    assert engine.evaluate(logic, matches) == "ESCALATE"

    # Any group false -> all false.
    no_any = {"risk_score": 0.9, "country": "DE", "amount": 5000, "verified": False}
    assert engine.evaluate(logic, no_any) is None

    # not group false (verified is true) -> all false.
    verified = {"risk_score": 0.9, "country": "IR", "amount": 50, "verified": True}
    assert engine.evaluate(logic, verified) is None

    # First all clause false -> no evaluation of the rest.
    low_risk = {"risk_score": 0.1, "country": "IR", "amount": 50, "verified": False}
    assert engine.evaluate(logic, low_risk) is None


def test_missing_field_is_false():
    """A field absent from the context never satisfies the condition."""
    assert engine.evaluate(_logic(">", 0.5), {"amount": 100}) is None
    assert engine.evaluate(_logic("!=", "anything", field="country"), {}) is None


def test_missing_field_inside_any_still_allows_other_branch():
    logic = {
        "condition": {
            "any": [
                {"field": "missing_field", "op": ">", "value": 1},
                {"field": "risk_score", "op": ">", "value": 0.5},
            ]
        },
        "result": "MATCH",
    }
    assert engine.evaluate(logic, {"risk_score": 0.9}) == "MATCH"


@pytest.mark.parametrize("result", ["ALWAYS", {"decision": "ALLOW"}, 0, False, ""])
def test_no_condition_returns_result_verbatim(result):
    """No condition (or an empty one) means always-match, preserving falsy results."""
    assert engine.evaluate({"result": result}, {}) is result
    assert engine.evaluate({"condition": None, "result": result}, {}) is result
    assert engine.evaluate({"condition": {}, "result": result}, {}) is result


def test_result_passthrough_is_not_transformed():
    result = {"action": "ESCALATE", "reason": "high risk"}
    assert engine.evaluate({"result": result}, {"risk_score": 0.99}) is result


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "risk_score"},            # missing operator
        {"op": ">", "value": 0.8},          # missing field
        {"value": 0.8},                     # missing field and operator
    ],
)
def test_invalid_condition_raises_value_error(condition):
    with pytest.raises(ValueError, match="Invalid condition"):
        engine.evaluate({"condition": condition, "result": "MATCH"}, {"risk_score": 0.9})


def test_unknown_operator_raises_value_error():
    with pytest.raises(ValueError, match="Unknown operator"):
        engine.evaluate(_logic("~=", 0.8), {"risk_score": 0.9})


def test_incomparable_types_raise_value_error():
    """Comparison failures are surfaced as ValueError, not swallowed."""
    with pytest.raises(ValueError, match="Failed to evaluate condition"):
        engine.evaluate(
            {"condition": {"field": "name", "op": ">", "value": 5}, "result": "MATCH"},
            {"name": "alice"},
        )


def test_falsy_context_values_are_present_not_missing():
    """Only ``None`` means missing: 0/False are real values and compare normally."""
    assert engine.evaluate(_logic(">=", 0), {"risk_score": 0}) == "MATCH"
    assert engine.evaluate(_logic("==", False, field="flagged"), {"flagged": False}) == "MATCH"


def test_evaluate_rules_returns_first_matching_result():
    rules = [
        _logic(">", 0.95, result="BLOCK"),
        _logic(">", 0.8, result="ESCALATE"),
        _logic(">", 0.5, result="REVIEW"),
    ]
    assert engine.evaluate_rules(rules, {"risk_score": 0.85}) == "ESCALATE"
    assert engine.evaluate_rules(rules, {"risk_score": 0.99}) == "BLOCK"
    assert engine.evaluate_rules(rules, {"risk_score": 0.1}) is None


def test_evaluate_rules_treats_falsy_non_none_result_as_match():
    """A matching rule whose result is ``False``/``0`` must not fall through."""
    rules = [
        {"condition": {"field": "score", "op": ">", "value": 0}, "result": False},
        {"condition": {"field": "score", "op": ">", "value": 0}, "result": "SECOND"},
    ]
    assert engine.evaluate_rules(rules, {"score": 5}) is False
