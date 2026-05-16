"""
COMPLETE SYSTEM INTEGRATION TEST
Tests all major features of the AI-Governed Workflow System
"""
import requests
import json
from uuid import uuid4
import time

BASE_URL = "http://127.0.0.1:8000/api/v1"
test_results = {"passed": 0, "failed": 0, "total": 0}

def test(name):
    """Decorator to track test results."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            global test_results
            test_results["total"] += 1
            print(f"\n{'='*70}")
            print(f"TEST {test_results['total']}: {name}")
            print('='*70)
            try:
                result = func(*args, **kwargs)
                test_results["passed"] += 1
                print(f"✓ PASS: {name}")
                return result
            except AssertionError as e:
                test_results["failed"] += 1
                print(f"✗ FAIL: {name}")
                print(f"  Error: {e}")
                raise
            except Exception as e:
                test_results["failed"] += 1
                print(f"✗ ERROR: {name}")
                print(f"  Exception: {e}")
                raise
        return wrapper
    return decorator


# Global variable to store token
ACCESS_TOKEN = None
USER_EMAIL = f"testuser_{uuid4().hex[:8]}@example.com"


@test("Server Health Check")
def test_health():
    response = requests.get("http://127.0.0.1:8000/health")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert data["status"] == "healthy", f"Expected healthy, got {data.get('status')}"
    print(f"  Server status: {data['status']}")


@test("Root Endpoint")
def test_root():
    response = requests.get(f"{BASE_URL}/")
    assert response.status_code in [200, 404], f"Expected 200 or 404, got {response.status_code}"
    print(f"  Root endpoint accessible")


@test("User Registration")
def test_register():
    register_data = {
        "email": USER_EMAIL,
        "password": "SecurePass123!",
        "full_name": "Integration Test User"
    }
    
    response = requests.post(f"{BASE_URL}/auth/register", json=register_data)
    assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
    
    user = response.json()
    assert user["email"] == USER_EMAIL, f"Email mismatch"
    assert user["is_active"] == True, f"User should be active"
    print(f"  User created: {user['email']}")
    print(f"  User ID: {user['id']}")


@test("User Login & JWT Token")
def test_login():
    global ACCESS_TOKEN
    
    login_data = {
        "username": USER_EMAIL,
        "password": "SecurePass123!"
    }
    
    response = requests.post(f"{BASE_URL}/auth/login", data=login_data)
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    
    token_data = response.json()
    assert "access_token" in token_data, "No access token in response"
    assert token_data["token_type"] == "bearer", f"Wrong token type: {token_data['token_type']}"
    
    ACCESS_TOKEN = token_data["access_token"]
    print(f"  JWT Token received: {ACCESS_TOKEN[:50]}...")


@test("Get Current User Profile")
def test_get_profile():
    assert ACCESS_TOKEN is not None, "No access token available"
    
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    response = requests.get(f"{BASE_URL}/auth/me", headers=headers)
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    
    profile = response.json()
    assert profile["email"] == USER_EMAIL, f"Email mismatch"
    print(f"  Profile retrieved: {profile['email']}")
    print(f"  Full name: {profile['full_name']}")


@test("List Workflows")
def test_list_workflows():
    response = requests.get(f"{BASE_URL}/workflows/")
    assert response.status_code in [200, 404], f"Expected 200 or 404, got {response.status_code}"
    
    if response.status_code == 200:
        workflows = response.json()
        print(f"  Found {len(workflows)} workflows")
    else:
        print(f"  No workflows yet (empty)")


@test("Create Workflow Draft")
def test_create_workflow():
    workflow_data = {
        "name": "Test Fraud Detection Workflow",
        "description": "Integration test workflow for fraud detection",
        "domain": "fraud",
        "steps": [
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Risk Assessment",
                "description": "AI analyzes transaction risk",
                "config_json": {
                    "rag_query": "fraud detection rules",
                    "prompt_template": "Analyze this transaction for fraud risk"
                }
            },
            {
                "order_index": 1,
                "step_type": "DOMAIN_RULE",
                "name": "Threshold Check",
                "description": "Apply risk thresholds",
                "config_json": {
                    "rule_id": "high_risk_threshold"
                }
            }
        ]
    }
    
    response = requests.post(f"{BASE_URL}/workflows/draft", json=workflow_data)
    assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
    
    workflow = response.json()
    assert workflow["name"] == workflow_data["name"], "Name mismatch"
    assert workflow["status"] == "DRAFT", f"Expected DRAFT status, got {workflow['status']}"
    assert len(workflow["steps"]) == 2, f"Expected 2 steps, got {len(workflow['steps'])}"
    
    print(f"  Workflow created: {workflow['name']}")
    print(f"  Workflow ID: {workflow['id']}")
    print(f"  Status: {workflow['status']}")
    print(f"  Steps: {len(workflow['steps'])}")
    
    return workflow["id"]


@test("List Documents")
def test_list_documents():
    response = requests.get(f"{BASE_URL}/documents/")
    assert response.status_code in [200, 404], f"Expected 200 or 404, got {response.status_code}"
    
    if response.status_code == 200:
        documents = response.json()
        print(f"  Found {len(documents)} documents")
    else:
        print(f"  No documents yet (empty)")


@test("API Documentation Available")
def test_api_docs():
    response = requests.get("http://127.0.0.1:8000/docs")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    print(f"  Swagger UI accessible at /docs")


# Execute all tests
if __name__ == "__main__":
    print("\n" + "="*70)
    print(" " * 15 + "COMPLETE SYSTEM INTEGRATION TEST")
    print("="*70)
    
    start_time = time.time()
    
    try:
        test_health()
        test_root()
        test_register()
        test_login()
        test_get_profile()
        test_list_workflows()
        workflow_id = test_create_workflow()
        test_list_documents()
        test_api_docs()
        
    except Exception as e:
        print(f"\n\nTest suite stopped due to error: {e}")
    
    # Print summary
    elapsed = time.time() - start_time
    
    print("\n" + "="*70)
    print(" " * 25 + "TEST SUMMARY")
    print("="*70)
    print(f"Total Tests:    {test_results['total']}")
    print(f"Passed:         {test_results['passed']} ✓")
    print(f"Failed:         {test_results['failed']} ✗")
    print(f"Success Rate:   {(test_results['passed']/test_results['total']*100):.1f}%")
    print(f"Time Elapsed:   {elapsed:.2f}s")
    print("="*70)
    
    if test_results['failed'] == 0:
        print("\n🎉 ALL TESTS PASSED! SYSTEM FULLY OPERATIONAL 🎉\n")
        exit(0)
    else:
        print(f"\n⚠️  {test_results['failed']} TEST(S) FAILED\n")
        exit(1)
