"""Lightweight printer wrapper (skeleton)."""

import os
import subprocess
import tempfile

from config import PRINTER_PATH, USE_CUPS, CUPS_PRINTER_NAME


class PrinterError(Exception):
    pass


class Printer:
    def __init__(self, device_path: str = PRINTER_PATH):
        self.device_path = device_path

    def send_zpl(self, zpl_data: str):
        if USE_CUPS:
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

    def send_pdf(self, pdf_data: str):
        if not USE_CUPS:
            raise PrinterError("PDF printing requires USE_CUPS=true")

        file_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", suffix=".pdf", delete=False) as temp_file:
                payload = pdf_data
                if isinstance(payload, str):
                    payload = payload.encode("latin1", errors="ignore")
                temp_file.write(payload)
                file_path = temp_file.name

            # Convert PDF to ZPL first to guarantee Zebra-compatible output
            # regardless of CUPS queue/PPD state.
            zpl_output = self._convert_pdf_to_zpl(file_path)
            self.send_zpl(zpl_output)
            return True
        except FileNotFoundError:
            raise PrinterError("Required printing command not found. Is CUPS installed?")
        except subprocess.TimeoutExpired:
            raise PrinterError("PDF print job timed out")
        except Exception as exc:
            raise PrinterError(f"PDF print failed: {exc}")
        finally:
            if file_path:
                try:
                    os.unlink(file_path)
                except OSError:
                    pass

    def _convert_pdf_to_zpl(self, pdf_path: str) -> str:
        script_path = os.path.join(os.path.dirname(__file__), "pdftozpl")
        if not os.path.exists(script_path):
            raise PrinterError(f"pdftozpl script not found at {script_path}")

        env = os.environ.copy()
        # Letter-sized PDFs from carriers often contain large whitespace margins.
        # Force fit to 4x6 and enable auto invert for thermal visibility.
        env.setdefault("ZPL_FORCE_FIT", "1")
        env.setdefault("ZPL_AUTO_INVERT", "1")
        env.setdefault("ZPL_SCALE_TO_LABEL", "1")
        env.setdefault("ZPL_WIDTH_INCH", "4")
        env.setdefault("ZPL_HEIGHT_INCH", "6")
        env.setdefault("ZPL_DPI", "203")
        env.setdefault("ZPL_AUTO_CROP_CONTENT", "1")
        env.setdefault("ZPL_ROTATE_TO_FIT", "1")
        env.setdefault("ZPL_CONTENT_ZOOM", "1")
        env.setdefault("ZPL_CONTENT_PAD_PX", "6")

        process = subprocess.run(
            [script_path, "1", "agent", "label", "1", "", pdf_path],
            capture_output=True,
            timeout=60,
            env=env,
        )
        stderr = process.stderr.decode("utf-8", errors="ignore")
        if process.returncode != 0:
            raise PrinterError(f"pdftozpl failed: {stderr}")

        if stderr.strip():
            print(f"pdftozpl: {stderr.strip()}")

        output_bytes = process.stdout or b""
        if not output_bytes:
            raise PrinterError("pdftozpl produced empty output")

        return output_bytes.decode("utf-8", errors="ignore")
