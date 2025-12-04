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
            raise PrinterError(f"Failed to write to printer: {exc}") from exc


