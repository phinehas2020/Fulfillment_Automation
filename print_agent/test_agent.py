#!/usr/bin/env python3
"""
Test version of print agent - simulates printing to console.
Use this to verify Odoo integration without a real printer.
"""

import time
import sys
import os

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import ODOO_API_KEY, ODOO_URL, POLL_INTERVAL, PRINTER_ID
from odoo_client import OdooClient


def main():
    client = OdooClient(base_url=ODOO_URL, api_key=ODOO_API_KEY, printer_id=PRINTER_ID)

    print("=" * 60)
    print("🖨️  TEST PRINT AGENT (Console Mode)")
    print("=" * 60)
    print(f"Printer ID: {PRINTER_ID}")
    print(f"Odoo URL:   {ODOO_URL}")
    print(f"Poll Interval: {POLL_INTERVAL}s")
    print("=" * 60)
    print("\nPress Ctrl+C to stop\n")

    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Polling for jobs...")
            jobs = client.fetch_pending_jobs()
            
            if not jobs:
                print(f"[{time.strftime('%H:%M:%S')}] No pending jobs.")
            else:
                print(f"\n✅ Found {len(jobs)} job(s)!\n")
                
                for job in jobs:
                    job_id = job.get("id")
                    job_type = job.get("job_type", "unknown")
                    zpl_data = job.get("zpl_data", "")
                    
                    print("-" * 60)
                    print(f"📄 Job ID: {job_id}")
                    print(f"   Type: {job_type}")
                    print(f"   ZPL Length: {len(zpl_data)} bytes")
                    
                    if zpl_data:
                        # Show first 200 chars of ZPL
                        preview = zpl_data[:200] + "..." if len(zpl_data) > 200 else zpl_data
                        print(f"   ZPL Preview:\n{preview}")
                    else:
                        print("   ⚠️  No ZPL data!")
                    
                    # Simulate successful print
                    print(f"\n   🖨️  Simulating print...")
                    time.sleep(1)  # Small delay to simulate print time
                    
                    # Mark job as complete
                    result = client.mark_complete(job_id=job_id, success=True)
                    if result.get("status") == "ok":
                        print(f"   ✅ Job {job_id} marked complete!")
                    else:
                        print(f"   ❌ Failed to mark complete: {result}")
                    
                    print("-" * 60)
                
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Agent stopped.")
