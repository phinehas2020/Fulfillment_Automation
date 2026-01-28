"""Lightweight printer wrapper (skeleton)."""

from config import PRINTER_PATH, USE_CUPS, CUPS_PRINTER_NAME


class PrinterError(Exception):
    pass


class Printer:
    def __init__(self, device_path: str = PRINTER_PATH):
        self.device_path = device_path

    def send_zpl(self, zpl_data: str):
        if USE_CUPS:
            # CUPS Printing Logic
            import subprocess
            try:
                # Use lpr to submit to CUPS queue
                process = subprocess.run(
                    ["lpr", "-P", CUPS_PRINTER_NAME, "-o", "raw"],
                    input=zpl_data.encode("utf-8"),
                    capture_output=True,
                    timeout=30
                )
                if process.returncode != 0:
                    raise PrinterError(f"lpr failed: {process.stderr.decode()}")
                return True
            except FileNotFoundError:
                raise PrinterError("lpr command not found. Is CUPS installed? Set USE_CUPS=false to failover to direct USB.")
            except subprocess.TimeoutExpired:
                raise PrinterError("Print job timed out")
            except Exception as e:
                raise PrinterError(f"Print failed: {e}")
        else:
            # Direct USB Printing Logic
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


