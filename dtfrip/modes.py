"""Printer model data (Epson L800 = Gutenprint "Artisan 50", model 97) and print modes.

All numbers here were taken from Gutenprint's description of this print head
and verified against Gutenprint's own output (see tests/).
"""
from dataclasses import dataclass, field

NOZZLES = 90             # nozzles per colour row
NOZZLE_DPI = 90          # nozzle pitch: 1/90 inch
HEAD_OFFSET_360 = 2      # some rows sit 2/360" lower on the head (LC, M, Y)
BASE_H = 720             # horizontal dots per inch in one pass

# ESC i colour codes and physical channel order of the L800 head
# (left -> right as seen from the front:  Y  K  LC  LM  M  C)
COLOR_CODE = {"K": 0x00, "C": 0x02, "LC": 0x12, "M": 0x01, "LM": 0x11, "Y": 0x04}
HEAD_OFFSET = {"K": 0, "C": 0, "LC": 1, "M": 1, "LM": 0, "Y": 1}  # 1 = offset row
EMIT_ORDER = ["K", "C", "LC", "M", "LM", "Y"]   # same order Gutenprint sends

# Your DTF ink set: Y K W W M C  -> W1 sits in the LC slot, W2 in the LM slot
DTF_INKS = {"C": "C", "M": "M", "Y": "Y", "K": "K", "W1": "LC", "W2": "LM"}


@dataclass(frozen=True)
class Resolution:
    name: str
    vres: int
    hres: int
    unit_bytes: bytes     # ESC ( U arguments
    dot_code: int         # ESC ( e  ink/dot type
    drops: tuple          # relative drop sizes: small, medium, large

    @property
    def pitch(self):      # rows between two nozzles
        return self.vres // NOZZLE_DPI

    @property
    def H(self):          # horizontal interleave (passes needed for full width)
        return self.hres // BASE_H

    @property
    def hunit(self):      # ESC ( $ units per inch
        return 5760 // self.unit_bytes[2]

    @property
    def head_shift(self):  # rows for the offset nozzle rows
        return HEAD_OFFSET_360 * self.vres // 360


RESOLUTIONS = {
    "720x720":   Resolution("720x720", 720, 720, bytes([8, 8, 8, 0x80, 0x16]), 0x21, (0.13, 0.58, 1.0)),
    "1440x720":  Resolution("1440x720", 720, 1440, bytes([8, 8, 4, 0x80, 0x16]), 0x21, (0.13, 0.58, 1.0)),
    "1440x1440": Resolution("1440x1440", 1440, 1440, bytes([4, 4, 2, 0x80, 0x16]), 0x25, (0.5, 0.6, 1.0)),
    "2880x1440": Resolution("2880x1440", 1440, 2880, bytes([4, 4, 2, 0x80, 0x16]), 0x25, (0.5, 0.6, 1.0)),
}


@dataclass
class Preset:
    label: str
    resolution: str
    feed: int             # paper advance per pass, in rows (must be odd)
    passes: int           # passes over every row, per nozzle group
    colour_limit: float   # max ink per colour channel (1.0 = every pixel a large dot)
    white_limit: float    # max ink per white channel
    unidirectional: bool
    note: str = ""


PRESETS = {
    "draft":    Preset("Draft (fast, check layout)", "720x720", 43, 1, 0.85, 1.00, False,
                       "720 dpi, 1 pass. Visible banding is normal."),
    "standard": Preset("Standard", "1440x720", 21, 2, 0.70, 0.95, False,
                       "1440x720 dpi, 2 passes per row."),
    "high":     Preset("High", "1440x1440", 11, 4, 0.45, 0.70, True,
                       "1440x1440 dpi, 4 passes, unidirectional."),
    "max":      Preset("Maximum", "1440x1440", 7, 6, 0.45, 0.70, True,
                       "1440x1440 dpi, 6 passes, unidirectional. Slowest, smoothest."),
}


def check_weave(feed, passes, res: Resolution, groups_nozzles):
    """Validate a weave. Returns list of problems (empty = ok)."""
    from math import gcd
    probs = []
    if gcd(feed, res.pitch) != 1:
        probs.append(f"feed {feed} must not share a factor with nozzle pitch {res.pitch} (use an odd number)")
    if passes % res.H:
        probs.append(f"passes ({passes}) must be a multiple of {res.H} at {res.name}")
    if groups_nozzles > NOZZLES:
        probs.append(f"needs {groups_nozzles} nozzles but the head has {NOZZLES}")
    return probs
