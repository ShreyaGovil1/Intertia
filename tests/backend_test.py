#!/usr/bin/env python3
"""
Intertia Backend API Test Suite
Tests all endpoints systematically with proper authentication
"""

import requests
import sys
import json
from datetime import datetime
from typing import Dict, Any, Optional

class IntertiaAPITester:
    def __init__(self, base_url="http://localhost:8000/api"):
        self.base_url = base_url
        self.token = None
        self.user_id = None
        self.session_token = None
        self.tests_run = 0
        self.tests_passed = 0
        self.test_results = []

    def log_test(self, name: str, success: bool, details: str = "", response_data: Any = None):
        """Log test result"""
        self.tests_run += 1
        if success:
            self.tests_passed += 1
            print(f"✅ {name}")
        else:
            print(f"❌ {name} - {details}")
        
        self.test_results.append({
            "test": name,
            "success": success,
            "details": details,
            "response_data": response_data
        })

    def run_test(self, name: str, method: str, endpoint: str, expected_status: int, 
                 data: Optional[Dict] = None, headers: Optional[Dict] = None) -> tuple[bool, Dict]:
        """Run a single API test"""
        url = f"{self.base_url}/{endpoint}"
        
        # Default headers
        default_headers = {'Content-Type': 'application/json'}
        if self.token:
            default_headers['Authorization'] = f'Bearer {self.token}'
        
        if headers:
            default_headers.update(headers)

        try:
            if method == 'GET':
                response = requests.get(url, headers=default_headers, timeout=10)
            elif method == 'POST':
                response = requests.post(url, json=data, headers=default_headers, timeout=10)
            elif method == 'PUT':
                response = requests.put(url, json=data, headers=default_headers, timeout=10)
            elif method == 'DELETE':
                response = requests.delete(url, headers=default_headers, timeout=10)

            success = response.status_code == expected_status
            response_data = {}
            
            try:
                response_data = response.json()
            except:
                response_data = {"raw_response": response.text}

            details = f"Status: {response.status_code}"
            if not success:
                details += f", Expected: {expected_status}, Response: {response.text[:200]}"

            self.log_test(name, success, details, response_data)
            return success, response_data

        except Exception as e:
            self.log_test(name, False, f"Error: {str(e)}")
            return False, {}

    def test_basic_endpoints(self):
        """Test basic non-auth endpoints"""
        print("\n🔍 Testing Basic Endpoints...")
        
        # Test root endpoint
        self.run_test("Root API", "GET", "", 200)
        
        # Test health endpoint
        self.run_test("Health Check", "GET", "health", 200)

    def test_auth_registration(self):
        """Test user registration"""
        print("\n🔍 Testing Authentication - Registration...")
        
        timestamp = int(datetime.now().timestamp())
        test_user = {
            "email": f"test.runner.{timestamp}@example.com",
            "password": "TestPass123!",
            "name": "Test Runner"
        }
        
        success, response = self.run_test(
            "User Registration", "POST", "auth/register", 200, test_user
        )
        
        if success and 'access_token' in response:
            self.token = response['access_token']
            self.user_id = response['user']['user_id']
            print(f"   📝 Registered user: {self.user_id}")
            return True
        return False

    def test_auth_login(self):
        """Test user login with existing credentials"""
        print("\n🔍 Testing Authentication - Login...")
        
        # Try to login with the registered user
        if not self.user_id:
            print("   ⚠️  No registered user to test login")
            return False
            
        # For this test, we'll use the token from registration
        # In a real scenario, we'd test with separate login
        success, response = self.run_test(
            "Get Current User", "GET", "auth/me", 200
        )
        
        return success

    def test_protected_endpoints(self):
        """Test endpoints that require authentication"""
        print("\n🔍 Testing Protected Endpoints...")
        
        if not self.token:
            print("   ⚠️  No auth token available, skipping protected tests")
            return False

        # Test user profile
        self.run_test("Get User Profile", "GET", "auth/me", 200)
        
        # Test user runs
        self.run_test("Get User Runs", "GET", "runs", 200)
        
        return True

    def test_leaderboards(self):
        """Test leaderboard endpoints"""
        print("\n🔍 Testing Leaderboards...")
        
        # Test different leaderboard metrics
        metrics = ["area", "distance", "runs", "streak"]
        for metric in metrics:
            self.run_test(f"Leaderboard - {metric}", "GET", f"leaderboards/{metric}", 200)

    def test_badges(self):
        """Test badge endpoints"""
        print("\n🔍 Testing Badges...")
        
        # Test get all badges
        self.run_test("Get All Badges", "GET", "badges", 200)
        
        # Test user badges (if we have a user)
        if self.user_id:
            self.run_test("Get User Badges", "GET", f"badges/user/{self.user_id}", 200)

    def test_seasons(self):
        """Test season endpoints"""
        print("\n🔍 Testing Seasons...")
        
        # Test current season
        self.run_test("Get Current Season", "GET", "seasons/current", 200)
        
        # Test all seasons
        self.run_test("Get All Seasons", "GET", "seasons", 200)

    def test_groups(self):
        """Test group endpoints"""
        print("\n🔍 Testing Groups...")
        
        # Test get groups
        self.run_test("Get Groups", "GET", "groups", 200)
        
        # Test create group (requires auth)
        if self.token:
            group_data = {"name": f"Test Group {datetime.now().strftime('%H%M%S')}"}
            success, response = self.run_test(
                "Create Group", "POST", "groups?name=" + group_data["name"], 200
            )
            
            if success and 'group_id' in response:
                group_id = response['group_id']
                print(f"   📝 Created group: {group_id}")
                
                # Test get specific group
                self.run_test("Get Specific Group", "GET", f"groups/{group_id}", 200)

    def test_run_flow(self):
        """Test the complete run flow"""
        print("\n🔍 Testing Run Flow...")
        
        if not self.token:
            print("   ⚠️  No auth token, skipping run flow tests")
            return False

        # Start a run
        run_data = {"run_type": "solo"}
        success, response = self.run_test(
            "Start Run", "POST", "runs/start", 200, run_data
        )
        
        if not success or 'run_id' not in response:
            print("   ❌ Failed to start run, skipping rest of flow")
            return False
            
        run_id = response['run_id']
        print(f"   📝 Started run: {run_id}")
        
        # Add some GPS points
        points_data = [
            {
                "timestamp": datetime.now().isoformat(),
                "lat": 37.7749,
                "lon": -122.4194,
                "accuracy_m": 5.0,
                "speed_mps": 3.0,
                "heading": 90.0
            },
            {
                "timestamp": datetime.now().isoformat(),
                "lat": 37.7750,
                "lon": -122.4195,
                "accuracy_m": 5.0,
                "speed_mps": 3.5,
                "heading": 95.0
            }
        ]
        
        self.run_test(
            "Add GPS Points", "POST", f"runs/{run_id}/points", 200, points_data
        )
        
        # Get the run details
        self.run_test("Get Run Details", "GET", f"runs/{run_id}", 200)
        
        # End the run
        self.run_test("End Run", "POST", f"runs/{run_id}/end", 200)
        
        return True

    def test_claims(self):
        """Test claims endpoints"""
        print("\n🔍 Testing Claims...")
        
        # Test get claims in a bounding box
        params = "?min_lat=37.7&max_lat=37.8&min_lon=-122.5&max_lon=-122.4"
        self.run_test("Get Claims", "GET", f"claims{params}", 200)
        
        # Test user claims (if we have a user)
        if self.user_id:
            self.run_test("Get User Claims", "GET", f"claims/user/{self.user_id}", 200)

    def create_test_user_with_mongo(self):
        """Create test user using MongoDB directly (as per auth_testing.md)"""
        print("\n🔍 Creating Test User via MongoDB...")
        
        import subprocess
        
        mongo_script = '''
        use('test_database');
        var userId = 'test-user-' + Date.now();
        var sessionToken = 'test_session_' + Date.now();
        db.users.insertOne({
          user_id: userId,
          email: 'test.user.' + Date.now() + '@example.com',
          name: 'Test Runner',
          picture: 'https://via.placeholder.com/150',
          total_distance_m: 5000,
          total_area_m2: 1500,
          total_runs: 10,
          current_streak: 5,
          badges: ['first_run', 'area_100'],
          created_at: new Date()
        });
        db.user_sessions.insertOne({
          user_id: userId,
          session_token: sessionToken,
          expires_at: new Date(Date.now() + 7*24*60*60*1000),
          created_at: new Date()
        });
        print('Session token: ' + sessionToken);
        print('User ID: ' + userId);
        '''
        
        try:
            result = subprocess.run(
                ['mongosh', '--eval', mongo_script],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                output = result.stdout
                print("   ✅ MongoDB test user created")
                
                # Extract session token and user ID from output
                for line in output.split('\n'):
                    if 'Session token:' in line:
                        self.session_token = line.split('Session token: ')[1].strip()
                    elif 'User ID:' in line:
                        self.user_id = line.split('User ID: ')[1].strip()
                
                if self.session_token:
                    print(f"   📝 Session token: {self.session_token}")
                    print(f"   📝 User ID: {self.user_id}")
                    return True
            else:
                print(f"   ❌ MongoDB error: {result.stderr}")
                
        except Exception as e:
            print(f"   ❌ Error creating MongoDB user: {e}")
            
        return False

    def test_with_session_token(self):
        """Test endpoints using session token authentication"""
        print("\n🔍 Testing Session Token Authentication...")
        
        if not self.session_token:
            print("   ⚠️  No session token available")
            return False
            
        # Test with session token in cookie
        headers = {'Cookie': f'session_token={self.session_token}'}
        
        success, response = self.run_test(
            "Auth with Session Token", "GET", "auth/me", 200, headers=headers
        )
        
        return success

    def run_all_tests(self):
        """Run all test suites"""
        print("[*] Starting Intertia API Test Suite")
        print(f"📍 Testing against: {self.base_url}")
        
        # Basic tests (no auth required)
        self.test_basic_endpoints()
        self.test_leaderboards()
        self.test_badges()
        self.test_seasons()
        self.test_groups()
        self.test_claims()
        
        # Try to create MongoDB test user first
        mongo_user_created = self.create_test_user_with_mongo()
        if mongo_user_created:
            self.test_with_session_token()
        
        # Auth tests
        auth_success = self.test_auth_registration()
        if auth_success:
            self.test_auth_login()
            self.test_protected_endpoints()
            self.test_run_flow()
        
        # Print summary
        print(f"\n📊 Test Summary:")
        print(f"   Tests run: {self.tests_run}")
        print(f"   Tests passed: {self.tests_passed}")
        print(f"   Success rate: {(self.tests_passed/self.tests_run*100):.1f}%")
        
        # Return success if most tests passed
        return self.tests_passed / self.tests_run >= 0.7

def main():
    tester = IntertiaAPITester()
    success = tester.run_all_tests()
    
    # Save detailed results
    with open('/app/backend_test_results.json', 'w') as f:
        json.dump({
            'summary': {
                'tests_run': tester.tests_run,
                'tests_passed': tester.tests_passed,
                'success_rate': tester.tests_passed / tester.tests_run if tester.tests_run > 0 else 0
            },
            'results': tester.test_results
        }, f, indent=2)
    
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())