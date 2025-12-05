"""Lightweight printer wrapper (skeleton)."""

from config import PRINTER_PATH


class PrinterError(Exception):
    pass


class Printer:
    def __init__(self, device_path: str = PRINTER_PATH):
        self.device_path = device_path

    def send_zpl(self, zpl_data: str):
        try:
            with open(self.device_path, "wb") as printer:
                printer.write(zpl_data.encode("utf-8"))
            return True
        except OSError as exc:
            import os
            error_msg = f"Failed to write to printer at {self.device_path}: {exc}"
            
            # Help debug by listing available usb printers
            if os.path.exists("/dev/usb"):
                devices = os.listdir("/dev/usb")
                error_msg += f"\nAvailable devices in /dev/usb/: {devices}"
            else:
                error_msg += "\n/dev/usb/ directory does not exist. Is the printer connected?"
                
            raise PrinterError(error_msg) from exc


