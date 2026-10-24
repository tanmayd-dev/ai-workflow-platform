"""
End-to-end demonstration of the AI-Governed Workflow System.

This script demonstrates the full workflow:
1. Upload a regulatory document
2. Draft a workflow from natural language intent
3. Create and approve a workflow
4. (Future) Execute the workflow
"""
import requests
import json
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"

def print_section(title):
    """Print a formatted section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

def create_sample_document():
    """Create a sample regulatory document."""
    content = """
FRAUD DETECTION POLICY - ACME BANK

High-Risk Transaction Criteria:
1. Transaction amount exceeds $10,000
2. Transaction originates from high-risk jurisdiction
3. Multiple failed authentication attempts in past 24 hours
4. Transaction pattern deviates significantly from account history

Risk Assessment Requirements:
- Transactions with risk score > 0.8: Immediate escalation to fraud team with human review required within 2 hours
- Transactions with risk score 0.5-0.8: Additional verification required, may proceed after automated checks
- Transactions with risk score < 0.5: Auto-approve with standard monitoring

Mandatory Controls:
- All transactions over $50,000 require dual approval
- AI risk assessments must be logged with full context
- All escalated cases must maintain complete audit trail
- Decisions must be replayable for regulatory compliance

Approved Processing Steps:
1. AI analyzes transaction against historical patterns and known fraud indicators
2. Rule engine evaluates risk score against threshold policies  
3. Human approval for high-risk or high-value transactions
4. Final decision logged with complete reasoning chain
"""
    
    filepath = Path("sample_fraud_policy.txt")
    filepath.write_text(content)
    return filepath

## Step 1: Upload Document
print_section("STEP 1: Upload Regulatory Document")

# Create sample document
doc_path = create_sample_document()
print(f"✓ Created sample document: {doc_path}")

try:
    with open(doc_path, 'rb') as f:
        files = {'file': (doc_path.name, f, 'text/plain')}
        data = {
            'domain': 'fraud',
            'description': 'Fraud detection policy and procedures'
        }
        
        response = requests.post(
            f"{BASE_URL}/api/v1/documents/upload",
            files=files,
            data=data
        )
        
        if response.status_code == 201:
            doc_data = response.json()
            print(f"✓ Document uploaded successfully!")
            print(f"  - ID: {doc_data['id']}")
            print(f"  - Chunks created: {doc_data['chunk_count']}")
            print(f"  - Domain: {doc_data['domain']}")
        else:
            print(f"✗ Upload failed: {response.status_code}")
            print(f"  Error: {response.text}")
except Exception as e:
    print(f"✗ Upload failed: {e}")

## Step 2: Draft Workflow from Intent
print_section("STEP 2: AI-Assisted Workflow Drafting")

intent_request = {
    "intent": "Create a fraud assessment workflow for high-value transactions that uses AI to analyze risk, applies rule-based thresholds, and requires human approval for high-risk cases",
    "domain": "fraud"
}

try:
    response = requests.post(
        f"{BASE_URL}/api/v1/intents/draft",
        json=intent_request
    )
    
    if response.status_code == 200:
        result = response.json()
        print("✓ AI-generated workflow draft:")
        print(json.dumps(result['workflow_draft'], indent=2))
        
        # Save for next step
        workflow_draft = result['workflow_draft']
        print(f"\n✓ Retrieved context from {len(result.get('retrieved_context', ''))} characters of regulatory docs")
    else:
        print(f"✗ Drafting failed: {response.status_code}")
        print(f"  Error: {response.text}")
        # Create manual workflow as fallback
        workflow_draft = {
            "name": "Fraud Assessment Workflow",
            "description": "AI-assisted fraud detection for high-value transactions",
            "steps": []
        }
except Exception as e:
    print(f"✗ Drafting failed: {e}")
    workflow_draft = {
        "name": "Fraud Assessment Workflow",
        "description": "AI-assisted fraud detection workflow",
        "steps": []
    }

## Step 3: Create Workflow
print_section("STEP 3: Create and Approve Workflow")

# Use the AI-drafted workflow or create a manual one
workflow_create_request = {
    "name": workflow_draft.get("name", "Fraud Assessment Workflow"),
    "description": workflow_draft.get("description", "AI-assisted fraud detection"),
    "domain": "fraud",
    "steps": workflow_draft.get("steps", [
        {
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Risk Scoring",
            "description": "AI analyzes transaction for fraud indicators",
            "config_json": {
                "rag_query": "fraud risk assessment criteria",
                "prompt_template": "Analyze this transaction for fraud risk based on regulatory guidelines"
            }
        },
        {
            "order_index": 1,
            "step_type": "DOMAIN_RULE",
            "name": "Threshold Evaluation",
            "description": "Apply rule-based risk thresholds",
            "config_json": {
                "rule_id": "fraud_risk_threshold"
            }
        },
        {
            "order_index": 2,
            "step_type": "HUMAN_APPROVAL",
            "name": "Manual Review",
            "description": "Human reviews high-risk transactions",
            "config_json": {}
        }
    ])
}

try:
    response = requests.post(
        f"{BASE_URL}/api/v1/workflows/draft",
        json=workflow_create_request
    )
    
    if response.status_code == 201:
        workflow = response.json()
        workflow_id = workflow['id']
        print(f"✓ Workflow created successfully!")
        print(f"  - ID: {workflow_id}")
        print(f"  - Name: {workflow['name']}")
        print(f"  - Status: {workflow['status']}")
        print(f"  - Steps: {len(workflow['steps'])}")
        
        # Approve the workflow
        print("\n  Approving workflow...")
        approval_response = requests.post(
            f"{BASE_URL}/api/v1/workflows/{workflow_id}/approve"
        )
        
        if approval_response.status_code == 200:
            approved_workflow = approval_response.json()
            print(f"  ✓ Workflow approved!")
            print(f"    - Status: {approved_workflow['status']}")
            print(f"    - Approved at: {approved_workflow['approved_at']}")
        else:
            print(f"  ✗ Approval failed: {approval_response.status_code}")
    else:
        print(f"✗ Creation failed: {response.status_code}")
        print(f"  Error: {response.text}")
except Exception as e:
    print(f"✗ Workflow creation failed: {e}")

## Step 4: List All Resources
print_section("STEP 4: View System State")

# List workflows
try:
    response = requests.get(f"{BASE_URL}/api/v1/workflows/")
    workflows = response.json()
    print(f"✓ Total workflows: {len(workflows)}")
    for wf in workflows:
        print(f"  - {wf['name']} ({wf['status']}) - {len(wf['steps'])} steps")
except Exception as e:
    print(f"✗ Failed to list workflows: {e}")

# List documents
try:
    response = requests.get(f"{BASE_URL}/api/v1/documents/")
    documents = response.json()
    print(f"\n✓ Total documents: {len(documents)}")
    for doc in documents:
        print(f"  - {doc['filename']} ({doc['domain']}) - {doc['chunk_count']} chunks")
except Exception as e:
    print(f"✗ Failed to list documents: {e}")

print_section("DEMONSTRATION COMPLETE")
print("✓ System is operational and ready for execution!")
print("\n📚 Next steps:")
print("  1. Implement LangGraph execution engine")
print("  2. Create workflow runs")
print("  3. Test replay functionality")
print("  4. Add authentication")
print("\n📖 See API_TESTING.md for more examples")
print("📖 See walkthrough.md for architecture details")
