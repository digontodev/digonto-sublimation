"""Maintenance for printers that are driven by their normal driver (sublimation): test pages, weekly keep-alive."""
import json
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from . import config

PRIMARIES = {"Cyan": (0, 255, 255), "Magenta": (255, 0, 255), "Yellow": (255, 255, 0), "Black": (0, 0, 0)}
WEEK = 7 * 24 * 3600


def _state_file():
    return config.config_dir() / "driver.json"


def load_state():
    try:
        d = json.loads(_state_file().read_text())
    except Exception:
        d = {}
    return {"printer": d.get("printer", ""), "weekly": bool(d.get("weekly", True)), "last": float(d.get("last", 0))}


def save_state(st):
    _state_file().write_text(json.dumps(st))


def due(st):
    return st["weekly"] and (time.time() - st["last"]) > WEEK


def _font(px):
    for n in ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(n, px)
        except OSError:
            pass
    return ImageFont.load_default()


def nozzle_check(dpi=300):
    """A4 page: per ink a solid block and a fine line pattern. Missing lines / banding = clogged nozzles."""
    mm = dpi / 25.4
    W, H = round(210 * mm), round(297 * mm)
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f = _font(int(4 * mm))
    d.text((12 * mm, 8 * mm), "Digonto Sublimation - nozzle check", fill=(0, 0, 0), font=f)
    y = 20 * mm
    for name, col in PRIMARIES.items():
        d.text((12 * mm, y), name, fill=(0, 0, 0), font=f)
        d.rectangle([40 * mm, y, 195 * mm, y + 14 * mm], fill=col)                       # solid block
        step = max(3, round(0.5 * mm))
        for yy in range(round(y + 17 * mm), round(y + 29 * mm), step):                    # fine lines
            d.line([40 * mm, yy, 195 * mm, yy], fill=col, width=1)
        y += 40 * mm
    return im


def keepalive_page(dpi=300):
    """A small stripe of every ink near the top of an A4 page (use scrap paper): keeps the nozzles wet."""
    mm = dpi / 25.4
    W, H = round(210 * mm), round(297 * mm)
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    x = 12 * mm
    for col in PRIMARIES.values():
        d.rectangle([x, 10 * mm, x + 44 * mm, 22 * mm], fill=col)
        x += 47 * mm
    return im


def open_driver_maintenance(printer_name=""):
    """the Epson maintenance screens (nozzle check, head cleaning) live in the driver / Epson utility"""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/e", "/n", printer_name or ""])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.printfax"])
        else:
            subprocess.Popen(["system-config-printer"])
    except Exception:
        return False
    return True


HELP = ("Head cleaning and the built-in nozzle check are started by the Epson driver itself:\n\n"
        "  Windows: printer preferences > Maintenance > Nozzle Check / Head Cleaning (button below opens it)\n"
        "  macOS: System Settings > Printers & Scanners > your printer > Options & Supplies > Utility > Open Printer Utility\n"
        "  Linux: use the printer's own buttons, or Epson's utility.\n\n"
        "This program adds a weekly reminder and prints the test pages through the driver for you.\n"
        "Rule of thumb: print the nozzle check; if lines are missing, run one head cleaning, print the check again, "
        "and repeat at most 3 times - then wait a few hours before trying again.")
