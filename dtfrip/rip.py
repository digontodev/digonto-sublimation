"""DTF RIP pipeline: images -> layout -> colour + white separation -> dots -> ESC/P2."""
import io
import math
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
from PIL import Image, ImageCms

from . import config
from .core import Halftoner, erode
from .escp2 import JobGeometry, JobWriter, dot_mask, nozzle_groups, simple_groups
from .modes import NOZZLES, PRESETS, RESOLUTIONS, check_weave

Image.MAX_IMAGE_PIXELS = 400_000_000
WORK_DPI = 720          # layout / colour resolution (device res is 720 or 1440)
MM = 25.4

PAGE_SIZES = {"A4": (210.0, 297.0), "A3": (297.0, 420.0), "A5": (148.0, 210.0), "Letter": (215.9, 279.4)}
MARGIN_MM = 3.2          # printer can not print closer to the left/right edge


# ======================================================================== data
from .flavor import FLAVOR
SUB = FLAVOR == "sub"
# head channel -> ink.  DTF: the two light channels carry white.  Sublimation: 4 colours (CMYK); the two light
# channels are unused unless you load light cyan / light magenta and choose them in Settings > Ink order.
DEFAULT_SLOTS = ({"K": "K", "C": "C", "LC": "-", "M": "M", "LM": "-", "Y": "Y"} if SUB
                 else {"K": "K", "C": "C", "LC": "W1", "M": "M", "LM": "W2", "Y": "Y"})
INK_NAMES = ("C", "M", "Y", "K", "W1", "W2", "LC", "LM", "-")
ALL_INKS = ("C", "M", "Y", "K", "W1", "W2", "LC", "LM")


# up to 8 ink channels, in the order of the tanks (left to right). "-" = channel off.
N_CHANNELS = 8
CHANNEL_INKS = ("K", "C", "M", "Y", "LC", "LM") + (() if SUB else ("W1", "W2"))
DEFAULT_CHANNELS = (["C", "M", "Y", "K", "-", "-", "-", "-"] if SUB else ["Y", "K", "W1", "W2", "M", "C", "-", "-"])
HEAD_ORDER = ("K", "C", "LC", "M", "LM", "Y") if SUB else ("Y", "K", "LC", "LM", "M", "C")


def clean_channels(ch):
    """8 entries, every ink at most once, unknown names = off"""
    ch = list(ch or DEFAULT_CHANNELS)[:N_CHANNELS]
    ch += ["-"] * (N_CHANNELS - len(ch))
    used, out = set(), []
    for v in ch:
        if v not in CHANNEL_INKS or v in used:
            v = "-"
        if v != "-":
            used.add(v)
        out.append(v)
    return out


def slots_from_channels(ch):
    """channel list -> head slots. DTF: tank 1..6 = Y K LC LM M C positions of the L800 head.
    Sublimation (printed through the driver): the driver decides the order, we only note which inks are present."""
    ch = clean_channels(ch)
    if SUB:
        return {p: (p if p in ch else "-") for p in DEFAULT_SLOTS}
    return dict(zip(HEAD_ORDER, ch[:6]))


def split_light(v):
    """one cyan / magenta amount -> (dark, light): light ink carries the pale tones, dark takes over from 50 %"""
    dark = np.clip(2 * v - 1, 0, 1)
    light = np.minimum(2 * v, 1) * (1 - dark)
    return dark, light


def clean_slots(slots):
    """head channel -> ink; every ink at most once, unknown names become '-' (unused)"""
    out, used = {}, set()
    for phys in DEFAULT_SLOTS:
        v = (slots or {}).get(phys, DEFAULT_SLOTS[phys])
        if v not in INK_NAMES or (v != "-" and v in used):
            v = "-"
        if v != "-":
            used.add(v)
        out[phys] = v
    return out


@dataclass
class Item:
    path: str
    width_mm: float = 0.0          # 0 = natural size
    x_mm: Optional[float] = None   # None = auto layout
    y_mm: Optional[float] = None
    copies: int = 1
    rotate: int = 0                # 0 / 90 / 180 / 270
    name: str = ""


@dataclass
class RipSettings:
    preset: str = "high"
    page: str = "A4"
    page_height_mm: float = 0.0       # 0 = page default (longer = roll); with page "custom": the page length
    page_width_mm: float = 0.0        # only with page "custom"
    feed_mode: str = "tray"           # tray = sheets from the rear tray | roll = roll, the page is as long as the artwork
    colour_dot: str = "var"           # var (all drop sizes) | s | m | l
    white_dot: str = "var"            # var | m | l
    white_detail: int = 1             # 1 = same detail as colour, 2 / 4 = coarser white mask (white "DPI" divided)
    page_width_mm: float = 0.0        # 0 = width of the named page (A4 ...); used for "Custom" / roll
    white_dpi: int = 0                # 0 = same detail as colour; 2 = white mask at half detail, 4 = quarter
    colour_dot: str = "var"           # var | small | medium | large
    white_dot: str = "var"            # var | medium | large
    profile: str = ""                 # ICC file name ("" = built-in conversion)
    intent: int = 0                   # 0 perceptual 1 relative 2 saturation 3 absolute
    mirror: bool = True
    colour_first: bool = True         # DTF: colour first, white on top
    white_mode: str = "off" if SUB else "solid"         # solid | soft | off
    white_w1: bool = True
    white_w2: bool = True
    white_amount: float = 1.0         # x preset white limit
    colour_amount: float = 1.0        # x preset colour limit
    total_ink_limit: float = 3.0      # max C+M+Y+K (3.0 = 300 %)
    choke_mm: float = 0.15            # white shrink, hides white edge
    alpha_threshold: float = 0.5      # pixels more transparent than this are not printed
    no_white_under_black: bool = False
    white_bg_transparent: bool = False  # treat pure white background as transparent
    black_threshold: int = 35         # 0..255, "black" for the option above
    density: dict = field(default_factory=lambda: {k: 1.0 for k in ALL_INKS})
    curve: dict = field(default_factory=lambda: {k: 1.0 for k in ALL_INKS})  # >1 lighter mid-tones
    # which ink sits in which channel of the print head (left to right on the L800: Y K LC LM M C is the head's own names)
    slots: dict = field(default_factory=lambda: dict(DEFAULT_SLOTS))
    channels: list = field(default_factory=lambda: list(DEFAULT_CHANNELS))   # up to 8 ink channels in tank order, "-" = off
    out_dpi: int = 600                # driver printing: resolution of the page image
    sharpen: bool = False             # driver printing: gentle sharpening
    unidirectional: Optional[bool] = None   # None = preset default
    feed: int = 0                     # 0 = preset default (advanced)
    passes: int = 0
    white_passes: int = 0
    spacing_mm: float = 4.0           # gap between auto-placed items
    top_mm: float = 3.0               # first row on the film

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        if d and "channels" not in d and d.get("slots"):          # older saved settings: build the channel list from the head slots
            sl = clean_slots(d["slots"])
            if SUB:
                s.channels = clean_channels([i for i in ("C", "M", "Y", "K", "LC", "LM") if i in sl.values()])
            else:
                s.channels = clean_channels([sl.get(p, "-") for p in HEAD_ORDER])
        return s


# ======================================================================== load
def load_image(path, target_width_px=None):
    """Load anything printable into an RGBA PIL image (+ its ICC profile bytes)."""
    path = str(path)
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return _load_pdf(path, target_width_px), None
    if ext == ".svg":
        return _load_svg(path, target_width_px), None
    im = Image.open(path)
    im.load()
    icc = im.info.get("icc_profile")
    dpi = im.info.get("dpi")
    if im.mode == "CMYK":
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc)) if icc else _default_cmyk()
        im = ImageCms.profileToProfile(im, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        icc = None
    if im.mode in ("P", "LA", "L", "1", "I;16", "I", "F"):
        im = im.convert("RGBA")
    elif im.mode == "RGB":
        im = im.convert("RGBA")
    elif im.mode != "RGBA":
        im = im.convert("RGBA")
    im.info["dpi"] = dpi
    return im, icc


def _fitz():
    try:
        import fitz
    except ImportError:  # pragma: no cover
        raise RuntimeError("PDF / SVG support needs PyMuPDF (pip install pymupdf)")
    return fitz


def _render_doc(path, target_width_px):
    """first page of a PDF or SVG -> RGBA (transparent background)"""
    fitz = _fitz()
    doc = fitz.open(path)
    try:
        page = doc[0]
        zoom = (target_width_px / page.rect.width) if target_width_px else 300.0 / 72.0
        zoom = max(0.05, min(zoom, 60.0))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
        im = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()
    return im


def _load_pdf(path, target_width_px):
    return _render_doc(path, target_width_px)


def _load_svg(path, target_width_px):
    return _render_doc(path, target_width_px)


def _doc_size_mm(path):
    fitz = _fitz()
    doc = fitz.open(path)
    try:
        r = doc[0].rect
        return r.width / 72.0 * MM, r.height / 72.0 * MM
    finally:
        doc.close()


_SIZE_CACHE = {}


def natural_size_mm(path):
    """Size of the artwork in mm using its dpi (default 300 dpi)."""
    try:
        key = (str(path), os.path.getmtime(path))
    except OSError:
        key = None
    if key in _SIZE_CACHE:
        return _SIZE_CACHE[key]
    r = _natural_size_mm(path)
    if key:
        _SIZE_CACHE[key] = r
    return r


def _natural_size_mm(path):
    ext = Path(path).suffix.lower()
    if ext in (".pdf", ".svg"):
        return _doc_size_mm(path)
    else:
        im = Image.open(path)
        d = im.info.get("dpi") or (300, 300)
        dpi = float(d[0]) if d and d[0] and d[0] >= 100 else 300.0   # 72/96 dpi tags are usually meaningless
    w, h = im.size
    return w / dpi * MM, h / dpi * MM


# ======================================================================== colour
_PROFILE_CACHE = {}


def _default_cmyk():
    p = config.find_profile("def-CMYK.icc")
    if p:
        return ImageCms.getOpenProfile(str(p))
    return None


def colour_transform(profile_name, intent):
    key = (profile_name, intent)
    if key in _PROFILE_CACHE:
        return _PROFILE_CACHE[key]
    t = None
    p = config.find_profile(profile_name) if profile_name else None
    if p:
        dst = ImageCms.getOpenProfile(str(p))
        t = ImageCms.buildTransform(ImageCms.createProfile("sRGB"), dst, "RGB", "CMYK",
                                    renderingIntent=ImageCms.Intent(intent))
    _PROFILE_CACHE[key] = t
    return t


def rgb_to_cmyk(rgb: Image.Image, profile_name, intent):
    t = colour_transform(profile_name, intent)
    if t is not None:
        return ImageCms.applyTransform(rgb, t)
    # built-in conversion: simple GCR (used when no ICC profile is installed)
    a = np.asarray(rgb, dtype=np.float32) / 255.0
    k = 1.0 - a.max(axis=2)
    d = np.maximum(1.0 - k, 1e-6)
    cmy = (1.0 - a - k[..., None]) / d[..., None]
    # 70 % grey component replacement keeps neutrals stable
    out = np.dstack([cmy, k])
    return Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), "CMYK")


# ======================================================================== layout
@dataclass
class Placed:
    item: Item
    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float


def layout(items: List[Item], s: RipSettings):
    """Shelf packing of items (and copies) on the page. Returns placements, page (w, h)."""
    pw, ph = PAGE_SIZES.get(s.page, PAGE_SIZES["A4"])
    if s.page_height_mm:
        ph = s.page_height_mm
    if s.page_width_mm:
        pw = s.page_width_mm
    usable_w = pw - 2 * MARGIN_MM
    placed = []
    x = MARGIN_MM; y = s.top_mm; shelf_h = 0.0
    for it in items:
        nw, nh = natural_size_mm(it.path)
        if it.rotate in (90, 270):
            nw, nh = nh, nw
        w = it.width_mm or nw
        h = nh * w / nw
        if w > usable_w + 0.01:
            raise ValueError(f"{it.name or Path(it.path).name}: {w:.0f} mm is wider than the printable {usable_w:.0f} mm")
        for _ in range(max(1, int(it.copies))):
            if it.x_mm is not None and it.y_mm is not None:
                placed.append(Placed(it, it.x_mm, it.y_mm, w, h)); continue
            if x + w > pw - MARGIN_MM + 0.01:
                x = MARGIN_MM; y += shelf_h + s.spacing_mm; shelf_h = 0
            placed.append(Placed(it, x, y, w, h))
            x += w + s.spacing_mm
            shelf_h = max(shelf_h, h)
    if s.feed_mode == "roll":                    # roll: no sheet end - the length follows the artwork
        ph = max(50.0, max((p.y_mm + p.h_mm for p in placed), default=0.0) + 3.0)
    for p in placed:
        if p.y_mm + p.h_mm > ph + 0.01:
            raise ValueError(f"layout does not fit on the {pw:.0f}x{ph:.0f} mm page "
                             f"(needs {p.y_mm + p.h_mm:.0f} mm). Reduce sizes/copies or use a longer page.")
    return placed, (pw, ph)


def compose(placed: List[Placed], page_mm, dpi=WORK_DPI):
    """Render all items into one RGBA canvas covering only their bounding box."""
    x0 = min(p.x_mm for p in placed); y0 = min(p.y_mm for p in placed)
    x1 = max(p.x_mm + p.w_mm for p in placed); y1 = max(p.y_mm + p.h_mm for p in placed)
    W = int(math.ceil((x1 - x0) / MM * dpi)); H = int(math.ceil((y1 - y0) / MM * dpi))
    canvas = Image.new("RGBA", (max(W, 1), max(H, 1)), (255, 255, 255, 0))
    cache = {}
    for p in placed:
        tw = max(1, round(p.w_mm / MM * dpi)); th = max(1, round(p.h_mm / MM * dpi))
        key = (p.item.path, tw, th, p.item.rotate)
        if key not in cache:
            src_w = th if p.item.rotate in (90, 270) else tw
            im, icc = load_image(p.item.path, target_width_px=src_w)
            if icc:
                try:
                    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                    rgb = ImageCms.profileToProfile(im.convert("RGB"), src, ImageCms.createProfile("sRGB"))
                    rgb.putalpha(im.getchannel("A")); im = rgb
                except Exception:
                    pass
            if p.item.rotate:
                im = im.rotate(-p.item.rotate, expand=True)
            # resample in premultiplied space so transparent edges do not go dark
            im = _resize_rgba(im, (tw, th))
            cache[key] = im
        canvas.alpha_composite(cache[key], (round((p.x_mm - x0) / MM * dpi), round((p.y_mm - y0) / MM * dpi)))
    return canvas, (x0, y0)


def _resize_rgba(im, size):
    """Pillow resamples RGBA in premultiplied space, so edges stay clean."""
    if im.size == size:
        return im
    return im.resize(size, Image.LANCZOS)


# ======================================================================== separation
@dataclass
class Separation:
    cmyk: np.ndarray        # uint8 (h, w, 4) at work dpi
    white: np.ndarray       # uint8 (h, w)
    origin_mm: tuple        # top-left of the arrays on the page
    page_mm: tuple
    rgba_preview: Image.Image
    mirrored: bool = False
    white2: Optional[np.ndarray] = None   # separate plane for the 2nd white channel (tests)


def separate(items: List[Item], s: RipSettings) -> Separation:
    placed, page = layout(items, s)
    canvas, origin = compose(placed, page)
    arr = np.asarray(canvas)
    alpha = arr[..., 3]
    if s.white_bg_transparent:
        alpha = np.where(arr[..., :3].min(axis=2) >= 250, 0, alpha).astype(np.uint8)
    thr = int(round(s.alpha_threshold * 255))
    rgb = Image.fromarray(np.ascontiguousarray(arr[..., :3]), "RGB")
    cmyk = np.array(rgb_to_cmyk(rgb, s.profile, s.intent))
    if s.white_mode == "soft":
        cov = alpha
    else:
        cov = np.where(alpha >= max(thr, 1), 255, 0).astype(np.uint8)
    for y in range(0, cmyk.shape[0], 512):          # in strips to save memory
        c = cmyk[y:y + 512].astype(np.uint16)
        c *= cov[y:y + 512, :, None]
        cmyk[y:y + 512] = c // 255
    del rgb
    if s.white_mode == "off":
        white = np.zeros_like(alpha)
    else:
        white = cov.copy()
        if s.no_white_under_black:
            black = arr[..., :3].max(axis=2) <= s.black_threshold
            white[black] = 0
        r = int(round(s.choke_mm / MM * WORK_DPI))
        if r > 0:
            white = erode(white, r)
    if s.mirror:
        cmyk = cmyk[:, ::-1]; white = white[:, ::-1]
        pw = page[0]
        w_mm = arr.shape[1] / WORK_DPI * MM
        origin = (pw - origin[0] - w_mm, origin[1])
    return Separation(np.ascontiguousarray(cmyk), np.ascontiguousarray(white), origin, page, canvas, s.mirror)


# ======================================================================== rip
@dataclass
class RipResult:
    prn_path: str
    seconds: float
    passes_note: str
    width_mm: float
    height_mm: float
    bytes: int


def weave_params(s: RipSettings):
    pr = PRESETS[s.preset]
    res = RESOLUTIONS[pr.resolution]
    feed = s.feed or pr.feed
    pc = s.passes or pr.passes
    pw = s.white_passes or pc
    uni = pr.unidirectional if s.unidirectional is None else s.unidirectional
    return pr, res, feed, pc, pw, uni


def rip(items: List[Item], s: RipSettings, out_path, progress: Callable = None,
        cancel: Callable = None) -> RipResult:
    t0 = time.time()
    prog = progress or (lambda stage, f: None)
    prog("layout", 0.0)
    sep = separate(items, s)
    return rip_separation(sep, s, out_path, progress, cancel, t0)


def rip_separation(sep: Separation, s: RipSettings, out_path, progress=None, cancel=None, t0=None):
    t0 = t0 or time.time()
    prog = progress or (lambda stage, f: None)
    pr, res, feed, pc, pw, uni = weave_params(s)
    want_white = s.white_mode != "off" and (s.white_w1 or s.white_w2) and (sep.white.any() or (sep.white2 is not None and sep.white2.any()))
    probs = check_weave(feed, pc, res, feed * pc + (feed * pw if want_white else 0))
    if want_white:
        probs += [p for p in check_weave(feed, pw, res, 0) if "multiple" in p]
    if probs:
        raise ValueError("; ".join(probs))

    fy = res.vres // WORK_DPI; fx = res.hres // WORK_DPI
    h, w = sep.white.shape
    H, W = h * fy, w * fx
    workdir = Path(tempfile.mkdtemp(prefix="dtfrip-", dir=config.spool_dir()))
    planes = {}
    slots = clean_slots(s.slots)
    channels = {}                       # ink -> head channel
    for phys, ink in slots.items():
        if ink == "-":
            continue
        if ink.startswith("W") and not (want_white and (s.white_w1 if ink == "W1" else s.white_w2)):
            continue
        channels[ink] = phys
    channels = {i: channels[i] for i in ALL_INKS if i in channels}   # fixed order keeps the dot pattern stable
    colour_ch = [p for i, p in channels.items() if not i.startswith("W")]
    white_ch = [p for i, p in channels.items() if i.startswith("W")]
    for logical, phys in channels.items():
        planes[phys] = np.lib.format.open_memmap(workdir / f"{phys}.npy", mode="w+", dtype=np.uint8, shape=(H, W))

    lvl = list(res.drops)
    colour_levels = ([0.0] + lvl, [0, 1, 2, 3])
    white_levels = ([0.0, lvl[1], 1.0], [0, 2, 3])    # white: medium + large dots only
    cd = {"s": 0, "m": 1, "l": 2}.get(s.colour_dot)
    if cd is not None:                                   # one fixed dot size for colour
        colour_levels = ([0.0, lvl[cd]], [0, cd + 1])
    wd = {"m": 1, "l": 2}.get(s.white_dot)
    if wd is not None:
        white_levels = ([0.0, lvl[wd]], [0, wd + 1])
    wdiv = s.white_detail if s.white_detail in (1, 2, 4) else 1
    cd = {"small": 0, "medium": 1, "large": 2}.get(s.colour_dot)
    if cd is not None:                                 # one fixed dot size for colour
        colour_levels = ([0.0, lvl[cd]], [0, cd + 1])
    wd = {"medium": (lvl[1], 2), "large": (1.0, 3)}.get(s.white_dot)
    if wd is not None:
        white_levels = ([0.0, wd[0]], [0, wd[1]])
    climit = min(1.0, pr.colour_limit * s.colour_amount)
    wlimit = min(1.0, pr.white_limit * s.white_amount)
    dens = {**RipSettings().density, **(s.density or {})}
    curve = {**RipSettings().curve, **(s.curve or {})}
    ht = {}
    for i, (logical, phys) in enumerate(channels.items()):
        levels, codes = white_levels if logical.startswith("W") else colour_levels
        ht[phys] = Halftoner(W, levels, codes, mirror=(logical == "W2"), seed=17 + i * 101)

    strip = 32
    for y in range(0, h, strip):
        if cancel and cancel():
            raise RuntimeError("cancelled")
        sl = slice(y, min(h, y + strip))
        cm = sep.cmyk[sl].astype(np.float32) / 255.0
        tot = cm.sum(axis=2)
        over = tot > s.total_ink_limit
        if over.any():
            cm[over] *= (s.total_ink_limit / tot[over])[:, None]
        wv = sep.white[sl].astype(np.float32) / 255.0
        wv2 = wv if sep.white2 is None else sep.white2[sl].astype(np.float32) / 255.0
        vals = {"C": cm[..., 0], "M": cm[..., 1], "Y": cm[..., 2], "K": cm[..., 3]}
        if "LC" in channels:
            vals["C"], vals["LC"] = split_light(cm[..., 0])
        if "LM" in channels:
            vals["M"], vals["LM"] = split_light(cm[..., 1])
        for logical, phys in channels.items():
            if logical.startswith("W"):
                v = (wv2 if logical == "W2" else wv) * wlimit * dens.get(logical, 1.0)
            else:
                v = vals[logical] * climit * dens.get(logical, 1.0)
            v = np.clip(v, 0, 1)
            g = float(curve.get(logical, 1.0))
            if abs(g - 1.0) > 1e-3:
                v = np.power(v, max(0.2, g))
            if fy > 1: v = np.repeat(v, fy, axis=0)
            if fx > 1: v = np.repeat(v, fx, axis=1)
            if logical.startswith("W") and s.white_dpi in (2, 4):      # coarser white mask (lower white detail)
                k = s.white_dpi
                v = np.repeat(np.repeat(v[::k, ::k], k, axis=0), k, axis=1)[:v.shape[0], :v.shape[1]]
            dy = y * fy
            ht[phys].run(v, planes[phys][dy:dy + v.shape[0]])
        prog("dots", min(1.0, (y + strip) / h))

    geo = JobGeometry(res, page_w_pt=sep.page_mm[0] / MM * 72, page_h_pt=sep.page_mm[1] / MM * 72,
                      left_pt=max(9.0, sep.origin_mm[0] / MM * 72), top_pt=sep.origin_mm[1] / MM * 72,
                      unidirectional=uni)
    if want_white:
        groups = nozzle_groups(feed, pc, pw, colour_first=s.colour_first, colour_ch=colour_ch, white_ch=white_ch)
        groups = [g for g in groups if g.name == "colour" or g.channels]
        groups = [type(g)(g.name, [c for c in g.channels if c in planes], g.first, g.count, g.passes) for g in groups]
    else:
        groups = simple_groups(feed, pc, colour_ch) if feed * pc <= NOZZLES else None
    prog("printfile", 0.0)
    with open(out_path, "wb") as f:
        jw = JobWriter(f, geo)
        jw.header()
        ok = jw.passes(planes, groups, feed, dot_mask(), progress=lambda x: prog("printfile", x), cancel=cancel)
        jw.footer()
    for p in planes.values():
        del p
    shutil.rmtree(workdir, ignore_errors=True)
    if not ok:
        raise RuntimeError("cancelled")
    note = (f"{res.name} dpi, {pc} colour passes" + (f" + {pw} white passes" if want_white else "")
            + (", unidirectional" if uni else ", bidirectional") + f", {jw.head_passes} head passes")
    return RipResult(str(out_path), time.time() - t0, note, w / WORK_DPI * MM, h / WORK_DPI * MM,
                     os.path.getsize(out_path))


# ======================================================================== preview
def previews(sep: Separation, max_px=900):
    """(as-on-garment, white-layer) preview PNG bytes."""
    rgba = sep.rgba_preview
    scale = min(1.0, max_px / max(rgba.size))
    size = (max(1, int(rgba.width * scale)), max(1, int(rgba.height * scale)))
    garment = Image.new("RGB", rgba.size, (60, 60, 64))
    garment.paste(rgba, (0, 0), rgba)
    garment = garment.resize(size, Image.LANCZOS)
    wl = Image.fromarray(np.ascontiguousarray(sep.white[:, ::-1]) if sep.mirrored else sep.white)  # reading direction
    wl = wl.resize(size, Image.BILINEAR)
    bg = Image.new("RGB", size, (30, 34, 40))
    white_img = Image.new("RGB", size, (245, 245, 245))
    bg.paste(white_img, (0, 0), wl)
    out = []
    for im in (garment, bg):
        b = io.BytesIO(); im.save(b, "PNG"); out.append(b.getvalue())
    return out
