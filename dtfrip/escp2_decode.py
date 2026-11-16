"""ESC/P2 stream decoder + virtual printer (used for testing our output).

It parses the command stream and replays it onto a virtual page, recording
for every dot: channel, level (1..3) and the pass number that printed it.
"""
import struct
from collections import defaultdict

import numpy as np

COLOR_NAMES = {0: "K", 1: "M", 2: "C", 4: "Y", 0x11: "LM", 0x12: "LC"}


def unpack_rle(data, pos, nbytes):
    """TIFF PackBits decompression. Returns (bytes, new_pos)."""
    out = bytearray()
    while len(out) < nbytes:
        n = data[pos]; pos += 1
        if n < 128:
            out += data[pos:pos + n + 1]; pos += n + 1
        elif n > 128:
            out += bytes([data[pos]]) * (257 - n); pos += 1
    if len(out) != nbytes:
        raise ValueError(f"RLE overrun {len(out)} != {nbytes}")
    return bytes(out), pos


def decode(data, simulate=True, verbose=False, sink=None):
    pos = 0
    cmds = []
    st = dict(page_unit=None, v_unit=None, h_unit=None, vpos=0, hpos=0,
              nozzle_sep_units=None, base_h=None, raster_units=None)
    dots = []  # (channel, x_hunits, y_vunits, level, pass_no)
    pass_no = -1
    last_v = None
    n = len(data)
    remote = False
    while pos < n:
        b = data[pos]
        if remote:
            # remote mode: 2 letters + len16 + args, ends with ESC 00 00 00
            if data[pos:pos + 4] == b"\x1b\x00\x00\x00":
                remote = False; pos += 4; cmds.append(("REMOTE_END",)); continue
            name = data[pos:pos + 2].decode("latin1")
            ln = struct.unpack("<H", data[pos + 2:pos + 4])[0]
            cmds.append(("RC", name, data[pos + 4:pos + 4 + ln]))
            pos += 4 + ln; continue
        if b == 0x00 and data[pos:pos + 4] == b"\x00\x00\x00\x1b":
            # EJL preamble: skip to after '\n@EJL     \n'
            end = data.find(b"@EJL     \n", pos)
            if end < 0:
                end = data.find(b"@EJL\n@EJL\n", pos)
            pos = data.find(b"\n", end) + 1
            cmds.append(("EJL",)); continue
        if b == 0x0c:
            cmds.append(("FF",)); pos += 1; continue
        if b == 0x0d:
            # carriage return: head goes back to the left margin
            st["hpos"] = 0
            cmds.append(("CR",)); pos += 1; continue
        if b == 0x0a:
            cmds.append(("LF",)); pos += 1; continue
        if b == 0x00:
            pos += 1; continue
        if b != 0x1b:
            raise ValueError(f"unexpected byte {b:#x} at {pos}")
        c = data[pos + 1]
        if c == ord("@"):
            cmds.append(("INIT",)); pos += 2; continue
        if c == ord("U"):
            cmds.append(("ESC U", data[pos + 2])); pos += 3; continue
        if c == ord("("):
            sub = chr(data[pos + 2])
            ln = struct.unpack("<H", data[pos + 3:pos + 5])[0]
            args = data[pos + 5:pos + 5 + ln]
            pos += 5 + ln
            if sub == "R":
                remote = True; cmds.append(("REMOTE", args)); continue
            if sub == "U":
                P, V, H, m = args[0], args[1], args[2], struct.unpack("<H", args[3:5])[0]
                st.update(page_unit=m // P, v_unit=m // V, h_unit=m // H)
            elif sub == "D":
                r = struct.unpack("<H", args[0:2])[0]
                st.update(raster_units=r, nozzle_sep_res=r // args[2], base_h=r // args[3])
            elif sub == "v":
                d = struct.unpack("<i", args[:4])[0] if ln == 4 else struct.unpack("<h", args[:2])[0]
                st["vpos"] += d
            elif sub == "V":
                st["vpos"] = struct.unpack("<i", args[:4])[0] if ln == 4 else struct.unpack("<h", args[:2])[0]
            elif sub == "$":
                st["hpos"] = struct.unpack("<i", args[:4])[0]
            elif sub == "\\":
                u, d = struct.unpack("<Hh", args[:4])
                st["hpos"] += d
            cmds.append(("ESC(" + sub, args)); continue
        if c == ord("i"):
            color, comp, bpp = data[pos + 2], data[pos + 3], data[pos + 4]
            nb, rows = struct.unpack("<HH", data[pos + 5:pos + 9])
            pos += 9
            if (st["vpos"], ) != last_v:
                pass_no += 1; last_v = (st["vpos"],)
            nozzle_rows = []
            for r in range(rows):
                if comp == 1:
                    row, pos = unpack_rle(data, pos, nb)
                else:
                    row = data[pos:pos + nb]; pos += nb
                nozzle_rows.append(row)
            cmds.append(("ESC i", color, comp, bpp, nb, rows, st["vpos"], st["hpos"]))
            if simulate:
                arr = np.frombuffer(b"".join(nozzle_rows), dtype=np.uint8).reshape(rows, nb)
                if bpp == 2:
                    lv = np.stack([(arr >> 6) & 3, (arr >> 4) & 3, (arr >> 2) & 3, arr & 3], -1).reshape(rows, nb * 4)
                else:
                    lv = np.unpackbits(arr, axis=1)
                ys, xs = np.nonzero(lv)
                if sink:
                    sink(color, xs, ys, lv[ys, xs], st["vpos"], st["hpos"], pass_no, st)
                else:
                    dots.append((color, xs, ys, lv[ys, xs], st["vpos"], st["hpos"], pass_no))
            continue
        raise ValueError(f"unknown ESC {chr(c)!r} at {pos}")
    return cmds, st, dots


def render(dots, st, v_res, h_res):
    """Rebuild per-channel dot images on the device grid.

    v_res / h_res: the raster resolution of the job (dots per inch).
    Each ESC i row r is nozzle r, which sits r * nozzle_pitch below vpos.
    Returns {color: (level_img, pass_img)} in device pixels.
    """
    pitch_rows = v_res // 90  # L800 / Artisan 50: 90 dpi nozzle pitch
    vunit = st["v_unit"]; hunit = st["h_unit"]
    out = {}
    pts = defaultdict(list)
    for color, xs, ys, lvl, vpos, hpos, pno in dots:
        y = vpos * v_res // vunit + ys * pitch_rows
        # each ESC i column step is one base-res pixel (h_res/base_h raster cols)
        x0 = hpos * h_res // hunit
        step = h_res // st["base_h"]
        x = x0 + xs * step
        pts[color].append((y, x, lvl, np.full(len(xs), pno)))
    for color, lst in pts.items():
        y = np.concatenate([p[0] for p in lst]); x = np.concatenate([p[1] for p in lst])
        l = np.concatenate([p[2] for p in lst]); p = np.concatenate([p[3] for p in lst])
        out[color] = (y, x, l, p)
    return out
