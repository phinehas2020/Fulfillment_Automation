#!/usr/bin/env python3
"""
Standalone test script for Shippo API integration.
Tests the API with dummy data to verify the connection works.
"""

import requests
import json

# ============================================================
# CONFIGURATION - Update with your Shippo test API key
# ============================================================
SHIPPO_API_KEY = "shippo_test_XXXXXXXXX"  # <-- Replace with your test key

# ============================================================
# Test Addresses
# ============================================================
ADDRESS_FROM = {
    "name": "Homestead Grist Mill",
    "street1": "123 Main Street",
    "city": "Waco",
    "state": "TX",
    "zip": "76701",
    "country": "US",
    "phone": "555-123-4567",
    "email": "shipping@example.com",
}

ADDRESS_TO = {
    "name": "John Doe",
    "street1": "456 Oak Avenue",
    "city": "Austin",
    "state": "TX",
    "zip": "78701",
    "country": "US",
    "phone": "555-987-6543",
    "email": "customer@example.com",
}

# Test parcel (Small Box from your default_boxes.xml)
PARCEL = {
    "length": 8,
    "width": 6,
    "height": 4,
    "distance_unit": "in",
    "weight": 500,  # 500 grams
    "mass_unit": "g",
}


def test_shippo_connection():
    """Test basic API connectivity."""
    print("=" * 60)
    print("Testing Shippo API Connection...")
    print("=" * 60)
    
    url = "https://api.goshippo.com/addresses/"
    headers = {
        "Authorization": f"ShippoToken {SHIPPO_API_KEY}",
        "Content-Type": "application/json",
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            print("✅ API Connection successful!")
            return True
        elif resp.status_code == 401:
            print("❌ Authentication failed. Check your API key.")
            print(f"   Response: {resp.text}")
            return False
        else:
            print(f"❌ Unexpected status: {resp.status_code}")
            print(f"   Response: {resp.text}")
            return False
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return False


def test_get_rates():
    """Test fetching shipping rates."""
    print("\n" + "=" * 60)
    print("Testing Rate Shopping...")
    print("=" * 60)
    
    url = "https://api.goshippo.com/shipments"
    headers = {
        "Authorization": f"ShippoToken {SHIPPO_API_KEY}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "address_from": ADDRESS_FROM,
        "address_to": ADDRESS_TO,
        "parcels": [PARCEL],
        "async": False,
    }
    
    print(f"\n📦 Parcel: {PARCEL['length']}x{PARCEL['width']}x{PARCEL['height']} in, {PARCEL['weight']}g")
    print(f"📍 From: {ADDRESS_FROM['city']}, {ADDRESS_FROM['state']}")
    print(f"📍 To: {ADDRESS_TO['city']}, {ADDRESS_TO['state']}")
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        
        if resp.status_code >= 400:
            print(f"❌ Error: {resp.status_code}")
            print(f"   Response: {resp.text}")
            return None
        
        data = resp.json()
        rates = data.get("rates", [])
        
        if not rates:
            print("⚠️  No rates returned. Checking for messages...")
            messages = data.get("messages", [])
            if messages:
                for msg in messages:
                    print(f"   - {msg.get('text', msg)}")
            return None
        
        print(f"\n✅ Got {len(rates)} shipping rates!\n")
        print("-" * 60)
        
        # Sort by price
        sorted_rates = sorted(rates, key=lambda r: float(r.get("amount", 999999)))
        
        for i, rate in enumerate(sorted_rates[:5], 1):  # Show top 5 cheapest
            carrier = rate.get("provider", "Unknown")
            service = rate.get("servicelevel", {}).get("name", "Unknown")
            amount = rate.get("amount", "N/A")
            currency = rate.get("currency", "USD")
            days = rate.get("estimated_days", "?")
            
            print(f"{i}. {carrier} - {service}")
            print(f"   Price: ${amount} {currency} | Est. {days} day(s)")
            print()
        
        return sorted_rates[0]  # Return cheapest for label test
        
    except Exception as e:
        print(f"❌ Request failed: {e}")
        return None


def test_purchase_label(rate):
    """Test purchasing a label (creates a TEST label)."""
    print("\n" + "=" * 60)
    print("Testing Label Purchase (TEST MODE)...")
    print("=" * 60)
    
    if not rate:
        print("⚠️  Skipping - no rate provided")
        return
    
    url = "https://api.goshippo.com/transactions"
    headers = {
        "Authorization": f"ShippoToken {SHIPPO_API_KEY}",
        "Content-Type": "application/json",
    }
    
    rate_id = rate.get("object_id")
    carrier = rate.get("provider")
    service = rate.get("servicelevel", {}).get("name")
    
    print(f"\n🏷️  Purchasing label for: {carrier} - {service}")
    print(f"   Rate ID: {rate_id}")
    
    payload = {
        "rate": rate_id,
        "label_file_type": "ZPLII",  # Request ZPL for thermal printer
        "async": False,
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        
        if resp.status_code >= 400:
            print(f"❌ Error: {resp.status_code}")
            print(f"   Response: {resp.text}")
            return
        
        data = resp.json()
        status = data.get("status")
        
        if status == "SUCCESS":
            print("\n✅ Label purchased successfully!")
            print(f"   Tracking #: {data.get('tracking_number')}")
            print(f"   Label URL: {data.get('label_url')}")
            print(f"   Tracking URL: {data.get('tracking_url_provider')}")
            
            # Check if ZPL was returned
            if data.get("label_file_type") == "ZPLII":
                print("   ✅ ZPL format confirmed (ready for thermal printer)")
        else:
            print(f"❌ Label purchase status: {status}")
            messages = data.get("messages", [])
            for msg in messages:
                print(f"   - {msg.get('text', msg)}")
                
    except Exception as e:
        print(f"❌ Request failed: {e}")


def main():
    print("\n" + "=" * 60)
    print("🚀 SHIPPO API TEST SCRIPT")
    print("=" * 60)
    
    if "XXXXXXXXX" in SHIPPO_API_KEY:
        print("\n⚠️  Please update SHIPPO_API_KEY in this script with your test key!")
        print("   Edit: test_shippo.py line 13")
        return
    
    # Test 1: Connection
    if not test_shippo_connection():
        return
    
    # Test 2: Get Rates
    cheapest_rate = test_get_rates()
    
    # Test 3: Purchase Label (only if rates succeeded)
    if cheapest_rate:
        print("\n" + "-" * 60)
        confirm = input("Do you want to test label purchase? (y/n): ").strip().lower()
        if confirm == 'y':
            test_purchase_label(cheapest_rate)
        else:
            print("Skipping label purchase test.")
    
    print("\n" + "=" * 60)
    print("🏁 Test Complete!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
