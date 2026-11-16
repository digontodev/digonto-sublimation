"""Command line:  dtfrip <command> ...   (run `dtfrip -h`)."""
import argparse
import sys
import time
from pathlib import Path

from . import __version__, config, printer, testprints
from .modes import PRESETS
from .rip import Item, RipSettings, rip


_last = {"s": None, "p": -1}


def _bar(stage, f):
    p = int(f * 20)
    if (stage, p) == (_last["s"], _last["p"]):
        return
    _last.update(s=stage, p=p)
    sys.stderr.write(f"\r{stage:10s} {int(f * 100):3d}%   "); sys.stderr.flush()


def _send(path, name):
    printer.SENDER.send_file(path, name)
    while printer.SENDER.busy():
        s = printer.SENDER.state
        _bar("printing", s["sent"] / max(1, s["total"])); time.sleep(0.5)
    sys.stderr.write("\n")
    if printer.SENDER.state["error"]:
        raise printer.PrinterError(printer.SENDER.state["error"])
    cfg = config.load(); cfg["last_print"] = time.time(); config.save(cfg)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dtfrip", description=f"Digonto DTFRIP {__version__} for Epson L800 (Y K W W M C)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keepalive", help="run without a window and keep the white ink moving (autostart service)")

    p = sub.add_parser("print", help="RIP image(s) and print")
    p.add_argument("files", nargs="+")
    p.add_argument("-w", "--width", type=float, default=0, help="width in mm (0 = natural size)")
    p.add_argument("-n", "--copies", type=int, default=1)
    p.add_argument("-q", "--quality", choices=list(PRESETS), default=None)
    p.add_argument("--profile", default=None, help="ICC profile name in the profiles folder")
    p.add_argument("--no-white", action="store_true")
    p.add_argument("--no-mirror", action="store_true")
    p.add_argument("-o", "--output", help="only write the print file (.prn), do not print")

    for name, hlp in [("nozzle", "print the nozzle check pattern"), ("clean", "head cleaning"),
                      ("power-clean", "power cleaning (uses a lot of ink)"),
                      ("align-print", "print the head alignment pattern"), ("ink", "show ink counters"),
                      ("devices", "list USB printer devices"), ("build", "compile the fast C helper")]:
        sub.add_parser(name, help=hlp)
    a = sub.add_parser("align-set", help="save alignment: four numbers 1-15")
    a.add_argument("choices", nargs=4, type=int)
    t = sub.add_parser("test", help="test prints")
    t.add_argument("kind", choices=["channels", "density", "keepalive"])
    t.add_argument("-o", "--output")
    sp = sub.add_parser("send", help="send an existing .prn file to the printer")
    sp.add_argument("file")

    args = ap.parse_args(argv)
    try:
        return _run(args)
    except (printer.PrinterError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _settings():
    cfg = config.load()
    try:
        import json
        sess = json.loads((config.config_dir() / "session.json").read_text()).get("settings", {})
    except Exception:
        sess = {}
    return RipSettings.from_dict({**cfg.get("rip", {}), **sess})


def _run(args):
    if args.cmd == "keepalive":
        from .backend import keepalive_forever
        keepalive_forever(); return 0
    if args.cmd == "build":
        from .core import build
        p = build(quiet=False); print(p or "build failed"); return 0 if p else 1
    if args.cmd == "devices":
        for dev, ident in printer.list_devices():
            print(dev, ident)
        print("selected:", printer.find_device()); return 0
    if args.cmd == "nozzle":
        printer.nozzle_check(); print("nozzle check sent"); return 0
    if args.cmd == "clean":
        printer.head_clean(); print("head cleaning started"); return 0
    if args.cmd == "power-clean":
        printer.power_clean(); print("power cleaning started"); return 0
    if args.cmd == "align-print":
        printer.align_print(); print("alignment pattern sent"); return 0
    if args.cmd == "align-set":
        printer.align_apply(args.choices); print("alignment saved"); return 0
    if args.cmd == "ink":
        for k, v in printer.ink_levels().items():
            print(f"{k:10s} {v:3d}%  " + "#" * (v // 4))
        return 0
    if args.cmd == "send":
        _send(args.file, Path(args.file).name); return 0

    s = _settings()
    out = Path(getattr(args, "output", None) or config.spool_dir() / f"cli-{int(time.time())}.prn")
    if args.cmd == "test":
        fn = {"channels": testprints.channel_check, "density": testprints.density_chart}.get(args.kind)
        res = fn(s, out, progress=_bar) if fn else testprints.keepalive_stripe(s, out)
    else:
        if args.quality: s.preset = args.quality
        if args.profile is not None: s.profile = args.profile
        if args.no_white: s.white_mode = "off"
        if args.no_mirror: s.mirror = False
        items = [Item(f, width_mm=args.width, copies=args.copies) for f in args.files]
        res = rip(items, s, out, progress=_bar)
    sys.stderr.write("\n")
    print(f"{res.width_mm:.0f} x {res.height_mm:.0f} mm, {res.passes_note}, ripped in {res.seconds:.1f}s -> {out}")
    if not getattr(args, "output", None):
        _send(str(out), out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
