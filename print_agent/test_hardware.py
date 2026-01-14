#!/usr/bin/env python3
import sys
import os
from printer import Printer, PrinterError

def test_hardware_print():
    # Use the default path from config or /dev/usb/lp0
    device_path = os.getenv("PRINTER_PATH", "/dev/usb/lp0")
    
    print(f"--- Zebra Printer Hardware Test ---")
    print(f"Target Device: {device_path}")
    
    if not os.path.exists(device_path):
        print(f"❌ Error: {device_path} not found. Check connections.")
        return

    # Simple ZPL for a 4x6 label with a box and text
    zpl_test = """
    ^XA
    ^FX Test Label ^FS
    ^CFA,30
    ^FO50,50^GB700,1100,3^FS
    ^FO100,100^FDODOO PRINT AGENT^FS
    ^FO100,150^FDStatus: CONNECTED^FS
    ^FO100,200^FDDevice: /dev/usb/lp0^FS
    ^FO100,300^GB600,1,3^FS
    ^XZ
    """
    
    printer = Printer(device_path=device_path)
    try:
        print("Sending ZPL to printer...")
        printer.send_zpl(zpl_test)
        print("✅ Print command sent successfully!")
        print("Check the printer for a 'Test Label'.")
    except PrinterError as e:
        print(f"❌ Failed to print: {e}")
    except PermissionError:
        print(f"❌ Permission Denied! Try running with sudo or add user to 'lp' group:")
        print(f"   sudo usermod -a -G lp $USER")

if __name__ == "__main__":
    test_hardware_print()
