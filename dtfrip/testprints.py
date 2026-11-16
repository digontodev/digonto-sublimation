"""Built-in test prints: channel check, ink density chart, keep-alive stripe."""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .modes import PRESETS
from .rip import MM, SUB, clean_slots, WORK_DPI, RipSettings, Separation, rip_separation

PX = WORK_DPI / MM   # pixels per mm at work resolution


def _font(size_px):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(f, int(size_px))
        except OSError:
            pass
    return ImageFont.load_default(size=int(size_px))


def _text_mask(w, h, text, size_mm):
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    f = _font(size_mm * PX)
    d.text((w / 2, h / 2), text, fill=255, font=f, anchor="mm")
    return np.asarray(im)


def _sep(cmyk, white, width_mm, top_mm, page=(210.0, 297.0), left_mm=None):
    h, w = white.shape
    left = left_mm if left_mm is not None else (page[0] - w / PX) / 2
    preview = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return Separation(cmyk, white, (left, top_mm), page, preview, False)


def _absolute(s: RipSettings):
    """Settings where 1.0 in the planes means 100 % dots (ignore preset limits)."""
    pr = PRESETS[s.preset]
    s.colour_amount = 1.0 / pr.colour_limit
    s.white_amount = 1.0 / pr.white_limit
    s.total_ink_limit = 4.0
    s.density = {k: 1.0 for k in ("C", "M", "Y", "K", "W1", "W2", "LC", "LM")}
    s.curve = dict(s.density)
    return s


def channel_check(s: RipSettings, out_path, top_mm=5.0, progress=None):
    """Six labelled blocks, each printed by exactly one channel.

    Use it to confirm the ink order (Y K W W M C) really matches the channels.
    White blocks are only visible against something dark.
    """
    s = _absolute(RipSettings.from_dict(vars(s)))
    s.mirror = False
    names = ([n for n in ("C", "M", "Y", "K", "LC", "LM") if n in clean_slots(s.slots).values()]) if SUB else ["C", "M", "Y", "K", "W1", "W2"]
    bw, bh = int(28 * PX), int(20 * PX)
    gap = int(3 * PX)
    W = len(names) * bw + (len(names) - 1) * gap
    cmyk = np.zeros((bh, W, 4), np.uint8)
    white = np.zeros((bh, W), np.uint8)
    w1 = np.zeros((bh, W), np.uint8); w2 = np.zeros((bh, W), np.uint8)
    for i, n in enumerate(names):
        x = i * (bw + gap)
        block = np.full((bh, bw), 200, np.uint8)            # 80 % ink
        label = _text_mask(bw, bh, n, 9)
        block[label > 128] = 0                              # name knocked out
        if SUB and n in ("LC", "LM"):                       # low amount = only the light ink prints
            cmyk[:, x:x + bw, 0 if n == "LC" else 1] = (block * 0.5).astype(np.uint8)
        elif SUB and n in "CMYK":
            cmyk[:, x:x + bw, "CMYK".index(n)] = np.where(block > 0, 255, 0).astype(np.uint8)
        elif n in "CMYK":
            cmyk[:, x:x + bw, "CMYK".index(n)] = block
        elif n == "W1":
            w1[:, x:x + bw] = block
        else:
            w2[:, x:x + bw] = block
    if SUB:
        s.white_mode = "off"
        sep = _sep(cmyk, w1, W / PX, top_mm)
        return rip_separation(sep, s, out_path, progress)
    s.white_w1 = True; s.white_w2 = True; s.white_mode = "soft"
    sep = _sep(cmyk, w1, W / PX, top_mm)
    sep.white2 = w2
    return rip_separation(sep, s, out_path, progress)


def density_chart(s: RipSettings, out_path, top_mm=5.0, progress=None):
    """Rows of patches for every channel at 20 %...100 % dot coverage."""
    s = _absolute(RipSettings.from_dict(vars(s)))
    s.mirror = False
    steps = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    names = ["C", "M", "Y", "K"] if SUB else ["C", "M", "Y", "K", "W"]
    pw, ph = int(18 * PX), int(10 * PX)
    gap = int(2 * PX); lab = int(10 * PX)
    W = lab + len(steps) * (pw + gap)
    H = len(names) * (ph + gap) + int(6 * PX)
    cmyk = np.zeros((H, W, 4), np.uint8)
    white = np.zeros((H, W), np.uint8)
    for r, n in enumerate(names):
        y = int(6 * PX) + r * (ph + gap)
        for i, v in enumerate(steps):
            x = lab + i * (pw + gap)
            val = int(round(v * 255))
            if n == "W":
                white[y:y + ph, x:x + pw] = val
            else:
                cmyk[y:y + ph, x:x + pw, "CMYK".index(n)] = val
        # channel name in black at the left
        m = _text_mask(lab, ph, n, 6)
        cmyk[y:y + ph, 0:lab, 3] = np.maximum(cmyk[y:y + ph, 0:lab, 3], m)
    for i, v in enumerate(steps):           # % labels on top
        x = lab + i * (pw + gap)
        m = _text_mask(pw, int(5 * PX), f"{int(v * 100)}%", 3.5)
        cmyk[0:int(5 * PX), x:x + pw, 3] = m
    s.white_mode = "off" if SUB else "soft"
    sep = _sep(cmyk, white, W / PX, top_mm)
    return rip_separation(sep, s, out_path, progress)


def keepalive_stripe(s: RipSettings, out_path, height_mm=4.0, white_only=False, amount=0.35,
                     top_mm=3.0):
    """A thin stripe that pushes a little ink through every channel."""
    s = _absolute(RipSettings.from_dict(vars(s)))
    s.preset = "draft"
    s = _absolute(s)
    s.mirror = False
    W = int(180 * PX); H = max(int(height_mm * PX), 8)
    cmyk = np.zeros((H, W, 4), np.uint8)
    white = np.zeros((H, W), np.uint8)
    val = int(round(amount * 255))
    if SUB:
        seg = W // 6
        for i, (ch, v) in enumerate(((0, .35), (1, .35), (2, .3), (3, .3), (0, .55), (1, .55))):
            cmyk[:, i * seg:(i + 1) * seg, ch] = int(round(v * 255))
        s.white_mode = "off"
        return rip_separation(_sep(cmyk, white, W / PX, top_mm), s, out_path)
    if white_only:
        white[:] = val
    else:
        seg = W // 5
        for i in range(4):
            cmyk[:, i * seg:(i + 1) * seg, i] = val
        white[:, 4 * seg:] = val
    s.white_mode = "soft"; s.white_w1 = True; s.white_w2 = True
    sep = _sep(cmyk, white, W / PX, top_mm)
    return rip_separation(sep, s, out_path)
