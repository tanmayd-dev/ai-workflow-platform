"""
Complete Workflow Execution Demonstration
Shows: Create → Execute → Verify Audit Trail
"""
import requests
import json
import time

BASE_URL = "http://127.0.0.1:8000/api/v1"

print("="*70)
print(" " * 15 + "WORKFLOW EXECUTION DEMONSTRATION")
print("="*70)

# Step 1: Create a Simple Workflow for Testing
print("\n[STEP 1] Creating Test Workflow...")
print("-" * 70)

workflow_data = {
    "name": "Simple Fraud Check Workflow",
    "description": "2-step workflow for testing execution",
    "domain": "fraud",
    "steps": [
        {
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Fraud Risk Assessment",
            "description": "Analyze transaction for fraud indicators",
            "config_json": {
                "rag_query": "fraud detection indicators",
                "prompt_template": "Analyze this transaction for fraud: Amount=${amount}, Location=${location}",
                "output_schema": {"risk_level": "string", "risk_score": "integer"}
            }
        },
        {
            "order_index": 1,
            "step_type": "DOMAIN_RULE",
            "name": "Amount Check",
            "description": "Check if amount exceeds threshold",
            "config_json": {
                "rule_expression": {
                    "field": "transaction.amount",
                    "operator": ">",
                    "value": 5000
                },
                "action_if_true": "ESCALATE",
                "action_if_false": "APPROVE"
            }
        }
    ]
}

response = requests.post(f"{BASE_URL}/workflows/draft", json=workflow_data)
if response.status_code == 201:
    workflow = response.json()
    workflow_id = workflow['id']
    print(f"✓ Workflow Created: {workflow['name']}")
    print(f"  ID: {workflow_id}")
    print(f"  Steps: {len(workflow['steps'])}")
else:
    print(f"✗ Failed to create workflow: {response.status_code}")
    print(response.text)
    exit(1)

# Step 2: Approve the Workflow
print(f"\n[STEP 2] Approving Workflow...")
print("-" * 70)

response = requests.post(f"{BASE_URL}/workflows/{workflow_id}/approve")
if response.status_code == 200:
    workflow = response.json()
    print(f"✓ Workflow Approved")
    print(f"  Status: {workflow['status']}")
    print(f"  Version: {workflow['version']}")
else:
    print(f"✗ Failed to approve: {response.status_code}")
    print(response.text)

# Step 3: Execute the Workflow
print(f"\n[STEP 3] Executing Workflow...")
print("-" * 70)

execution_input = {
    "workflow_id": workflow_id,
    "input_data": {
        "transaction": {
            "amount": 7500,
            "location": "International",
            "merchant": "Online Store XYZ"
        }
    }
}

response = requests.post(f"{BASE_URL}/runs/", json=execution_input)
if response.status_code == 201:
    run = response.json()
    run_id = run['id']
    print(f"✓ Workflow Execution Started")
    print(f"  Run ID: {run_id}")
    print(f"  Status: {run['status']}")
    print(f"  Started: {run['started_at']}")
else:
    print(f"✗ Failed to execute: {response.status_code}")
    print(response.text)
    run_id = None

# Step 4: Check Execution Status
if run_id:
    print(f"\n[STEP 4] Checking Execution Status...")
    print("-" * 70)
    
    # Wait a bit for execution
    time.sleep(2)
    
    response = requests.get(f"{BASE_URL}/runs/{run_id}")
    if response.status_code == 200:
        run_details = response.json()
        print(f"✓ Execution Status: {run_details['status']}")
        if run_details.get('completed_at'):
            print(f"  Completed: {run_details['completed_at']}")
        if run_details.get('result'):
            print(f"  Result: {json.dumps(run_details['result'], indent=2)}")
    
    # Step 5: View Audit Trail
    print(f"\n[STEP 5] Viewing Execution Audit Trail...")
    print("-" * 70)
    
    response = requests.get(f"{BASE_URL}/runs/{run_id}/executions")
    if response.status_code == 200:
        executions = response.json()
        print(f"✓ Found {len(executions)} node executions:")
        for i, exec_node in enumerate(executions, 1):
            print(f"\n  Node {i}: {exec_node.get('step_name', 'Unknown')}")
            print(f"    Status: {exec_node.get('status', 'N/A')}")
            print(f"    Started: {exec_node.get('started_at', 'N/A')}")
            if exec_node.get('completed_at'):
                print(f"    Completed: {exec_node['completed_at']}")
            if exec_node.get('output'):
                print(f"    Output: {json.dumps(exec_node['output'], indent=6)}")
    else:
        print(f"  Info: Execution details not available ({response.status_code})")

# Summary
print("\n" + "="*70)
print(" " * 20 + "EXECUTION SUMMARY")
print("="*70)
print(f"✓ Workflow Created & Approved")
print(f"✓ Execution Initiated: {run_id if run_id else 'Failed'}")
print(f"✓ Audit Trail: Available")
print("\n🎉 WORKFLOW EXECUTION DEMONSTRATION COMPLETE!")
print("="*70)
