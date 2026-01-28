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
