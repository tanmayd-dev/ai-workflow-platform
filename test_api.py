"""Simple test script to verify API endpoints."""
import requests
import json

BASE_URL = "http://127.0.0.1:8000"

def test_health():
    """Test health endpoint."""
    print("Testing /health endpoint...")
    try:
        response = requests.get(f"{BASE_URL}/health")
        print(f"✓ Status: {response.status_code}")
        print(f"✓ Response: {response.json()}")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False

def test_root():
    """Test root endpoint."""
    print("\nTesting / endpoint...")
    try:
        response = requests.get(f"{BASE_URL}/")
        print(f"✓ Status: {response.status_code}")
        print(f"✓ Response: {json.dumps(response.json(), indent=2)}")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False

def test_list_workflows():
    """Test workflow list endpoint."""
    print("\nTesting GET /api/v1/workflows/ endpoint...")
    try:
        response = requests.get(f"{BASE_URL}/api/v1/workflows/")
        print(f"✓ Status: {response.status_code}")
        print(f"✓ Workflows found: {len(response.json())}")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False

def test_list_documents():
    """Test document list endpoint."""
    print("\nTesting GET /api/v1/documents/ endpoint...")
    try:
        response = requests.get(f"{BASE_URL}/api/v1/documents/")
        print(f"✓ Status: {response.status_code}")
        print(f"✓ Documents found: {len(response.json())}")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("API ENDPOINT VERIFICATION")
    print("=" * 60)
    
    results = []
    results.append(("Health Check", test_health()))
    results.append(("Root Endpoint", test_root()))
    results.append(("List Workflows", test_list_workflows()))
    results.append(("List Documents", test_list_documents()))
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")
    
    total_passed = sum(1 for _, passed in results if passed)
    print(f"\nTotal: {total_passed}/{len(results)} tests passed")
