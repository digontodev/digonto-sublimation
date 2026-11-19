"""In-process engine for the program window (no web server: the window calls these functions directly)."""
import io
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from . import __version__, config, printer, testprints
from .flavor import FLAVOR, TAGLINE, TITLE
from .modes import PRESETS
from .rip import (MM, PAGE_SIZES, Item, RipSettings, clean_channels, clean_slots, compose, layout, natural_size_mm, rip,
                  slots_from_channels)

LOCK = threading.Lock()
JOB = {"stage": "idle", "progress": 0.0, "error": "", "result": None, "kind": "", "started": 0}
_cancel = {"flag": False}
BUSY = ("layout", "dots", "printfile", "printing")


class EngineError(RuntimeError):
    pass


# ------------------------------------------------------------------ session
def session_path():
    return config.config_dir() / "session.json"


def load_session():
    try:
        d = json.loads(session_path().read_text())
    except Exception:
        d = {}
    cfg = config.load()
    rs = RipSettings.from_dict({**cfg.get("rip", {}), **d.get("settings", {})})
    items = [Item(**i) for i in d.get("items", []) if os.path.exists(i.get("path", ""))]
    return items, rs


def save_session(items, rs):
    session_path().write_text(json.dumps({"items": [asdict(i) for i in items],
                                          "settings": asdict(rs)}, indent=1))


def note_activity():
    cfg = config.load(); cfg["last_print"] = time.time(); config.save(cfg)


# ------------------------------------------------------------------ state
def state():
    items, rs = load_session()
    cfg = config.load()
    st = printer.status()
    out_items = []
    for i, it in enumerate(items):
        try:
            nw, nh = natural_size_mm(it.path)
        except Exception:
            nw, nh = 0, 0
        if it.rotate in (90, 270):
            nw, nh = nh, nw
        w = it.width_mm or nw
        out_items.append({**asdict(it), "index": i, "nat_w": nw, "nat_h": nh,
                          "w": w, "h": nh * w / nw if nw else 0,
                          "file": Path(it.path).name})
    return {
        "version": __version__,
        "flavor": FLAVOR, "title": TITLE, "tagline": TAGLINE,
        "printer": st,
        "items": out_items,
        "settings": asdict(rs),
        "presets": {k: {"label": p.label, "note": p.note, "resolution": p.resolution,
                        "passes": p.passes, "feed": p.feed, "unidirectional": p.unidirectional}
                    for k, p in PRESETS.items()},
        "profiles": config.list_profiles(),
        "job": dict(JOB),
        "keepalive": cfg.get("keepalive", {}),
        "last_print": cfg.get("last_print", 0),
        "device_override": cfg.get("device", ""),
        "devices": printer.list_devices(),
        "alignment": cfg.get("alignment", []),
        "pages": {k: list(v) for k, v in PAGE_SIZES.items()},
    }


# ------------------------------------------------------------------ items
ALLOWED = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf", ".svg", ".webp", ".bmp"}


def upload(filepath):
    """add one artwork file (copied into the program's own folder)"""
    items, rs = load_session()
    name = Path(filepath).name
    if Path(name).suffix.lower() not in ALLOWED:
        raise EngineError(f"{name}: unsupported file type")
    dest = config.uploads_dir() / f"{uuid.uuid4().hex[:8]}_{name}"
    shutil.copyfile(filepath, dest)
    try:
        nw, nh = natural_size_mm(dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise EngineError(f"{name}: cannot read ({e})") from None
    usable = PAGE_W(rs) - 2 * 3.2                       # the paper that is set (A4, A3 for the L1800, a roll ...)
    items.append(Item(str(dest), width_mm=round(min(nw, usable), 1), name=name))
    save_session(items, rs)
    return {"added": [name]}


def set_items(data):
    items, rs = load_session()
    by_index = {int(d["index"]): d for d in data.get("items", [])}
    new = []
    for i, it in enumerate(items):
        d = by_index.get(i)
        if d is None or d.get("remove"):
            continue
        it.width_mm = max(1.0, float(d.get("width_mm") or it.width_mm))
        it.copies = max(1, min(50, int(d.get("copies") or 1)))
        it.rotate = int(d.get("rotate") or 0) % 360
        new.append(it)
    save_session(new, rs)
    return {"ok": True}


def align_item(d):
    """put one artwork at a corner / centre of the page (or back to automatic layout)"""
    from .rip import MARGIN_MM
    items, rs = load_session()
    i, mode = int(d.get("index", -1)), d.get("mode", "")
    if not 0 <= i < len(items):
        raise EngineError("no artwork selected")
    it = items[i]
    if mode == "auto":
        it.x_mm = it.y_mm = None
        save_session(items, rs)
        return {"ok": True}
    if it.copies > 1:
        raise EngineError("Aligning needs 1 copy. Set Copies to 1 first (or add the file again).")
    try:
        placed, (pw, ph) = layout(items, rs)
    except Exception as e:
        raise EngineError(str(e)) from None
    p = next((q for q in placed if q.item is it), None)
    if p is None:
        raise EngineError("artwork not found")
    x, y = p.x_mm, p.y_mm
    if mode in ("lt", "rt"):
        x = MARGIN_MM if mode == "lt" else pw - MARGIN_MM - p.w_mm
        y = rs.top_mm
    elif mode == "hc":
        x = (pw - p.w_mm) / 2
    elif mode == "vc":
        y = (ph - p.h_mm) / 2
    elif mode == "c":
        x, y = (pw - p.w_mm) / 2, (ph - p.h_mm) / 2
    else:
        raise EngineError("unknown mode")
    it.x_mm, it.y_mm = round(max(0.0, x), 2), round(max(0.0, y), 2)
    save_session(items, rs)
    return {"ok": True}


def layout_boxes():
    """where every artwork sits on the page (mm) - the window uses it to drag artwork with the mouse"""
    items, rs = load_session()
    if not items:
        return {"page": [PAGE_W(rs), PAGE_H(rs)], "boxes": []}
    try:
        placed, (pw, ph) = layout(items, rs)
    except Exception:
        return {"page": [PAGE_W(rs), PAGE_H(rs)], "boxes": []}
    ids = {id(it): i for i, it in enumerate(items)}
    return {"page": [pw, ph], "boxes": [{"index": ids[id(p.item)], "x": p.x_mm, "y": p.y_mm, "w": p.w_mm, "h": p.h_mm}
                                         for p in placed]}


def PAGE_W(rs):
    from .rip import PAGE_SIZES
    return rs.page_width_mm or PAGE_SIZES.get(rs.page, PAGE_SIZES["A4"])[0]


def PAGE_H(rs):
    from .rip import PAGE_SIZES
    return rs.page_height_mm or PAGE_SIZES.get(rs.page, PAGE_SIZES["A4"])[1]


def _fix(items, i):
    """make artwork i a single, freely placed piece (extra copies stay in the automatic layout)"""
    it = items[i]
    if it.copies > 1:
        import copy
        rest = copy.copy(it)
        rest.copies = it.copies - 1
        rest.x_mm = rest.y_mm = None
        it.copies = 1
        items.insert(i + 1, rest)
    return it


def place(d):
    """drop artwork at x, y (mm, top left corner)"""
    items, rs = load_session()
    i = int(d.get("index", -1))
    if not 0 <= i < len(items):
        raise EngineError("no artwork selected")
    it = _fix(items, i)
    it.x_mm, it.y_mm = round(max(0.0, float(d["x"])), 2), round(max(0.0, float(d["y"])), 2)
    save_session(items, rs)
    return {"ok": True}


def nudge(d):
    """move artwork by dx, dy mm"""
    items, rs = load_session()
    i = int(d.get("index", -1))
    if not 0 <= i < len(items):
        raise EngineError("no artwork selected")
    try:
        placed, _ = layout(items, rs)
    except Exception as e:
        raise EngineError(str(e)) from None
    p = next((q for q in placed if q.item is items[i]), None)
    if p is None:
        raise EngineError("artwork not found")
    it = _fix(items, i)
    it.x_mm = round(max(0.0, p.x_mm + float(d.get("dx", 0))), 2)
    it.y_mm = round(max(0.0, p.y_mm + float(d.get("dy", 0))), 2)
    save_session(items, rs)
    return {"ok": True}


def layout_boxes():
    """where every artwork sits on the page (for dragging in the window)"""
    items, rs = load_session()
    if not items:
        return {"page": [210.0, 297.0], "boxes": []}
    try:
        placed, (pw, ph) = layout(items, rs)
    except Exception:
        return {"page": [210.0, 297.0], "boxes": []}
    idx = {id(it): i for i, it in enumerate(items)}
    return {"page": [pw, ph], "boxes": [{"index": idx[id(p.item)], "x": p.x_mm, "y": p.y_mm, "w": p.w_mm, "h": p.h_mm}
                                        for p in placed]}


def place(d):
    """drop an artwork at x, y (mm from the page corner); several copies: this one is split off"""
    items, rs = load_session()
    i = int(d.get("index", -1))
    if not 0 <= i < len(items):
        raise EngineError("no artwork selected")
    it = items[i]
    if it.copies > 1:
        rest = Item(**{**asdict(it), "copies": it.copies - 1})
        it.copies = 1
        items.insert(i + 1, rest)
    try:
        placed, (pw, ph) = layout(items, rs)
    except Exception as e:
        raise EngineError(str(e)) from None
    p = next((q for q in placed if q.item is it), None)
    w, h = (p.w_mm, p.h_mm) if p else (0, 0)
    it.x_mm = round(min(max(0.0, float(d["x"])), max(0.0, pw - w)), 2)
    it.y_mm = round(max(0.0, float(d["y"])) if rs.feed_mode == "roll" else min(max(0.0, float(d["y"])), max(0.0, ph - h)), 2)
    save_session(items, rs)
    return {"ok": True}


def nudge(d):
    items, rs = load_session()
    i = int(d.get("index", -1))
    if not 0 <= i < len(items):
        raise EngineError("no artwork selected")
    it = items[i]
    if it.copies > 1:
        raise EngineError("Moving needs 1 copy. Set Copies to 1 first, or drag the picture.")
    placed, _ = layout(items, rs)
    p = next((q for q in placed if q.item is it), None)
    if p is None:
        raise EngineError("artwork not found")
    return place({"index": i, "x": p.x_mm + float(d.get("dx", 0)), "y": p.y_mm + float(d.get("dy", 0))})


def clear_items():
    _, rs = load_session()
    save_session([], rs)
    return {"ok": True}


def set_settings(d):
    items, rs = load_session()
    cur = asdict(rs); cur.update(d)
    rs = RipSettings.from_dict(cur)
    rs.channels = clean_channels(rs.channels)
    rs.slots = clean_slots(slots_from_channels(rs.channels))
    if rs.preset not in PRESETS:
        rs.preset = "high"
    save_session(items, rs)
    cfg = config.load(); cfg.setdefault("rip", {})["profile"] = rs.profile; config.save(cfg)
    return {"ok": True}


# ------------------------------------------------------------------ preview
def preview(view="colour"):
    items, rs = load_session()
    dpi = 40
    pw, ph = PAGE_W(rs), PAGE_H(rs)
    try:
        placed, (pw, ph) = layout(items, rs) if items else ([], (pw, ph))
        err = ""
    except Exception as e:
        placed, err = [], str(e)
    W, H = int(pw / MM * dpi), int(ph / MM * dpi)
    dark = view == "white"
    page = Image.new("RGB", (W, H), (34, 38, 46) if dark else (236, 238, 242))
    if placed:
        canvas, (x0, y0) = compose(placed, (pw, ph), dpi=dpi)
        ox, oy = round(x0 / MM * dpi), round(y0 / MM * dpi)
        if view == "white":
            import numpy as np
            a = np.asarray(canvas)[..., 3]
            if rs.white_bg_transparent:
                a = np.where(np.asarray(canvas)[..., :3].min(axis=2) >= 250, 0, a)
            thr = int(rs.alpha_threshold * 255)
            m = a if rs.white_mode == "soft" else np.where(a >= max(thr, 1), 255, 0).astype("uint8")
            if rs.white_mode == "off":
                m = m * 0
            m = Image.fromarray(m.astype("uint8"))
            page.paste(Image.new("RGB", m.size, (250, 250, 250)), (ox, oy), m)
        else:
            chk = Image.new("RGB", canvas.size, (255, 255, 255))
            d = ImageDraw.Draw(chk)
            s = 6
            for yy in range(0, canvas.height, s):
                for xx in range((yy // s) % 2 * s, canvas.width, 2 * s):
                    d.rectangle([xx, yy, xx + s - 1, yy + s - 1], fill=(222, 225, 230))
            chk.paste(canvas, (0, 0), canvas)
            mask = canvas.getchannel("A").point(lambda v: 255 if v > 0 else 0)
            page.paste(chk, (ox, oy), mask)
        if view == "film" or (view == "white" and rs.mirror):
            page = page.transpose(Image.FLIP_LEFT_RIGHT)
    d = ImageDraw.Draw(page)
    d.rectangle([0, 0, W - 1, H - 1], outline=(150, 150, 160))
    m = round(3.2 / MM * dpi)
    d.line([m, 0, m, H], fill=(200, 120, 120)); d.line([W - m, 0, W - m, H], fill=(200, 120, 120))
    b = io.BytesIO(); page.save(b, "PNG")
    return b.getvalue(), {"X-Layout-Error": err.encode("ascii", "replace").decode()}




# ------------------------------------------------------------------ rip / print
def _run_job(kind, fn, send):
    def prog(stage, f):
        JOB["stage"] = stage; JOB["progress"] = round(float(f), 3)

    try:
        spool = config.spool_dir()
        for old in sorted(spool.glob("*.prn"))[:-5]:      # keep the last few
            old.unlink(missing_ok=True)
        out = spool / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}.prn"
        res = fn(out, prog)
        JOB["result"] = {"file": out.name, "seconds": round(res.seconds, 1), "note": res.passes_note,
                         "size_mm": [round(res.width_mm), round(res.height_mm)], "mb": round(res.bytes / 1e6, 1)}
        if send:
            JOB["stage"] = "printing"; JOB["progress"] = 0.0
            done = threading.Event()
            printer.SENDER.send_file(str(out), kind, on_done=lambda e: done.set())
            while not done.wait(0.5):
                s = printer.SENDER.state
                JOB["progress"] = round(s["sent"] / max(1, s["total"]), 3)
            if printer.SENDER.state["error"]:
                raise RuntimeError(printer.SENDER.state["error"])
            note_activity()
        JOB["stage"] = "done"; JOB["progress"] = 1.0
    except Exception as e:
        JOB["stage"] = "error"; JOB["error"] = str(e)
        traceback.print_exc()


def start_job(kind, fn, send):
    with LOCK:
        if JOB["stage"] in BUSY:
            raise printer.PrinterError("A job is already running.")
        if send:
            printer.device_or_raise()
            if printer.SENDER.busy():
                raise printer.PrinterError("The printer is busy.")
        _cancel["flag"] = False
        JOB.update(stage="layout", progress=0.0, error="", result=None, kind=kind, started=time.time())
    threading.Thread(target=_run_job, args=(kind, fn, send), daemon=True).start()


def _cancelled():
    return _cancel["flag"]


def do_print(send=True):
    items, rs = load_session()
    if not items:
        raise EngineError("Add an image first.")
    start_job("print", lambda out, prog: rip(items, rs, out, prog, _cancelled), send)
    return {"ok": True}


def driver_image(fmt, dpi=None):
    """print-ready page (PNG / PDF) for printing through the printer driver"""
    from . import driverprint
    items, rs = load_session()
    if not items:
        raise EngineError("Add an image first.")
    if fmt not in ("png", "pdf"):
        raise EngineError("png or pdf")
    dpi = int(dpi or rs.out_dpi or 600)
    img, page, warns = driverprint.render_page(items, rs, dpi)
    note_activity()
    data = driverprint.to_png_bytes(img, dpi) if fmt == "png" else driverprint.to_pdf_bytes(img, dpi)
    return data, {"X-Page-Mm": f"{page[0]:.2f}x{page[1]:.2f}", "X-Dpi": str(dpi),
                  "X-Warnings": " | ".join(warns)[:400].encode("ascii", "replace").decode()}


def _keepalive_job(rs, out):
    ka = config.load().get("keepalive", {})
    return testprints.keepalive_stripe(rs, out, height_mm=float(ka.get("height_mm", 4)),
                                      white_only=bool(ka.get("white_only")))


def do_test(kind, send=True):
    _, rs = load_session()
    fns = {"channels": lambda out, prog: testprints.channel_check(rs, out, progress=prog),
           "density": lambda out, prog: testprints.density_chart(rs, out, progress=prog),
           "keepalive": lambda out, prog: _keepalive_job(rs, out)}
    if kind not in fns:
        raise EngineError("unknown test")
    start_job(kind, fns[kind], send)
    return {"ok": True}


def cancel():
    _cancel["flag"] = True
    printer.SENDER.cancel()
    return {"ok": True}


def download(name):
    p = config.spool_dir() / Path(name).name
    if not p.exists():
        raise EngineError("not found")
    return p.read_bytes(), {}


# ------------------------------------------------------------------ maintenance
def maint(action, data=None):
    if action == "nozzle":
        printer.nozzle_check()
    elif action == "clean":
        printer.head_clean()
    elif action == "power":
        printer.power_clean()
    elif action == "align_print":
        printer.align_print()
    elif action == "align_apply":
        ch = (data or {}).get("choices", [])
        printer.align_apply(ch)
        cfg = config.load(); cfg["alignment"] = ch; config.save(cfg)
    elif action == "ink":
        return {"levels": printer.ink_levels()}
    else:
        raise EngineError("unknown action")
    if action in ("clean", "power", "nozzle"):
        note_activity()
    return {"ok": True}


def upload_profile(filepath):
    if Path(filepath).suffix.lower() not in (".icc", ".icm"):
        raise EngineError("choose an .icc or .icm file")
    dest = config.profiles_dir() / Path(filepath).name
    shutil.copyfile(filepath, dest)
    try:
        from PIL import ImageCms
        p = ImageCms.getOpenProfile(str(dest))
        if p.profile.xcolor_space.strip() not in (("RGB", "CMYK") if FLAVOR == "sub" else ("CMYK",)):
            dest.unlink()
            raise EngineError("that is not a usable printer profile")
    except EngineError:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise EngineError(f"invalid profile: {e}") from None
    return {"ok": True, "name": dest.name}


def _start_service():
    """keep-alive was switched on: start the background part now (it is not left running while the option is off)"""
    import subprocess
    import sys
    slug = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if slug not in ("digonto-dtfrip",):          # only the program that installs a background service
        return
    try:
        if sys.platform == "darwin":
            plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "de.digonto." + slug.split("-", 1)[1] + ".plist")
            if os.path.isfile(plist):
                subprocess.run(["launchctl", "unload", plist], capture_output=True)
                subprocess.run(["launchctl", "load", "-w", plist], capture_output=True)
        elif sys.platform.startswith("linux"):
            subprocess.run(["systemctl", "--user", "start", slug + ".service"], capture_output=True)
        elif sys.platform == "win32":
            script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "digonto_dtfrip.py")
            subprocess.Popen([sys.executable, script, "--keepalive"], creationflags=0x08000000 | 0x00000008,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - never break saving settings
        pass


def set_config(d):
    cfg = config.load()
    was_on = bool(cfg.get("keepalive", {}).get("enabled"))
    if "keepalive" in d:
        cfg["keepalive"].update(d["keepalive"])
    for k in ("device",):
        if k in d:
            cfg[k] = d[k]
    config.save(cfg)
    if cfg.get("keepalive", {}).get("enabled") and not was_on:
        _start_service()
    return {"ok": True}


# ------------------------------------------------------------------ keep-alive (white ink must not dry out)
def keepalive_due():
    """True when a keep-alive stripe should be printed now"""
    cfg = config.load()
    ka = cfg.get("keepalive", {})
    if not ka.get("enabled"):
        return False
    hour = datetime.now().hour
    qf, qt = int(ka.get("quiet_from", 22)), int(ka.get("quiet_to", 8))
    quiet = (qf <= hour or hour < qt) if qf > qt else (qf <= hour < qt)
    if quiet or JOB["stage"] in BUSY or printer.SENDER.busy():
        return False
    if time.time() - float(cfg.get("last_print", 0)) < float(ka.get("hours", 24)) * 3600:
        return False
    if time.time() - float(cfg.get("ka_lock", 0)) < 900:          # another copy (window / service) just started one
        return False
    return bool(printer.find_device())


def keepalive_tick():
    try:
        if keepalive_due():
            cfg = config.load(); cfg["ka_lock"] = time.time(); config.save(cfg)
            _, rs = load_session()
            start_job("keepalive", lambda out, prog: _keepalive_job(rs, out), True)
    except Exception:
        traceback.print_exc()


def keepalive_forever():
    """headless mode for the autostart service (no window)"""
    def _on():
        return bool(config.load().get("keepalive", {}).get("enabled"))
    if not _on():                       # keep-alive is off: no process stays in the background (it is started again when switched on)
        print(f"{TITLE} keep-alive is off - nothing to do")
        return
    print(f"{TITLE} {__version__} keep-alive running")
    while True:
        time.sleep(300)
        if not _on():
            return
        keepalive_tick()
