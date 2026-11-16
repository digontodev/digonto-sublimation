"""ESC/P2 job writer for the Epson L800 head, with DTF nozzle-split weaving.

Weaving model
-------------
Pass p puts nozzle 0 at "naive" row  vpos(p) = V0 + p*feed; nozzle j prints the
row vpos + j*pitch (minus a small head offset for some channels). For a fixed
page row, high-numbered nozzles reach it first and low-numbered ones later.

DTF needs the colour layer first and the white layer on top of it, but this
head has all six channels side by side, so one pass would mix wet white into
wet colour. Like AcroRIP, we split the head: colour channels only use the
high nozzles, white only the low nozzles. Every row is coloured first and gets
its white a few passes later, after the colour has started to set.

With feed F odd (co-prime to the nozzle pitch) and a group of n = k*F nozzles,
every row is hit exactly k times by that group. Consecutive hits of a row are
`pitch` passes apart, which is used to give each hit its horizontal phase and
its share of the dots (mask).
"""
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from .core import BlockBuilder
from .modes import (COLOR_CODE, EMIT_ORDER, HEAD_OFFSET, NOZZLES, Resolution)

ESC = b"\x1b"
EXIT_PACKET = b"\x00\x00\x00\x1b\x01@EJL 1284.4\n@EJL     \n"


def remote(cmd: str, args: bytes = b"") -> bytes:
    return cmd.encode() + struct.pack("<H", len(args)) + args


def remote_block(*cmds: bytes) -> bytes:
    return ESC + b"@" + ESC + b"(R\x08\x00\x00REMOTE1" + b"".join(cmds) + ESC + b"\x00\x00\x00"


def esc_paren(c: str, args: bytes) -> bytes:
    return ESC + b"(" + c.encode() + struct.pack("<H", len(args)) + args


@dataclass
class Group:
    name: str
    channels: List[str]       # physical channel names (K, C, LC, M, LM, Y)
    first: int                # first nozzle
    count: int                # number of nozzles = passes * feed
    passes: int               # passes per row (k)


@dataclass
class JobGeometry:
    res: Resolution
    page_w_pt: float = 595.0      # A4
    page_h_pt: float = 842.0
    left_pt: float = 9.0          # image position on the page
    top_pt: float = 0.0
    unidirectional: bool = True


def nozzle_groups(feed, passes_colour, passes_white, colour_first=True, white=True,
                  colour=True, colour_ch=None, white_ch=None):
    """Split the 90 nozzles into a colour group and a white group."""
    nc = feed * passes_colour if colour else 0
    nw = feed * passes_white if white else 0
    if nc + nw > NOZZLES:
        raise ValueError(f"weave needs {nc + nw} nozzles, the head has {NOZZLES}")
    groups = []
    high_first = (NOZZLES - nc, nc)   # printed first
    low_last = (0, nw)                # printed last
    if not colour_first:
        high_first = (NOZZLES - nw, nw); low_last = (0, nc)
    if colour:
        f, n = high_first if colour_first else low_last
        groups.append(Group("colour", list(colour_ch) if colour_ch is not None else ["K", "C", "M", "Y"], f, n, passes_colour))
    if white:
        f, n = low_last if colour_first else high_first
        groups.append(Group("white", list(white_ch) if white_ch is not None else ["LC", "LM"], f, n, passes_white))
    return groups


def simple_groups(feed, passes, channels):
    """All channels share the full head (no split) - for paper test prints."""
    n = feed * passes
    return [Group("all", channels, NOZZLES - n, n, passes)]


class JobWriter:
    def __init__(self, out, geo: JobGeometry):
        self.out = out
        self.geo = geo
        self.res = geo.res
        self.bytes = 0
        self.head_passes = 0

    def w(self, b: bytes):
        self.out.write(b)
        self.bytes += len(b)

    # -------------------------------------------------------------- header
    def header(self):
        r = self.res
        vu = r.vres  # page / vertical units per inch (same as Gutenprint)
        page_len = round(self.geo.page_h_pt * vu / 72)
        page_wid = round(self.geo.page_w_pt * vu / 72)
        top = -vu                                    # allow positioning 1" above
        bottom = page_len + 64 * vu // 72
        self.w(EXIT_PACKET + ESC + b"@")
        self.w(ESC + b"@" + ESC + b"(R\x08\x00\x00REMOTE1"
               + remote("SN", b"\x00")
               + remote("PP", b"\x00\x01\x00")          # rear paper path
               + remote("MI", b"\x00\x01\x00\x00")
               + ESC + b"\x00\x00\x00")
        self.w(esc_paren("G", b"\x01"))
        self.w(esc_paren("U", r.unit_bytes))
        self.w(esc_paren("K", b"\x00\x02"))
        self.w(esc_paren("i", b"\x00"))                 # printer microweave off
        self.w(ESC + b"U" + (b"\x01" if self.geo.unidirectional else b"\x00"))
        self.w(esc_paren("e", bytes([0, r.dot_code])))
        self.w(esc_paren("D", struct.pack("<HBB", 14400, 14400 // 90, 14400 // 720)))
        self.w(esc_paren("C", struct.pack("<I", page_len)))
        self.w(esc_paren("c", struct.pack("<iI", top, bottom)))
        self.w(esc_paren("S", struct.pack("<II", page_wid, page_len)))
        self.w(esc_paren("m", b"\x51"))

    def footer(self):
        self.w(b"\x0c" + ESC + b"@")
        self.w(ESC + b"(R\x08\x00\x00REMOTE1" + remote("LD") + remote("JE", b"\x00")
               + ESC + b"\x00\x00\x00")

    # -------------------------------------------------------------- body
    def passes(self, planes: Dict[str, np.ndarray], groups: List[Group], feed: int,
               mask: np.ndarray, progress: Optional[Callable] = None,
               cancel: Optional[Callable] = None):
        """planes: physical channel -> uint8 dot-code array (rows, cols) at device res."""
        r = self.res
        pitch, H = r.pitch, r.H
        any_plane = next(iter(planes.values()))
        img_h, img_w = any_plane.shape
        builders = {c: BlockBuilder(p, H, mask) for c, p in planes.items()}
        shift = {c: HEAD_OFFSET[c] * r.head_shift for c in EMIT_ORDER}
        for g in groups:
            if g.passes % H:
                raise ValueError(f"{g.name}: passes {g.passes} not a multiple of {H}")
        T = r.vres + round(self.geo.top_pt * r.vres / 72)     # naive row of image row 0
        V0 = T - (NOZZLES - 1) * pitch
        lowest = min(g.first for g in groups)
        p_end = (T + img_h - 1 + r.head_shift - lowest * pitch - V0) // feed + 1
        X0 = round((self.geo.left_pt - 9.0) * r.hunit / 72)
        hstep = r.hunit // r.hres
        cur_v = 0
        nozzle = np.arange(NOZZLES, dtype=np.int64)
        group_of = {c: g for g in groups for c in g.channels}
        for p in range(p_end + 1):
            if cancel and cancel():
                return False
            vpos = V0 + p * feed
            printed = False
            q = p // pitch
            phase = q % H
            for c in EMIT_ORDER:
                g = group_of.get(c)
                if g is None or c not in builders:
                    continue
                rows = vpos + nozzle * pitch - shift[c] - T
                inside = (nozzle >= g.first) & (nozzle < g.first + g.count) & (rows >= 0) & (rows < img_h)
                if not inside.any():
                    continue
                row_idx = np.where(inside, rows, -1).astype(np.int32)
                K = g.passes // H
                m = (q // H) % K
                payload, nrows = builders[c].build(row_idx, phase, K, m)
                if not nrows:
                    continue
                if vpos != cur_v:
                    self.w(esc_paren("v", struct.pack("<i", vpos - cur_v)))
                    cur_v = vpos
                self.w(esc_paren("$", struct.pack("<i", X0 + phase * hstep)))
                nb = builders[c].nb
                self.w(ESC + b"i" + bytes([COLOR_CODE[c], 1, 2]) + struct.pack("<HH", nb, nrows))
                self.w(payload)
                self.w(b"\r")
                printed = True
            if printed:
                self.head_passes += 1
            if progress and p % 16 == 0:
                progress(p / (p_end + 1))
        if progress:
            progress(1.0)
        return True


def dot_mask(seed=11):
    """64x64 random mask used to share dots between multiple passes."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(64, 64), dtype=np.uint8)


# ---------------------------------------------------------------- maintenance
def maint_job(*remote_cmds: bytes) -> bytes:
    """A maintenance job in the same framing escputil uses."""
    body = EXIT_PACKET + ESC + b"@"
    for c in remote_cmds:
        body += ESC + b"@" + ESC + b"(R\x08\x00\x00REMOTE1" + c + ESC + b"\x00\x00\x00" + ESC + b"\x00"
    return body + b"\x0c\x1b\x00\x1b\x00"


def cmd_head_clean(power=False, group=0):
    return maint_job(remote("CH", bytes([0, group | (0x10 if power else 0)])))


def cmd_nozzle_check():
    body = EXIT_PACKET + ESC + b"@" + ESC + b"@" + ESC + b"(R\x08\x00\x00REMOTE1"
    body += remote("VI", b"\x00\x00") + remote("NC", b"\x00\x10") + remote("NC", b"\x00\x00")
    body += ESC + b"\x00\x00\x00" + ESC + b"\x00"
    return body + b"\x0c\x1b\x00\x1b\x00"


def cmd_align_print(passes=4):
    return maint_job(*[remote("DT", bytes([0, i, 0])) for i in range(passes)])


def cmd_align_set(choices):
    """choices: list of best-pair numbers (1-based), one per alignment pattern."""
    cmds = [remote("DA", bytes([0, i, 0, int(c)])) for i, c in enumerate(choices)]
    return maint_job(*cmds)


def cmd_align_save():
    return maint_job(remote("SV"))
