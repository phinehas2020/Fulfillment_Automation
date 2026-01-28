# Plan: Convert Raspberry Pi into AirPrint/Network Print Server

## Overview

Transform the Raspberry Pi from a dedicated print agent into a **CUPS-based network print server** that exposes the Zebra ZP 505 label printer as an AirPrint/IPP network printer. This allows any device on the network to print directly to the label printer while maintaining compatibility with the existing Odoo print job system.

---

## Current State

- **Printer**: Zebra ZP 505 thermal label printer (4x6" labels, ZPL II format)
- **Connection**: USB direct to Raspberry Pi at `/dev/usb/lp0`
- **Current Method**: Print agent writes raw ZPL directly to USB device file
- **Problem**: Only the print agent can access the printer; employees cannot print custom labels

---

## Target State

- Printer appears as a standard network printer on the local network
- Any device (Windows, Mac, iOS, Android, Linux) can discover and print to it
- AirPrint support for iOS/macOS native printing
- IPP (Internet Printing Protocol) for cross-platform compatibility
- Existing Odoo print agent continues to work (via CUPS or direct)

---

## Implementation Tasks

### Phase 1: CUPS Installation & Configuration

#### Task 1.1: Install CUPS and Dependencies

**Location**: Raspberry Pi (SSH session)

```bash
# Update package lists
sudo apt update

# Install CUPS and related packages
sudo apt install -y cups cups-bsd cups-client cups-filters

# Install Avahi for network discovery (AirPrint/Bonjour)
sudo apt install -y avahi-daemon avahi-utils

# Install printer-driver-zebra for ZPL support (if available)
# Note: Zebra printers often work with raw queue - no driver needed
sudo apt install -y printer-driver-zpl || echo "ZPL driver not in repos, will use raw queue"
```

#### Task 1.2: Configure CUPS for Remote Administration

**File to modify**: `/etc/cups/cupsd.conf`

Make these changes to allow network access:

```diff
# Listen on all interfaces, not just localhost
- Listen localhost:631
+ Port 631

# Allow access from local network (adjust subnet as needed)
# In the <Location /> block:
  <Location />
    Order allow,deny
+   Allow @LOCAL
  </Location>

# In the <Location /admin> block:
  <Location /admin>
    Order allow,deny
+   Allow @LOCAL
  </Location>

# In the <Location /admin/conf> block:
  <Location /admin/conf>
    AuthType Default
    Require user @SYSTEM
    Order allow,deny
+   Allow @LOCAL
  </Location>

# Enable printer sharing
+ Browsing On
+ BrowseLocalProtocols dnssd
+ DefaultAuthType Basic
+ WebInterface Yes

# Share printers by default
+ DefaultShared Yes
```

#### Task 1.3: Add User to CUPS Admin Group

```bash
# Add the gristmill user to lpadmin group for printer administration
sudo usermod -a -G lpadmin gristmill

# Restart CUPS to apply changes
sudo systemctl restart cups
sudo systemctl enable cups
```

---

### Phase 2: Zebra Printer Setup in CUPS

#### Task 2.1: Identify USB Printer Device

```bash
# List USB devices to find Zebra printer
lsusb | grep -i zebra

# Check if printer is detected by CUPS
lpinfo -v | grep usb

# Expected output similar to:
# direct usb://Zebra%20Technologies/ZTC%20ZP%20505-XXX
```

#### Task 2.2: Add Printer to CUPS

**Option A: Via Command Line (Recommended for automation)**

```bash
# Get the exact USB URI from lpinfo -v
PRINTER_URI=$(lpinfo -v | grep -i zebra | head -1 | awk '{print $2}')

# Add the printer with raw queue (ZPL pass-through)
sudo lpadmin -p ZebraZP505 \
    -E \
    -v "$PRINTER_URI" \
    -m raw \
    -o printer-is-shared=true \
    -D "Zebra ZP 505 Label Printer" \
    -L "Warehouse"

# Set as default printer (optional)
sudo lpadmin -d ZebraZP505

# Enable the printer
sudo cupsenable ZebraZP505
sudo cupsaccept ZebraZP505
```

**Option B: Via Web Interface**

1. Access CUPS web interface: `https://<raspberry-pi-ip>:631`
2. Go to Administration > Add Printer
3. Select the Zebra USB device
4. Choose "Raw" as the driver (for ZPL pass-through)
5. Enable "Share This Printer"

#### Task 2.3: Create PPD File for Label Dimensions (Optional but Recommended)

**File to create**: `/usr/share/cups/model/zebra-zp505.ppd`

This PPD tells the system about the label size (4x6"):

```ppd
*PPD-Adobe: "4.3"
*FormatVersion: "4.3"
*FileVersion: "1.0"
*LanguageVersion: English
*LanguageEncoding: ISOLatin1
*PCFileName: "ZEBRAZP505.PPD"
*Manufacturer: "Zebra Technologies"
*Product: "(ZP 505)"
*ModelName: "Zebra ZP 505 Label Printer"
*ShortNickName: "Zebra ZP 505"
*NickName: "Zebra ZP 505 4x6 Label Printer"
*PSVersion: "(3010.000) 0"
*LanguageLevel: "3"
*ColorDevice: False
*DefaultColorSpace: Gray
*FileSystem: False
*Throughput: "1"
*LandscapeOrientation: Plus90
*TTRasterizer: Type42
*cupsFilter: "application/vnd.cups-raw 0 -"
*cupsLanguages: "en"

*% Paper sizes for 4x6 labels
*OpenUI *PageSize/Media Size: PickOne
*OrderDependency: 10 AnySetup *PageSize
*DefaultPageSize: 4x6Label
*PageSize 4x6Label/4x6 Shipping Label: "<</PageSize[288 432]/ImagingBBox null>>setpagedevice"
*CloseUI: *PageSize

*OpenUI *PageRegion: PickOne
*OrderDependency: 10 AnySetup *PageRegion
*DefaultPageRegion: 4x6Label
*PageRegion 4x6Label/4x6 Shipping Label: "<</PageSize[288 432]/ImagingBBox null>>setpagedevice"
*CloseUI: *PageRegion

*DefaultImageableArea: 4x6Label
*ImageableArea 4x6Label/4x6 Shipping Label: "0 0 288 432"

*DefaultPaperDimension: 4x6Label
*PaperDimension 4x6Label/4x6 Shipping Label: "288 432"

*% 288 points = 4 inches, 432 points = 6 inches (72 points per inch)
```

Then reinstall the printer with the PPD:

```bash
sudo lpadmin -p ZebraZP505 \
    -E \
    -v "$PRINTER_URI" \
    -P /usr/share/cups/model/zebra-zp505.ppd \
    -o printer-is-shared=true
```

---

### Phase 3: AirPrint / Network Discovery Setup

#### Task 3.1: Configure Avahi for AirPrint Discovery

Avahi (Bonjour/mDNS) enables automatic printer discovery. CUPS + Avahi should work together automatically, but verify:

```bash
# Ensure avahi-daemon is running
sudo systemctl enable avahi-daemon
sudo systemctl start avahi-daemon

# Verify CUPS is advertising via mDNS
avahi-browse -a | grep -i ipp
```

#### Task 3.2: Create Avahi Service File (If Auto-Discovery Fails)

**File to create**: `/etc/avahi/services/airprint-ZebraZP505.service`

```xml
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Zebra ZP 505 @ %h</name>
  <service>
    <type>_ipp._tcp</type>
    <subtype>_universal._sub._ipp._tcp</subtype>
    <port>631</port>
    <txt-record>txtvers=1</txt-record>
    <txt-record>qtotal=1</txt-record>
    <txt-record>rp=printers/ZebraZP505</txt-record>
    <txt-record>ty=Zebra ZP 505 Label Printer</txt-record>
    <txt-record>adminurl=https://%h:631/printers/ZebraZP505</txt-record>
    <txt-record>note=Warehouse Label Printer</txt-record>
    <txt-record>priority=0</txt-record>
    <txt-record>product=(Zebra ZP 505)</txt-record>
    <txt-record>pdl=application/pdf,image/jpeg,image/png,application/vnd.zebra-zpl</txt-record>
    <txt-record>Color=F</txt-record>
    <txt-record>Duplex=F</txt-record>
    <txt-record>Copies=T</txt-record>
    <txt-record>URF=none</txt-record>
  </service>
</service-group>
```

Then restart Avahi:

```bash
sudo systemctl restart avahi-daemon
```

#### Task 3.3: Verify Network Visibility

From another machine on the network:

```bash
# macOS/Linux
dns-sd -B _ipp._tcp

# Or using avahi-browse
avahi-browse -rt _ipp._tcp

# Windows (PowerShell)
# Printer should appear in Settings > Printers & Scanners
```

---

### Phase 4: Firewall Configuration

#### Task 4.1: Open Required Ports

```bash
# If using ufw (Ubuntu Firewall)
sudo ufw allow 631/tcp comment "CUPS IPP"
sudo ufw allow 5353/udp comment "mDNS/Bonjour"

# If using iptables directly
sudo iptables -A INPUT -p tcp --dport 631 -j ACCEPT
sudo iptables -A INPUT -p udp --dport 5353 -j ACCEPT

# Save iptables rules
sudo apt install iptables-persistent
sudo netfilter-persistent save
```

---

### Phase 5: Modify Print Agent (Optional - Two Options)

The existing print agent can coexist with CUPS in two ways:

#### Option A: Keep Direct USB (Simpler, No Changes)

The print agent continues to write directly to `/dev/usb/lp0`. This works because:
- CUPS doesn't lock the device exclusively
- Both can coexist if jobs don't overlap

**Risk**: Potential conflicts if both try to print simultaneously.

#### Option B: Route Print Agent Through CUPS (Recommended)

Modify the print agent to submit jobs to CUPS instead of direct USB writes.

**File to modify**: `print_agent/printer.py`

```python
# Current implementation (direct USB):
def print_zpl(self, zpl_data: str) -> bool:
    with open(self.device_path, "wb") as f:
        f.write(zpl_data.encode("utf-8"))
    return True

# New implementation (via CUPS):
import subprocess

def print_zpl(self, zpl_data: str) -> bool:
    """Print ZPL data via CUPS lpr command."""
    try:
        # Use lpr to submit to CUPS queue
        process = subprocess.run(
            ["lpr", "-P", "ZebraZP505", "-o", "raw"],
            input=zpl_data.encode("utf-8"),
            capture_output=True,
            timeout=30
        )
        if process.returncode != 0:
            self.logger.error(f"lpr failed: {process.stderr.decode()}")
            return False
        return True
    except subprocess.TimeoutExpired:
        self.logger.error("Print job timed out")
        return False
    except Exception as e:
        self.logger.error(f"Print failed: {e}")
        return False
```

**File to modify**: `print_agent/config.py`

```python
# Add new config option
CUPS_PRINTER_NAME = os.getenv("CUPS_PRINTER_NAME", "ZebraZP505")
USE_CUPS = os.getenv("USE_CUPS", "true").lower() == "true"
```

---

### Phase 6: Create Deployment Script

**File to create**: `print_agent/setup_cups.sh`

This script automates the entire setup process:

```bash
#!/bin/bash
set -e

echo "=== Zebra ZP 505 CUPS/AirPrint Setup ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Install packages
echo "[1/7] Installing CUPS and Avahi..."
apt update
apt install -y cups cups-bsd cups-client cups-filters avahi-daemon avahi-utils

# Configure CUPS for network access
echo "[2/7] Configuring CUPS for network access..."
cp /etc/cups/cupsd.conf /etc/cups/cupsd.conf.backup

# Use sed to modify cupsd.conf
sed -i 's/Listen localhost:631/Port 631/' /etc/cups/cupsd.conf
sed -i '/<Location \/>/,/<\/Location>/s/Order allow,deny/Order allow,deny\n  Allow @LOCAL/' /etc/cups/cupsd.conf
sed -i '/<Location \/admin>/,/<\/Location>/s/Order allow,deny/Order allow,deny\n  Allow @LOCAL/' /etc/cups/cupsd.conf

# Add sharing directives if not present
grep -q "^Browsing On" /etc/cups/cupsd.conf || echo "Browsing On" >> /etc/cups/cupsd.conf
grep -q "^BrowseLocalProtocols" /etc/cups/cupsd.conf || echo "BrowseLocalProtocols dnssd" >> /etc/cups/cupsd.conf
grep -q "^DefaultShared" /etc/cups/cupsd.conf || echo "DefaultShared Yes" >> /etc/cups/cupsd.conf
grep -q "^WebInterface" /etc/cups/cupsd.conf || echo "WebInterface Yes" >> /etc/cups/cupsd.conf

# Add user to lpadmin group
echo "[3/7] Adding gristmill user to lpadmin group..."
usermod -a -G lpadmin gristmill

# Restart CUPS
echo "[4/7] Restarting CUPS..."
systemctl restart cups
systemctl enable cups

# Wait for CUPS to start
sleep 3

# Find Zebra printer
echo "[5/7] Detecting Zebra printer..."
PRINTER_URI=$(lpinfo -v 2>/dev/null | grep -i zebra | head -1 | awk '{print $2}')

if [ -z "$PRINTER_URI" ]; then
    echo "ERROR: Zebra printer not detected!"
    echo "Make sure the printer is connected via USB and powered on."
    echo "Available devices:"
    lpinfo -v
    exit 1
fi

echo "Found printer at: $PRINTER_URI"

# Add printer to CUPS
echo "[6/7] Adding printer to CUPS..."
lpadmin -p ZebraZP505 \
    -E \
    -v "$PRINTER_URI" \
    -m raw \
    -o printer-is-shared=true \
    -D "Zebra ZP 505 Label Printer" \
    -L "Warehouse"

# Enable printer
cupsenable ZebraZP505
cupsaccept ZebraZP505

# Set as default
lpadmin -d ZebraZP505

# Start Avahi
echo "[7/7] Starting Avahi for network discovery..."
systemctl enable avahi-daemon
systemctl start avahi-daemon

echo ""
echo "=== Setup Complete ==="
echo ""
echo "The Zebra ZP 505 should now be visible as 'Zebra ZP 505 Label Printer'"
echo "on all devices on your local network."
echo ""
echo "To verify:"
echo "  - CUPS Web Interface: https://$(hostname -I | awk '{print $1}'):631"
echo "  - Test print: echo '^XA^FO50,50^A0N,50,50^FDTest Label^FS^XZ' | lpr -P ZebraZP505 -o raw"
echo ""
echo "On macOS/iOS: The printer should appear automatically."
echo "On Windows: Add printer > Search for printers on the network."
```

Make executable:
```bash
chmod +x print_agent/setup_cups.sh
```

---

### Phase 7: Testing & Verification

#### Task 7.1: Create Test Script

**File to create**: `print_agent/test_cups_print.py`

```python
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
    result = subprocess.run(["lpstat", "-p", "ZebraZP505"], capture_output=True, text=True)
    print(result.stdout or result.stderr)

    print("\n=== Print Queue ===")
    result = subprocess.run(["lpstat", "-o", "ZebraZP505"], capture_output=True, text=True)
    print(result.stdout or "Queue is empty")

if __name__ == "__main__":
    check_printer_status()
    print("\n" + "="*50 + "\n")
    test_cups_print()
```

#### Task 7.2: Verification Checklist

1. **CUPS Web Interface**
   - Access `https://<raspberry-pi-ip>:631`
   - Verify printer appears in "Printers" tab
   - Status should show "Idle" or "Ready"

2. **Network Discovery**
   - On macOS: Open System Preferences > Printers, printer should auto-appear
   - On iOS: Any app with print option should show the printer
   - On Windows: Settings > Printers > Add Printer > Search

3. **Test Print from Different Devices**
   - Print test page from CUPS web interface
   - Print from macOS/iOS device
   - Print from Windows device

4. **Verify Existing Print Agent Still Works**
   - Restart print agent service
   - Create test print job in Odoo
   - Confirm label prints successfully

---

### Phase 8: Troubleshooting Guide

**File to create**: `print_agent/CUPS_TROUBLESHOOTING.md`

```markdown
# CUPS/AirPrint Troubleshooting Guide

## Printer Not Showing on Network

1. Check CUPS is running:
   ```bash
   sudo systemctl status cups
   ```

2. Check Avahi is running:
   ```bash
   sudo systemctl status avahi-daemon
   ```

3. Verify mDNS is advertising:
   ```bash
   avahi-browse -rt _ipp._tcp
   ```

4. Check firewall:
   ```bash
   sudo ufw status
   # Ports 631/tcp and 5353/udp should be open
   ```

## Printer Shows But Won't Print

1. Check CUPS error log:
   ```bash
   sudo tail -f /var/log/cups/error_log
   ```

2. Check printer status:
   ```bash
   lpstat -p ZebraZP505
   ```

3. Check print queue:
   ```bash
   lpstat -o
   ```

4. Clear stuck jobs:
   ```bash
   cancel -a ZebraZP505
   ```

## USB Device Not Detected

1. Check USB connection:
   ```bash
   lsusb | grep -i zebra
   ```

2. Check device permissions:
   ```bash
   ls -la /dev/usb/lp0
   ```

3. Add udev rule for Zebra printers:
   ```bash
   echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0a5f", MODE="0666"' | \
       sudo tee /etc/udev/rules.d/99-zebra.rules
   sudo udevadm control --reload-rules
   sudo udevadm trigger
   ```

## Common CUPS Commands

```bash
# List printers
lpstat -p

# Print test page
lpr -P ZebraZP505 -o raw /path/to/test.zpl

# Enable disabled printer
sudo cupsenable ZebraZP505

# Accept jobs on printer
sudo cupsaccept ZebraZP505

# Remove and re-add printer
sudo lpadmin -x ZebraZP505
# Then re-run setup script
```
```

---

## File Summary

| File | Action | Description |
|------|--------|-------------|
| `/etc/cups/cupsd.conf` | Modify | Enable network access and sharing |
| `/usr/share/cups/model/zebra-zp505.ppd` | Create | Optional PPD for label dimensions |
| `/etc/avahi/services/airprint-ZebraZP505.service` | Create | Optional Avahi service file |
| `print_agent/setup_cups.sh` | Create | Automated setup script |
| `print_agent/printer.py` | Modify (Optional) | Route prints through CUPS |
| `print_agent/config.py` | Modify (Optional) | Add CUPS config options |
| `print_agent/test_cups_print.py` | Create | Test CUPS printing |
| `print_agent/CUPS_TROUBLESHOOTING.md` | Create | Troubleshooting documentation |

---

## Execution Order

1. SSH into Raspberry Pi
2. Run `setup_cups.sh` script
3. Verify printer appears in CUPS web interface
4. Test print from Raspberry Pi command line
5. Test print from another device on the network
6. (Optional) Modify print agent to use CUPS
7. Verify Odoo print jobs still work

---

## Notes

- The Zebra ZP 505 uses ZPL (Zebra Programming Language), which is a raw text format
- Using "raw" queue in CUPS passes data directly to printer without filtering
- AirPrint normally expects raster data, but many apps can send raw data
- For full AirPrint compatibility with image printing, you may need a filter that converts images to ZPL
- The existing print agent sends ZPL directly, so it will work with the raw queue

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Print job conflicts between agent and CUPS | Route agent through CUPS (Phase 5, Option B) |
| CUPS locks USB device | Test both systems simultaneously before deployment |
| Network security exposure | CUPS only allows @LOCAL by default |
| Breaking existing functionality | Test thoroughly before modifying print_agent code |
