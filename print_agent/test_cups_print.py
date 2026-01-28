#!/usr/bin/env python3
"""Test CUPS printing functionality."""

import subprocess
import sys

# Simple ZPL test label
TEST_ZPL = """^XA
^FO50,50^A0N,40,40^FDCUPS Test Label^FS
^FO50,100^A0N,30,30^FDPrinted via AirPrint^FS
^FO50,150^BY2^BCN,80,Y,N,N^FD123456789^FS
^XZ"""

def test_cups_print():
    """Submit test label to CUPS queue."""
    print("Submitting test label to CUPS queue 'ZebraZP505'...")

    try:
        result = subprocess.run(
            ["lpr", "-P", "ZebraZP505", "-o", "raw"],
            input=TEST_ZPL.encode("utf-8"),
            capture_output=True,
            timeout=30
        )

        if result.returncode == 0:
            print("SUCCESS: Test label submitted to print queue")
            print("\nCheck the printer for output.")
        else:
            print(f"FAILED: {result.stderr.decode()}")
            sys.exit(1)

    except FileNotFoundError:
        print("ERROR: 'lpr' command not found. Is CUPS installed?")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("ERROR: Print job timed out")
        sys.exit(1)

def check_printer_status():
    """Check CUPS printer status."""
    print("\n=== Printer Status ===")
    try:
        result = subprocess.run(["lpstat", "-p", "ZebraZP505"], capture_output=True, text=True)
        print(result.stdout or result.stderr)
    except FileNotFoundError:
        print("ERROR: lpstat command not found.")

    print("\n=== Print Queue ===")
    try:
        result = subprocess.run(["lpstat", "-o", "ZebraZP505"], capture_output=True, text=True)
        print(result.stdout or "Queue is empty")
    except FileNotFoundError:
        pass

if __name__ == "__main__":
    check_printer_status()
    print("\n" + "="*50 + "\n")
    test_cups_print()
