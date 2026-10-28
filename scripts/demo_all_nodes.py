"""
Demonstration: All Node Types in One Workflow
Shows: AI_ANALYSIS (LLM), DOMAIN_RULE (System), HUMAN_APPROVAL (Human)

NOTE (status): This script hits real AI/LLM paths and needs a live server,
per-role users (author, approver, executor -- currently it sends no auth at
all, so it only works against an unauthenticated or fully-bypassing session),
and a working OpenRouter key. It is deprecated in favor of scripts/demo.py,
the authoritative authenticated, offline-capable walkthrough. Use it only for
manual live sessions; its logic is not being ported across.
"""
import requests
import json

BASE_URL = "http://127.0.0.1:8000/api/v1"

print("="*70)
print("COMPLETE NODE TYPE DEMONSTRATION")
print("="*70)

# Create workflow with ALL node types
workflow_data = {
    "name": "Complete Node Type Demo Workflow",
    "description": "Demonstrates all 3 node types: AI, System, Human",
    "domain": "demo",
    "steps": [
        {
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "LLM Node - Risk Analysis",
            "description": "AI/LLM analyzes transaction using RAG",
            "config_json": {
                "rag_query": "fraud indicators",
                "prompt_template": "Analyze transaction: {data}",
                "output_schema": {"risk": "string", "score": "integer"}
            }
        },
        {
            "order_index": 1,
            "step_type": "DOMAIN_RULE",
            "name": "System Node - Rule Check",
            "description": "Deterministic rule evaluation (system logic)",
            "config_json": {
                "rule_expression": {
                    "field": "transaction.amount",
                    "operator": ">",
                    "value": 10000
                },
                "action_if_true": "ESCALATE",
                "action_if_false": "PROCEED"
            }
        },
        {
            "order_index": 2,
            "step_type": "HUMAN_APPROVAL",
            "name": "Human Node - Manual Review",
            "description": "Human officer approval required",
            "config_json": {
                "approval_role": "compliance_officer",
                "timeout_hours": 48,
                "escalation_chain": ["supervisor", "director"]
            }
        }
    ]
}

print("\n[1] Creating workflow with ALL node types...")
print("-" * 70)
response = requests.post(f"{BASE_URL}/workflows/draft", json=workflow_data)

if response.status_code == 201:
    workflow = response.json()
    print(f"✓ Workflow Created: {workflow['name']}")
    print(f"  ID: {workflow['id']}")
    print(f"\n  Node Types Demonstrated:")
    
    for step in workflow['steps']:
        node_type = step['step_type']
        emoji = "🤖" if node_type == "AI_ANALYSIS" else ("⚙️" if node_type == "DOMAIN_RULE" else "👤")
        type_name = "LLM/AI" if node_type == "AI_ANALYSIS" else ("System" if node_type == "DOMAIN_RULE" else "Human")
        
        print(f"    {emoji} Step {step['order_index']}: {step['name']}")
        print(f"       Type: {type_name} ({node_type})")
    
    print(f"\n✅ ALL 3 NODE TYPES VERIFIED!")
    print(f"\n  🤖 AI_ANALYSIS   = LLM Node (RAG + AI reasoning)")
    print(f"  ⚙️  DOMAIN_RULE   = System Node (Deterministic rules)")
    print(f"  👤 HUMAN_APPROVAL = Human Node (Manual approval)")
    
else:
    print(f"✗ Failed: {response.status_code}")
    print(response.text)

print("\n" + "="*70)
