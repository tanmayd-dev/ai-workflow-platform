"""Test authentication endpoints."""
import requests
import json

BASE_URL = "http://127.0.0.1:8000/api/v1"

# Test 1: Register a user
print("=" * 60)
print("TEST 1: User Registration")
print("=" * 60)

register_data = {
    "email": "admin@test.com",
    "password": "admin1234",
    "full_name": "Admin User"
}

try:
    response = requests.post(f"{BASE_URL}/auth/register", json=register_data)
    print(f"Status: {response.status_code}")
    if response.status_code == 201:
        user = response.json()
        print(f"✓ User created successfully!")
        print(f"  - ID: {user['id']}")
        print(f"  - Email: {user['email']}")
        print(f"  - Name: {user['full_name']}")
    else:
        print(f"✗ Registration failed")
        print(f"  Error: {response.text}")
except Exception as e:
    print(f"✗ Error: {e}")

# Test 2: Login
print("\n" + "=" * 60)
print("TEST 2: User Login")
print("=" * 60)

login_data = {
    "username": "admin@test.com",  # OAuth2 uses 'username' field
    "password": "admin1234"
}

try:
    response = requests.post(
        f"{BASE_URL}/auth/login",
        data=login_data  # form data, not JSON
    )
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        token_data = response.json()
        access_token = token_data['access_token']
        print(f"✓ Login successful!")
        print(f"  - Access Token: {access_token[:50]}...")
        
        # Test 3: Get current user
        print("\n" + "=" * 60)
        print("TEST 3: Get Current User Profile")
        print("=" * 60)
        
        headers = {"Authorization": f"Bearer {access_token}"}
        me_response = requests.get(f"{BASE_URL}/auth/me", headers=headers)
        print(f"Status: {me_response.status_code}")
        if me_response.status_code == 200:
            user_profile = me_response.json()
            print(f"✓ Got user profile!")
            print(f"  - Email: {user_profile['email']}")
            print(f"  - Name: {user_profile['full_name']}")
            print(f"  - Active: {user_profile['is_active']}")
        else:
            print(f"✗ Failed to get profile")
            print(f"  Error: {me_response.text}")
    else:
        print(f"✗ Login failed")
        print(f"  Error: {response.text}")
except Exception as e:
    print(f"✗ Error: {e}")

print("\n" + "=" * 60)
print("TESTS COMPLETE")
print("=" * 60)
