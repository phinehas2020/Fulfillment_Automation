#!/bin/bash
set -e

echo "=== Zebra ZP 505 CUPS/AirPrint Setup ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Install packages
echo "[1/8] Installing CUPS and Avahi..."
apt update
apt install -y cups cups-bsd cups-client cups-filters avahi-daemon avahi-utils

# Configure CUPS for network access
echo "[2/8] Configuring CUPS for network access..."
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
echo "[3/8] Adding gristmill user to lpadmin group..."
if id "gristmill" &>/dev/null; then
    usermod -a -G lpadmin gristmill
else
    echo "User 'gristmill' not found, skipping group add. You may need to add your specific user to lpadmin group."
fi

# Restart CUPS
echo "[4/8] Restarting CUPS..."
systemctl restart cups
systemctl enable cups

# Wait for CUPS to start
sleep 3

# Copy PPD if it exists in the same directory
SCRIPT_DIR=$(dirname "$0")
PPD_FILE="$SCRIPT_DIR/zebra-zp505.ppd"
if [ -f "$PPD_FILE" ]; then
    echo "[5/8] Installing PPD file..."
    mkdir -p /usr/share/cups/model/
    cp "$PPD_FILE" /usr/share/cups/model/zebra-zp505.ppd
else
    echo "[5/8] PPD file not found ($PPD_FILE), skipping custom PPD..."
fi

# Find Zebra printer
echo "[6/8] Detecting Zebra printer..."
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
echo "[7/8] Adding printer to CUPS..."
# Check if we should use the PPD
if [ -f "/usr/share/cups/model/zebra-zp505.ppd" ]; then
    lpadmin -p ZebraZP505 \
        -E \
        -v "$PRINTER_URI" \
        -P /usr/share/cups/model/zebra-zp505.ppd \
        -o printer-is-shared=true \
        -D "Zebra ZP 505 Label Printer" \
        -L "Warehouse"
else
    # Fallback to raw
    lpadmin -p ZebraZP505 \
        -E \
        -v "$PRINTER_URI" \
        -m raw \
        -o printer-is-shared=true \
        -D "Zebra ZP 505 Label Printer" \
        -L "Warehouse"
fi

# Enable printer
cupsenable ZebraZP505
cupsaccept ZebraZP505

# Set as default
lpadmin -d ZebraZP505

# Start Avahi
echo "[8/8] Starting Avahi for network discovery..."
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
