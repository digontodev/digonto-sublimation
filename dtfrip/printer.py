"""Talking to the printer: find it, send jobs, read ink levels, maintenance."""
import glob
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config
from .escp2 import (cmd_align_print, cmd_align_save, cmd_align_set, cmd_head_clean,
                    cmd_nozzle_check)


class PrinterError(Exception):
    pass


WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"
CANCEL_SEQ = b"\x0c\x1b@\x1b@\x1b(R\x08\x00\x00REMOTE1JE\x01\x00\x00\x1b\x00\x00\x00"


# ------------------------------------------------------------------ finding the printer
# A "device" is a string:  /dev/usb/lpN (Linux, direct USB)  |  cups:QUEUE (macOS / Linux)  |  win:PRINTER NAME (Windows)
def _usb_devices():
    out = []
    for dev in sorted(glob.glob("/dev/usb/lp*")):
        name = os.path.basename(dev)
        ident = ""
        for p in (f"/sys/class/usbmisc/{name}/device/ieee1284_id", f"/sys/class/usb/{name}/device/ieee1284_id"):
            try:
                ident = Path(p).read_text(errors="replace").strip(); break
            except OSError:
                pass
        out.append((dev, ident))
    return out


def _cups_queues():
    exe = shutil.which("lpstat")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-v"], capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        m = re.match(r"device for (.+?):\s*(.*)$", line.strip())
        if m:
            out.append((f"cups:{m.group(1)}", f"{m.group(1)}  {m.group(2)}"))
    return out


def _win_printers():
    if not WIN:
        return []
    import ctypes
    from ctypes import wintypes
    PRINTER_ENUM_LOCAL, PRINTER_ENUM_CONNECTIONS = 2, 4

    class PI4(ctypes.Structure):
        _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pServerName", wintypes.LPWSTR), ("Attributes", wintypes.DWORD)]
    need, num = wintypes.DWORD(0), wintypes.DWORD(0)
    ws = ctypes.windll.winspool.EnumPrintersW
    flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    ws(flags, None, 4, None, 0, ctypes.byref(need), ctypes.byref(num))
    if not need.value:
        return []
    buf = ctypes.create_string_buffer(need.value)
    if not ws(flags, None, 4, buf, need, ctypes.byref(need), ctypes.byref(num)):
        return []
    arr = ctypes.cast(buf, ctypes.POINTER(PI4))
    return [(f"win:{arr[i].pPrinterName}", arr[i].pPrinterName) for i in range(num.value)]


def list_devices():
    """[(device, description)] of everything the printer could be."""
    if WIN:
        return _win_printers()
    return _usb_devices() + _cups_queues()


def _is_epson(text):
    t = text.upper()
    return any(m in t for m in ("L800", "L805", "L1800"))     # the same print head (Gutenprint data)


def find_device():
    cfg = config.load()
    want = cfg.get("device")
    if want:
        if want.startswith(("cups:", "win:")) or os.path.exists(want):
            return want
    devs = list_devices()
    for dev, ident in devs:
        if dev.startswith("/dev/") and "EPSON" in ident.upper() and _is_epson(ident):
            return dev
    for dev, ident in devs:
        if _is_epson(ident) or _is_epson(dev):
            return dev
    for dev, ident in devs:
        if dev.startswith("/dev/") and "EPSON" in ident.upper():
            return dev
    for dev, ident in devs:
        if "EPSON" in (ident + dev).upper():
            return dev
    return devs[0][0] if devs and devs[0][0].startswith("/dev/") else None


def is_writable(dev):
    if not dev:
        return False
    if dev.startswith("/dev/"):
        return os.access(dev, os.W_OK)
    return True


def device_or_raise():
    dev = find_device()
    if not dev:
        if WIN:
            raise PrinterError("Printer not found. Install the Epson L800 driver, switch the printer on, and pick it under Settings > Printer.")
        if MAC:
            raise PrinterError("Printer not found. Add the Epson L800 in System Settings > Printers & Scanners, "
                               "switch it on, and pick it under Settings > Printer.")
        raise PrinterError("Printer not found. Is it switched on and connected by USB?")
    if not is_writable(dev):
        raise PrinterError(f"No permission to write {dev}. Add your user to the 'lp' group "
                           f"(sudo usermod -aG lp $USER) and log in again.")
    return dev


# ------------------------------------------------------------------ writers (raw bytes, no driver processing)
class _UsbWriter:
    def __init__(self, dev, name):
        self.f = open(dev, "wb", buffering=0)

    def write(self, b):
        self.f.write(b)

    def close(self, ok=True):
        self.f.close()


class _CupsWriter:
    def __init__(self, dev, name):
        q = dev[5:]
        exe = shutil.which("lp")
        if not exe:
            raise PrinterError("The 'lp' command is missing (CUPS).")
        self.p = subprocess.Popen([exe, "-d", q, "-o", "raw", "-t", name[:60] or "DTF job"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, b):
        try:
            self.p.stdin.write(b)
        except BrokenPipeError:
            raise OSError(self.p.stderr.read().decode(errors="replace") or "lp stopped")

    def close(self, ok=True):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        rc = self.p.wait(timeout=600)
        if rc != 0 and ok:
            raise OSError(self.p.stderr.read().decode(errors="replace").strip() or f"lp failed ({rc})")


class _WinWriter:
    """Windows print spooler, datatype RAW: the bytes reach the printer port untouched (the driver does not render them)."""

    def __init__(self, dev, name):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        self.lib = ctypes.windll.winspool

        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]
        self.h = wintypes.HANDLE()
        if not self.lib.OpenPrinterW(dev[4:], ctypes.byref(self.h), None):
            raise OSError(f"Cannot open printer '{dev[4:]}' (Windows error {ctypes.GetLastError()}).")
        di = DOC_INFO_1(name[:60] or "DTF job", None, "RAW")
        if not self.lib.StartDocPrinterW(self.h, 1, ctypes.byref(di)):
            err = ctypes.GetLastError()
            self.lib.ClosePrinter(self.h)
            raise OSError(f"Windows refused the print job (error {err}). Is the printer switched on and not paused?")
        self.lib.StartPagePrinter(self.h)

    def write(self, b):
        n = self.ct.c_ulong(0)
        buf = self.ct.create_string_buffer(bytes(b), len(b))
        if not self.lib.WritePrinter(self.h, buf, len(b), self.ct.byref(n)) or n.value != len(b):
            raise OSError(f"WritePrinter failed (error {self.ct.GetLastError()})")

    def close(self, ok=True):
        self.lib.EndPagePrinter(self.h)
        self.lib.EndDocPrinter(self.h)
        self.lib.ClosePrinter(self.h)


def open_writer(dev, name="DTF job"):
    if dev.startswith("cups:"):
        return _CupsWriter(dev, name)
    if dev.startswith("win:"):
        return _WinWriter(dev, name)
    return _UsbWriter(dev, name)


class Sender:
    """Streams a print file to the printer in a background thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.state = {"busy": False, "name": "", "sent": 0, "total": 0, "error": "",
                      "started": 0, "finished": 0}
        self._cancel = False

    def busy(self):
        return self.state["busy"]

    def send_bytes(self, data: bytes, name="command", wait=True):
        dev = device_or_raise()
        if self.busy():
            raise PrinterError("The printer is busy with a job.")
        try:
            w = open_writer(dev, name)
            try:
                w.write(data)
            finally:
                w.close()
        except OSError as e:
            raise PrinterError(f"printer error: {e}")
        return True

    def send_file(self, path, name, on_done=None):
        dev = device_or_raise()
        with self.lock:
            if self.busy():
                raise PrinterError("The printer is busy with another job.")
            total = os.path.getsize(path)
            self._cancel = False
            self.state.update(busy=True, name=name, sent=0, total=total, error="",
                              started=time.time(), finished=0)
        self.thread = threading.Thread(target=self._run, args=(dev, path, name, on_done), daemon=True)
        self.thread.start()

    def cancel(self):
        self._cancel = True

    def _run(self, dev, path, name, on_done):
        err = ""
        try:
            w = open_writer(dev, name)
            try:
                with open(path, "rb") as src:
                    while True:
                        if self._cancel:
                            # finish the page cleanly so the printer does not hang
                            w.write(CANCEL_SEQ)
                            err = "cancelled"
                            break
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        w.write(chunk)
                        self.state["sent"] += len(chunk)
            finally:
                w.close(ok=not err)
        except (OSError, subprocess.TimeoutExpired, PrinterError) as e:
            err = f"printer error: {e}"
        finally:
            self.state.update(busy=False, error=err, finished=time.time())
            if on_done:
                try:
                    on_done(err)
                except Exception:
                    pass


SENDER = Sender()


# --------------------------------------------------------------------- status
def ink_levels():
    """Ink counters as reported by the printer: {'Yellow': 64, ...} or raises."""
    dev = device_or_raise()
    if not dev.startswith("/dev/"):
        raise PrinterError("Ink levels can only be read over a direct USB connection on Linux. "
                           "On this system watch the lights on the printer and the ink tanks.")
    exe = shutil.which("escputil")
    if not exe:
        raise PrinterError("Install escputil to read ink levels (sudo apt install escputil)")
    if SENDER.busy():
        raise PrinterError("Busy printing - try again after the job.")
    r = subprocess.run([exe, "-r", dev, "-i", "-q"], capture_output=True, text=True, timeout=30)
    levels = {}
    for line in r.stdout.splitlines():
        m = re.match(r"\s*([A-Za-z ]+?)\s+(\d+)\s*$", line)
        if m and m.group(1).strip() not in ("Ink color", "Percent remaining"):
            levels[m.group(1).strip()] = int(m.group(2))
    if not levels:
        raise PrinterError((r.stderr or r.stdout or "no answer from printer").strip()[-300:])
    # name them after what is really in the tank
    names = {"Light Cyan": "White 1", "Light Magenta": "White 2"}
    return {names.get(k, k): v for k, v in levels.items()}


def status():
    dev = find_device()
    ident = dict(list_devices()).get(dev, "") if dev else ""
    return {"device": dev, "id": ident, "connected": bool(dev),
            "writable": is_writable(dev), "job": dict(SENDER.state)}


# --------------------------------------------------------------------- maintenance
def nozzle_check():
    SENDER.send_bytes(cmd_nozzle_check(), "nozzle check")


def head_clean():
    SENDER.send_bytes(cmd_head_clean(power=False), "head cleaning")


def power_clean():
    SENDER.send_bytes(cmd_head_clean(power=True), "power cleaning")


def align_print():
    SENDER.send_bytes(cmd_align_print(4), "alignment pattern")


def align_apply(choices, save=True):
    if len(choices) != 4 or not all(1 <= int(c) <= 15 for c in choices):
        raise PrinterError("Enter four numbers between 1 and 15.")
    SENDER.send_bytes(cmd_align_set([int(c) for c in choices]), "set alignment")
    if save:
        time.sleep(1.0)
        SENDER.send_bytes(cmd_align_save(), "save alignment")
