"""Print through the printer's own driver (sublimation on any 4-ink Epson such as the ET-2862).

The program does what a RIP does - layout, mirror, colour management, ink fine-tuning, sharp resampling - and
produces one page image at the real printed size. The Epson driver then only has to print it:
set the driver's own colour management to OFF (no ICC / "no colour adjustment") so nothing is changed twice.
"""
import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageFilter

from . import config
from .rip import MM, Item, RipSettings, compose, layout

DPI_CHOICES = (300, 360, 600, 720, 1200)


def _rgb_profile(name):
    """an RGB output profile (made for the printer + sublimation ink + paper), or None"""
    p = config.find_profile(name) if name else None
    if not p:
        return None
    try:
        pr = ImageCms.getOpenProfile(str(p))
        return pr if pr.profile.xcolor_space.strip() == "RGB" else None
    except Exception:
        return None


def _ink(arr, s):
    """per-ink amount and curve for cyan / magenta / yellow (red, green, blue taken away); 1.0 = unchanged"""
    dens = {**RipSettings().density, **(s.density or {})}
    curve = {**RipSettings().curve, **(s.curve or {})}
    if all(abs(dens[k] - 1) < 1e-3 and abs(curve[k] - 1) < 1e-3 for k in "CMY") and abs(s.colour_amount - 1) < 1e-3:
        return arr
    f = arr.astype(np.float32) / 255.0
    out = np.empty_like(f)
    for i, k in enumerate("CMY"):                 # R <-> C, G <-> M, B <-> Y
        ink = 1.0 - f[..., i]
        ink = np.power(ink, max(0.2, float(curve[k]))) * float(dens[k]) * float(s.colour_amount)
        out[..., i] = 1.0 - np.clip(ink, 0, 1)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def render_page(items, s: RipSettings, dpi=600, progress=None):
    """-> (PIL RGB image of the whole page at `dpi`, page size in mm, warnings)"""
    prog = progress or (lambda *_: None)
    dpi = int(dpi or s.out_dpi or 600)
    placed, page = layout(items, s)
    prog("layout", 0.1)
    canvas, origin = compose(placed, page, dpi=dpi)
    w_px, h_px = round(page[0] / MM * dpi), round(page[1] / MM * dpi)
    sheet = Image.new("RGB", (w_px, h_px), (255, 255, 255))
    ox, oy = round(origin[0] / MM * dpi), round(origin[1] / MM * dpi)
    rgba = canvas
    if s.white_bg_transparent:
        a = np.asarray(rgba).copy()
        a[..., 3] = np.where(a[..., :3].min(axis=2) >= 250, 0, a[..., 3])
        rgba = Image.fromarray(a, "RGBA")
    sheet.paste(rgba.convert("RGB"), (ox, oy), rgba.getchannel("A"))
    prog("colour", 0.5)
    warns = []
    if s.profile:
        pr = _rgb_profile(s.profile)
        if pr is None:
            warns.append("The selected profile is not an RGB profile, so it was ignored (use an RGB profile made for the driver).")
        else:
            try:
                tr = ImageCms.buildTransform(ImageCms.createProfile("sRGB"), pr, "RGB", "RGB",
                                             renderingIntent=int(s.intent) if int(s.intent) in (0, 1, 2, 3) else 0)
                sheet = ImageCms.applyTransform(sheet, tr)
            except Exception as e:
                warns.append(f"Colour profile failed ({e}).")
    arr = _ink(np.asarray(sheet), s)
    sheet = Image.fromarray(arr, "RGB")
    if getattr(s, "sharpen", False):
        sheet = sheet.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
    if s.mirror:
        sheet = sheet.transpose(Image.FLIP_LEFT_RIGHT)
    prog("done", 1.0)
    return sheet, page, warns


def to_png_bytes(img, dpi):
    b = io.BytesIO()
    img.save(b, "PNG", dpi=(dpi, dpi))
    return b.getvalue()


def to_pdf_bytes(img, dpi):
    b = io.BytesIO()
    img.save(b, "PDF", resolution=dpi)
    return b.getvalue()
