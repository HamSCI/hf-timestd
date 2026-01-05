#!/usr/bin/env python3
"""
Test script for solar-ionosphere correlation API endpoints.
Run this after starting the web-api service to verify functionality.
"""

import requests
import json
import sys

BASE_URL = "http://localhost:8000/api"

def test_endpoint(name, url, expected_keys=None):
    """Test a single API endpoint."""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"URL: {url}")
    print('-'*60)
    
    try:
        response = requests.get(url, timeout=10)
        print(f"Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"✓ Success")
            
            if expected_keys:
                missing = [k for k in expected_keys if k not in data]
                if missing:
                    print(f"⚠ Missing keys: {missing}")
                else:
                    print(f"✓ All expected keys present")
            
            # Print sample of data
            print(f"\nSample response:")
            print(json.dumps(data, indent=2)[:500] + "...")
            return True
        else:
            print(f"✗ Failed: {response.text[:200]}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"✗ Error: {e}")
        return False

def main():
    """Run all tests."""
    print("="*60)
    print("Solar-Ionosphere Correlation API Tests")
    print("="*60)
    
    tests = [
        # Space Weather Endpoints
        ("Current Space Weather", 
         f"{BASE_URL}/space-weather/current",
         ["timestamp", "xray", "kp", "alerts"]),
        
        ("X-ray Flux (24h)", 
         f"{BASE_URL}/space-weather/xray?hours=24",
         ["timestamps", "flux", "classes"]),
        
        ("Kp Index (24h)", 
         f"{BASE_URL}/space-weather/kp?hours=24",
         ["timestamps", "kp", "kp_index"]),
        
        ("Proton Flux (24h)", 
         f"{BASE_URL}/space-weather/protons?hours=24",
         ["timestamps", "flux"]),
        
        ("SID Events", 
         f"{BASE_URL}/space-weather/events/sid?hours=24",
         ["events", "count"]),
        
        ("Space Weather Summary", 
         f"{BASE_URL}/space-weather/summary?hours=24",
         ["timestamp", "current", "statistics"]),
        
        # Correlation Endpoints
        ("SNR-Solar Correlation", 
         f"{BASE_URL}/correlations/snr-solar?station=WWV&frequency=10&hours=24",
         None),  # May return error if no data
        
        ("SID Detection", 
         f"{BASE_URL}/correlations/sid-detection?hours=24",
         ["period", "sid_events_detected"]),
        
        ("Propagation-Kp Correlation", 
         f"{BASE_URL}/correlations/propagation-kp?hours=72",
         ["period", "kp_bins"]),
        
        ("Correlation Summary", 
         f"{BASE_URL}/correlations/summary?hours=24",
         ["timestamp", "period", "sid_events"]),
    ]
    
    results = []
    for name, url, keys in tests:
        success = test_endpoint(name, url, keys)
        results.append((name, success))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{status}: {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n⚠ {total - passed} test(s) failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
