"""
Simple Workflow Execution Test
"""
import requests
import json

BASE_URL = "http://127.0.0.1:8000/api/v1"

print("="*70)
print("WORKFLOW EXECUTION TEST")
print("="*70)

# Use an existing workflow
print("\n[1] Getting existing workflow...")
response = requests.get(f"{BASE_URL}/workflows/")
workflows = response.json()
print(f"Found {len(workflows)} workflows")

# Find an approved one or use first draft
approved_workflow = None
for wf in workflows:
    if wf['status'] == 'APPROVED':
        approved_workflow = wf
        break

if not approved_workflow and workflows:
    # Approve the first workflow
    wf_id = workflows[0]['id']
    print(f"\n[2] Approving workflow {wf_id}...")
    response = requests.post(f"{BASE_URL}/workflows/{wf_id}/approve")
    if response.status_code == 200:
        approved_workflow = response.json()
        print(f"✓ Approved: {approved_workflow['name']}")
    else:
        print(f"✗ Failed to approve: {response.status_code}")
        print(response.text)
        exit(1)

if approved_workflow:
    print(f"\n[3] Executing workflow...")
    print(f"  Name: {approved_workflow['name']}")
    print(f"  ID: {approved_workflow['id']}")
    
    # Create execution
    run_data = {
        "workflow_id": approved_workflow['id'],
        "account_id": "test-account-123",
        "trigger_reason": "Manual test execution"
    }
    
    response = requests.post(f"{BASE_URL}/runs/", json=run_data)
    print(f"  Response: {response.status_code}")
    
    if response.status_code == 201:
        run = response.json()
        print(f"✓ Execution created!")
        print(f"  Run ID: {run['id']}")
        print(f"  Status: {run['status']}")
        print(f"\n[4] Checking run details...")
        
        response = requests.get(f"{BASE_URL}/runs/{run['id']}")
        if response.status_code == 200:
            details = response.json()
            print(f"✓ Run Status: {details['status']}")
        
        print(f"\n[5] Checking audit trail...")
        response = requests.get(f"{BASE_URL}/runs/{run['id']}/executions")
        if response.status_code == 200:
            executions = response.json()
            print(f"✓ Found {len(executions)} node executions")
        
        print(f"\n🎉 WORKFLOW EXECUTION SUCCESSFUL!")
    else:
        print(f"✗ Failed: {response.status_code}")
        print(response.text)
else:
    print("No workflows available")

print("="*70)
