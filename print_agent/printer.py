"""Lightweight printer wrapper (skeleton)."""

from config import PRINTER_PATH


class PrinterError(Exception):
    pass


class Printer:
    def __init__(self, device_path: str = PRINTER_PATH):
        self.device_path = device_path

    def send_zpl(self, zpl_data: str):
        # TODO: handle USB I/O and retries
        _ = (zpl_data, self.device_path)
        return True


