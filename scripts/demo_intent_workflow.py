"""
Demo: AI-Generated Workflow from Intent
1. Uploads Loan Policy to Knowledge Base
2. Asks AI to "Create a workflow to approve a loan"
3. Displays the generated workflow definition

NOTE (status): This script hits real AI/LLM paths (OpenRouter intent drafting
plus RAG ingestion with the local embedding model) and needs a live server,
per-role users (author for upload/drafting -- currently it sends no auth at
all), and a working OpenRouter key. It is deprecated in favor of
scripts/demo.py, the authoritative authenticated, offline-capable walkthrough.
Use it only for manual live sessions; its logic is not being ported across.
"""
import requests
import json
import os

BASE_URL = "http://127.0.0.1:8000/api/v1"

def upload_policy():
    print("\n[1] Uploading Loan Policy to Knowledge Base...")
    file_path = "sample_docs/loan_policy.txt"
    
    if not os.path.exists(file_path):
        print(f"✗ File not found: {file_path}")
        return False
        
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, "text/plain")}
        data = {
            "domain": "loan_processing",
            "description": "Standard loan approval guidelines and compliance checks"
        }
        
        response = requests.post(f"{BASE_URL}/documents/upload", files=files, data=data)
        
    if response.status_code == 201:
        print("✓ Policy uploaded successfully!")
        print(f"  ID: {response.json()['id']}")
        print(f"  Chunks: {response.json()['chunks_created']}")
        return True
    else:
        print(f"✗ Upload failed: {response.text}")
        return False

def generate_workflow():
    print("\n[2] Generating Workflow from Intent...")
    print("  Intent: 'Create a workflow to approve a loan'")
    print("  Domain: 'loan_processing'")
    
    payload = {
        "intent": "Create a workflow to approve a loan",
        "domain": "loan_processing"
    }
    
    response = requests.post(f"{BASE_URL}/intents/draft", json=payload)
    
    if response.status_code == 200:
        result = response.json()
        workflow = result["workflow_draft"]
        context = result["retrieved_context"]
        
        print("\n✅ WORKFLOW GENERATED SUCCESSFULLY!")
        print("-" * 60)
        print(f"Name: {workflow.get('name')}")
        print(f"Description: {workflow.get('description')}")
        print("-" * 60)
        
        print("\nSteps Generated:")
        for step in workflow.get("steps", []):
            print(f"  [{step['order_index']}] {step['name']} ({step['step_type']})")
            print(f"      Desc: {step['description']}")
            if "config_json" in step:
                print(f"      Config: {json.dumps(step['config_json'], indent=8)}")
        
        print("-" * 60)
        print("\nContext Retrieved from RAG:")
        print(f"{context[:300]}...")
        
    else:
        print(f"✗ Generation failed: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    print("="*70)
    print("AI INTENT WORKFLOW GENERATION DEMO")
    print("="*70)
    
    if upload_policy():
        generate_workflow()
    
    print("\n" + "="*70)
