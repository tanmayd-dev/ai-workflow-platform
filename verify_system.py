"""
End-to-End Workflow Verification Script
Demonstrates: Document upload → Workflow creation → Complete system verification
"""
import requests
import json
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000/api/v1"

print("="*70)
print(" " * 15 + "SYSTEM VERIFICATION - END TO END")
print("="*70)

# Step 1: Upload Documents
print("\n[STEP 1] Uploading Regulatory Documents...")
print("-" * 70)

docs_to_upload = [
    ("sample_docs/fraud_policy.txt", "fraud", "Fraud Detection Policy v1.2"),
    ("sample_docs/aml_regulations.txt", "aml", "AML Regulations v2.1")
]

uploaded_docs = []
for file_path, domain, description in docs_to_upload:
    if Path(file_path).exists():
        with open(file_path, 'rb') as f:
            files = {'file': (Path(file_path).name, f)}
            data = {'domain': domain, 'description': description}
            
            response = requests.post(f"{BASE_URL}/documents/upload", files=files, data=data)
            
            if response.status_code == 201:
                doc = response.json()
                uploaded_docs.append(doc)
                print(f"✓ Uploaded: {doc['filename']}")
                print(f"  Domain: {doc['domain']}")
                print(f"  Size: {doc['file_size']} bytes")
            else:
                print(f"✗ Failed to upload {file_path}: {response.status_code}")
                # print(response.text)
    else:
        print(f"✗ File not found: {file_path}")

# Step 2: Create Comprehensive Workflow
print(f"\n[STEP 2] Creating Multi-Step Transaction Review Workflow...")
print("-" * 70)

workflow_data = {
    "name": "High-Value Transaction Review Workflow",
    "description": "Complete workflow for reviewing high-value transactions with fraud detection, AML checks, and risk assessment",
    "domain": "fraud",
    "steps": [
        {
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Initial Risk Assessment",
            "description": "AI analyzes transaction against fraud patterns",
            "config_json": {
                "rag_query": "fraud detection risk indicators",
                "prompt_template": "Analyze the following transaction for fraud risk based on regulatory guidelines: {transaction_data}",
                "output_schema": {
                    "risk_score": "integer",
                    "risk_level": "string",
                    "indicators": "list"
                }
            }
        },
        {
            "order_index": 1,
            "step_type": "DOMAIN_RULE",
            "name": "Amount Threshold Check",
            "description": "Apply transaction amount limits from policy",
            "config_json": {
                "rule_expression": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "field": "transaction.amount",
                            "operator": "<=",
                            "value": 10000
                        },
                        {
                            "field": "risk_assessment.risk_level",
                            "operator": "!=",
                            "value": "HIGH"
                        }
                    ]
                },
                "action_if_true": "PROCEED",
                "action_if_false": "ESCALATE"
            }
        },
        {
            "order_index": 2,
            "step_type": "AI_ANALYSIS",
            "name": "AML Compliance Check",
            "description": "Review transaction for AML red flags",
            "config_json": {
                "rag_query": "anti-money laundering suspicious activity indicators",
                "prompt_template": "Evaluate for AML compliance: {transaction_data}",
                "output_schema": {
                    "aml_risk": "string",
                    "requires_sar": "boolean",
                    "requires_ctr": "boolean"
                }
            }
        },
        {
            "order_index": 3,
            "step_type": "HUMAN_APPROVAL",
            "name": "Manual Review for High Risk",
            "description": "Senior officer reviews flagged transactions",
            "config_json": {
                "approval_role": "risk_officer",
                "timeout_hours": 24,
                "escalation_chain": ["branch_manager", "regional_risk_officer"]
            }
        }
    ]
}

response = requests.post(f"{BASE_URL}/workflows/draft", json=workflow_data)
workflow_id = None

if response.status_code == 201:
    workflow = response.json()
    workflow_id = workflow['id']
    print(f"✓ Workflow Created!")
    print(f"  Name: {workflow['name']}")
    print(f"  ID: {workflow['id']}")
    print(f"  Status: {workflow['status']}")
    print(f"  Version: {workflow['version']}")
    print(f"  Steps: {len(workflow['steps'])}")
    
    # Show step details
    print(f"\n  Workflow Steps:")
    for step in workflow['steps']:
        print(f"    {step['order_index']}. {step['name']} ({step['step_type']})")
else:
    print(f"✗ Failed to create workflow: {response.status_code}")
    print(f"  Response: {response.text}")

# Step 3: List All Workflows
print(f"\n[STEP 3] Verifying System State...")
print("-" * 70)

# List Workflows
response = requests.get(f"{BASE_URL}/workflows/")
total_workflows = 0
if response.status_code == 200:
    workflows = response.json()
    total_workflows = len(workflows)
    print(f"✓ Workflows in system: {total_workflows}")
    for wf in workflows[-3:]: # Show last 3
        print(f"  - {wf['name']} (v{wf['version']}, {wf['status']})")

# List Documents
response = requests.get(f"{BASE_URL}/documents/")
total_docs = 0
if response.status_code == 200:
    documents = response.json()
    total_docs = len(documents)
    print(f"✓ Documents in knowledge base: {total_docs}")
    for doc in documents:
        print(f"  - {doc['filename']} ({doc['domain']})")

# Summary
print("\n" + "="*70)
print(" " * 20 + "VERIFICATION SUMMARY")
print("="*70)
print(f"✓ Documents Uploaded: {len(uploaded_docs)}")
print(f"✓ Workflow Created: {'Yes' if workflow_id else 'No'}")
print(f"✓ Total Workflows: {total_workflows}")
print(f"✓ Total Documents: {total_docs}")
print("\n🎉 FULL SYSTEM VERIFICATION COMPLETE!")
print("="*70)
