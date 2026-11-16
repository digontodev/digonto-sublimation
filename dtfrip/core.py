"""Bridge to the C inner loops (libdtfcore.so), with slow pure-Python fallbacks."""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SRC = HERE / "csrc" / "dtfcore.c"

_lib = None


def _tag():
    import platform
    sysn = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x86_64"
    ext = {"windows": ".dll", "macos": ".dylib"}.get(sysn, ".so")
    return f"{sysn}-{arch}", ext


def _cache_dir():
    from . import config
    return config.data_dir() / "lib"


def _candidates():
    tag, ext = _tag()
    yield HERE / "lib" / f"libdtfcore-{tag}{ext}"        # shipped with the program
    yield _cache_dir() / f"libdtfcore-{tag}{ext}"        # compiled on this computer


def build(dest=None, quiet=True):
    """Compile the C helper with whatever C compiler is installed. Returns the path or None."""
    import shutil
    tag, ext = _tag()
    dest = Path(dest) if dest else _cache_dir() / f"libdtfcore-{tag}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    for cc in ("cc", "gcc", "clang"):
        exe = shutil.which(cc)
        if not exe:
            continue
        for extra in (["-march=x86-64-v2"] if tag.endswith("x86_64") and sys.platform != "darwin" else [], []):
            cmd = [exe, "-O3", *extra, "-shared"] + ([] if sys.platform == "win32" else ["-fPIC"]) + ["-o", str(dest), str(SRC)]
            try:
                subprocess.run(cmd, check=True, capture_output=quiet)
                return dest
            except Exception:
                continue
    return None


def lib():
    global _lib
    if _lib is not None:
        return _lib or None
    for c in _candidates():
        if c.exists():
            try:
                _lib = ctypes.CDLL(str(c)); break
            except OSError:
                pass
    if _lib is None:
        p = build()
        if p:
            try:
                _lib = ctypes.CDLL(str(p))
            except OSError:
                _lib = None
    if _lib is None:
        _lib = False
        print("dtfrip: C helper not available, using the slower fallback (print quality is lower)", file=sys.stderr)
        return None
    P = ctypes.c_void_p; I = ctypes.c_int; F = ctypes.c_float; U = ctypes.c_uint32
    _lib.ed_strip.argtypes = [P, I, I, I, P, I, P, P, P, I, I, I, F, U]
    _lib.packbits.argtypes = [P, I, P]; _lib.packbits.restype = I
    _lib.build_block.argtypes = [P, I, I, I, P, I, I, I, I, P, I, I, P, P, P]
    _lib.build_block.restype = I
    _lib.erode_rows.argtypes = [P, I, I, I, P]
    return _lib


def _ptr(a):
    return a.ctypes.data_as(ctypes.c_void_p)


# --------------------------------------------------------------------------
class Halftoner:
    """Error diffusion for one channel, fed strip by strip."""

    def __init__(self, width, levels, codes, mirror=False, noise=0.03, seed=1):
        self.w = width
        self.levels = np.ascontiguousarray(levels, dtype=np.float32)
        self.codes = np.ascontiguousarray(codes, dtype=np.uint8)
        self.err = np.zeros(2 * (width + 4), dtype=np.float32)
        self.mirror = int(bool(mirror))
        self.noise = float(noise)
        self.seed = seed
        self.row = 0
        self.L = lib()

    def run(self, strip, out):
        """strip: float32 (rows, w) ; out: uint8 (rows, w) view (may be memmap)."""
        strip = np.ascontiguousarray(strip, dtype=np.float32)
        rows = strip.shape[0]
        if self.L:
            tmp = out if (out.flags.c_contiguous and out.dtype == np.uint8) else np.empty_like(strip, dtype=np.uint8)
            self.L.ed_strip(_ptr(strip), self.w, rows, self.w, _ptr(tmp), tmp.strides[0],
                            _ptr(self.err), _ptr(self.levels), _ptr(self.codes), len(self.levels),
                            self.row, self.mirror, self.noise, self.seed)
            if tmp is not out:
                out[:] = tmp
        else:
            out[:] = _ordered_fallback(strip, self.levels, self.codes, self.row, self.mirror)
        self.row += rows


_BAYER = None


def _ordered_fallback(strip, levels, codes, row0, mirror):
    """Vectorised multi-level dither (used only without the C helper)."""
    global _BAYER
    if _BAYER is None:
        rng = np.random.default_rng(7)
        _BAYER = rng.random((64, 64)).astype(np.float32)
    h, w = strip.shape
    t = _BAYER[(np.arange(h)[:, None] + row0 + (32 if mirror else 0)) % 64, np.arange(w)[None, :] % 64]
    out = np.zeros(strip.shape, np.uint8)
    for k in range(1, len(levels)):
        lo, hi = levels[k - 1], levels[k]
        frac = np.clip((strip - lo) / max(hi - lo, 1e-6), 0, 1)
        sel = (strip > lo) & (frac > t)
        out[sel] = codes[k]
        sel_lo = (strip > lo) & ~(frac > t)
        if k > 1:
            out[sel_lo] = codes[k - 1]
    out[strip <= 0] = 0
    return out


# --------------------------------------------------------------------------
def packbits(row: bytes) -> bytes:
    L = lib()
    src = np.frombuffer(bytes(row), dtype=np.uint8)
    if L:
        dst = np.empty(len(src) * 2 + 16, np.uint8)
        n = L.packbits(_ptr(src), len(src), _ptr(dst))
        return dst[:n].tobytes()
    return _packbits_py(bytes(row))


def _packbits_py(s):
    out = bytearray(); i = 0; n = len(s)
    while i < n:
        run = 1
        while i + run < n and run < 128 and s[i + run] == s[i]:
            run += 1
        if run >= 3 or (run == 2 and i + run == n):
            out += bytes([257 - run, s[i]]); i += run; continue
        start = i; lit = 0
        while i < n and lit < 128:
            if i + 2 < n and s[i] == s[i + 1] == s[i + 2]:
                break
            i += 1; lit += 1
        out.append(lit - 1); out += s[start:start + lit]
    return bytes(out)


class BlockBuilder:
    """Builds the RLE payload of one ESC i command from a dot-code plane."""

    def __init__(self, plane, H, mask):
        self.plane = plane  # np.ndarray / memmap uint8 (h, w)
        self.h, self.w = plane.shape
        self.H = H
        self.ncols = (self.w + H - 1) // H
        self.nb = (self.ncols + 3) // 4
        self.mask = np.ascontiguousarray(mask, dtype=np.uint8)
        self.L = lib()
        self.out = np.empty(200 * (self.nb * 2 + 16), np.uint8)
        self.tmp = np.empty(self.nb + 8, np.uint8)
        self.used = ctypes.c_int(0)
        self._ptr_plane = _ptr(plane) if self.L else None

    def build(self, row_idx, phase, K, m):
        """row_idx: int32 array (one entry per nozzle). Returns (payload, rows) or (b'', 0)."""
        row_idx = np.ascontiguousarray(row_idx, dtype=np.int32)
        if self.L:
            n = self.L.build_block(self._ptr_plane, self.w, self.h, self.plane.strides[0],
                                   _ptr(row_idx), len(row_idx), phase, self.H, self.ncols,
                                   _ptr(self.mask), K, m, _ptr(self.out),
                                   ctypes.byref(self.used), _ptr(self.tmp))
            if n == 0:
                return b"", 0
            return self.out[:n].tobytes(), self.used.value
        return self._build_py(row_idx, phase, K, m)

    def _build_py(self, row_idx, phase, K, m):
        rows = []
        last = -1
        cols = np.arange(self.ncols)
        xs = phase + cols * self.H
        valid = xs < self.w
        for j, y in enumerate(row_idx):
            lv = np.zeros(self.ncols, np.uint8)
            if 0 <= y < self.h:
                lv[valid] = self.plane[y, xs[valid]]
                if K > 1:
                    lv[(self.mask[y & 63, cols & 63] % K) != m] = 0
            if lv.any():
                last = j
            rows.append(lv)
        if last < 0:
            return b"", 0
        payload = bytearray()
        for lv in rows[:last + 1]:
            pad = np.zeros(self.nb * 4, np.uint8); pad[:len(lv)] = lv
            q = pad.reshape(-1, 4)
            packed = (q[:, 0] << 6) | (q[:, 1] << 4) | (q[:, 2] << 2) | q[:, 3]
            payload += _packbits_py(packed.astype(np.uint8).tobytes())
        return bytes(payload), last + 1


def erode(mask_u8, r):
    """Square erosion (white choke). mask_u8: uint8 2-D array, modified copy returned."""
    if r <= 0:
        return mask_u8
    img = np.ascontiguousarray(mask_u8.copy())
    L = lib()
    if L:
        tmp = np.empty(max(img.shape) + 8, np.uint8)
        L.erode_rows(_ptr(img), img.shape[1], img.shape[0], int(r), _ptr(tmp))
        return img
    p = np.pad(img, r, constant_values=0)
    out = p.copy()
    for d in range(1, r + 1):
        out[:, :-d] = np.minimum(out[:, :-d], p[:, d:])
        out[:, d:] = np.minimum(out[:, d:], p[:, :-d])
    p = out.copy()
    for d in range(1, r + 1):
        out[:-d, :] = np.minimum(out[:-d, :], p[d:, :])
        out[d:, :] = np.minimum(out[d:, :], p[:-d, :])
    return out[r:-r, r:-r]
