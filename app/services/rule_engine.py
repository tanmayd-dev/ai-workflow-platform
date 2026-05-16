"""
Rule Engine - JSON DSL Evaluator
Deterministic rule evaluation for branching logic.
"""
from typing import Dict, Any, Union
import operator


class RuleEngine:
    """Evaluates rules defined in JSON DSL."""
    
    # Operator mapping
    OPERATORS = {
        ">": operator.gt,
        ">=": operator.ge,
        "<": operator.lt,
        "<=": operator.le,
        "==": operator.eq,
        "!=": operator.ne,
        "in": lambda a, b: a in b,
        "not_in": lambda a, b: a not in b
    }
    
    def evaluate(self, logic_json: Dict[str, Any], context: Dict[str, Any]) -> Any:
        """
        Evaluate a rule against a context.
        
        Args:
            logic_json: Rule logic in JSON DSL format
                Example: {
                    "condition": {"field": "risk_score", "op": ">", "value": 0.8},
                    "result": "ESCALATE"
                }
            context: Runtime data (e.g., {"risk_score": 0.85, "confidence": 0.9})
            
        Returns:
            The 'result' value if condition is true, else None
        """
        condition = logic_json.get("condition")
        result_value = logic_json.get("result")
        
        if not condition:
            # No condition means always true
            return result_value
        
        # Evaluate condition
        is_satisfied = self._evaluate_condition(condition, context)
        
        return result_value if is_satisfied else None
    
    def _evaluate_condition(self, condition: Dict, context: Dict) -> bool:
        """Recursively evaluate a condition."""
        
        # Handle logical operators (AND, OR, NOT)
        if "all" in condition:
            # AND logic
            return all(self._evaluate_condition(c, context) for c in condition["all"])
        
        if "any" in condition:
            # OR logic
            return any(self._evaluate_condition(c, context) for c in condition["any"])
        
        if "not" in condition:
            # NOT logic
            return not self._evaluate_condition(condition["not"], context)
        
        # Handle simple comparison
        field = condition.get("field")
        op_str = condition.get("op")
        value = condition.get("value")
        
        if not field or not op_str:
            raise ValueError(f"Invalid condition: {condition}")
        
        # Get field value from context
        context_value = context.get(field)
        
        if context_value is None:
            return False  # Missing field treated as false
        
        # Get operator function
        op_func = self.OPERATORS.get(op_str)
        if not op_func:
            raise ValueError(f"Unknown operator: {op_str}")
        
        # Evaluate
        try:
            return op_func(context_value, value)
        except Exception as e:
            raise ValueError(f"Failed to evaluate condition: {e}")
    
    def evaluate_rules(
        self, 
        rules: list[Dict[str, Any]], 
        context: Dict[str, Any]
    ) -> Union[Any, None]:
        """
        Evaluate multiple rules in order, return first matching result.
        
        Args:
            rules: List of rule logic objects
            context: Runtime context
            
        Returns:
            First matching rule result, or None
        """
        for rule in rules:
            result = self.evaluate(rule, context)
            if result is not None:
                return result
        return None
