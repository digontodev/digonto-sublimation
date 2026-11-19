"""Digonto DTFRIP window (Qt). The engine runs inside the program; the window also does the white-ink keep-alive."""
import json
import os
import sys
import threading
import time
import uuid

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QTabBar, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QLayout, QListWidget, QListWidgetItem, QFileDialog, QFormLayout, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QSplitter, QTabWidget, QTableWidget, QVBoxLayout, QWidget)

from . import __version__, backend
from .flavor import FLAVOR, SLUG, TAGLINE, TITLE

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import digonto_ui as ui  # noqa: E402

BUSY = ("layout", "dots", "printfile", "printing")
INK_COL = {"Yellow": "#F5D90A", "Black": "#222222", "White 1": "#E8E8E8", "White 2": "#E8E8E8", "Magenta": "#D6248F", "Cyan": "#1AA6D9"}


class Bridge(QObject):
    done = Signal(object, object)       # result, error


class Api:
    """The window talks to the engine (backend.py) directly - no server, no network."""

    def __init__(self, *_):
        pass

    def req(self, path, data=None, raw=None, headers=None, timeout=60, want_headers=False):
        from urllib.parse import parse_qs, urlsplit
        u = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p == "/api/preview.png":
                res = backend.preview(q.get("view", "colour"))
            elif p.startswith("/api/driver_image."):
                res = backend.driver_image(p.rsplit(".", 1)[1], q.get("dpi"))
            elif p == "/api/layout":
                res = (json.dumps(backend.layout_boxes()).encode(), {})
            elif p.startswith("/api/download/"):
                res = backend.download(p.rsplit("/", 1)[1])
            else:
                res = (json.dumps(self._json(p, data or {})).encode(), {})
        except backend.EngineError as e:
            raise RuntimeError(str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(str(e) or e.__class__.__name__) from None
        return res if want_headers else res[0]

    def _json(self, p, d):
        if p == "/api/state":
            return backend.state()
        if p == "/api/items":
            return backend.set_items(d)
        if p == "/api/align":
            return backend.align_item(d)
        if p == "/api/layout":
            return backend.layout_boxes()
        if p == "/api/place":
            return backend.place(d)
        if p == "/api/nudge":
            return backend.nudge(d)
        if p == "/api/place":
            return backend.place(d)
        if p == "/api/nudge":
            return backend.nudge(d)
        if p == "/api/clear":
            return backend.clear_items()
        if p == "/api/settings":
            return backend.set_settings(d)
        if p == "/api/print":
            return backend.do_print(d.get("send", True))
        if p.startswith("/api/test/"):
            return backend.do_test(p.rsplit("/", 1)[1], d.get("send", True))
        if p == "/api/cancel":
            return backend.cancel()
        if p.startswith("/api/maint/"):
            return backend.maint(p.rsplit("/", 1)[1], d)
        if p == "/api/config":
            return backend.set_config(d)
        raise RuntimeError(f"unknown request {p}")

    def json(self, path, data=None, **kw):
        out = self.req(path, data, **kw)
        return json.loads(out.decode()) if out else {}

    def upload(self, path, field, filepath):
        try:
            if path == "/api/profile":
                return backend.upload_profile(filepath)
            return backend.upload(filepath)
        except backend.EngineError as e:
            raise RuntimeError(str(e)) from None


def run_async(bridge_holder, fn, cb):
    """run fn() in a thread, call cb(result, error) in the window thread"""
    b = Bridge()
    bridge_holder.add(b)

    def finish(res, err):
        bridge_holder.discard(b)
        cb(res, err)
    b.done.connect(finish)

    def work():
        try:
            b.done.emit(fn(), None)
        except Exception as e:  # noqa: BLE001
            b.done.emit(None, str(e))
    threading.Thread(target=work, daemon=True).start()



def fit_column(root):
    """the right panel is a fixed 250 px column: buttons, drop-downs and fields give way so nothing is cut off"""
    from PySide6.QtWidgets import QComboBox as _CB, QLineEdit as _LE, QPushButton as _PB, QSizePolicy as _SP
    for b in root.findChildren(_PB):
        if b.maximumWidth() < 100 and b.maximumWidth() == b.minimumWidth():
            continue
        p = b.sizePolicy()
        p.setHorizontalPolicy(_SP.Ignored)
        b.setSizePolicy(p)
        if b.text():
            b.setMinimumWidth(min(70, b.fontMetrics().horizontalAdvance(b.text()) + 16))
    for c in root.findChildren(_CB):
        c.setSizeAdjustPolicy(_CB.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(4)
        c.setMinimumWidth(40)
    for e in root.findChildren(_LE):
        e.setMinimumWidth(40)


def tool_icon(kind, size=22):
    """small drawn icons for the layout buttons (rotate and the six alignments)"""
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 22.0, size / 22.0)
    ink, acc = QColor("#4A1F6E"), QColor("#C2395C")
    p.setPen(QPen(ink, 1.5))
    if kind == "rotate":
        from PySide6.QtCore import QRectF
        p.drawArc(QRectF(4, 4, 14, 14), 40 * 16, 280 * 16)
        p.setBrush(ink)
        from PySide6.QtGui import QPolygonF
        from PySide6.QtCore import QPointF
        p.drawPolygon(QPolygonF([QPointF(15.5, 2.5), QPointF(19.5, 7.5), QPointF(12.5, 7.5)]))
    else:
        from PySide6.QtCore import QRectF
        p.setBrush(QColor("#FFFFFF"))
        p.drawRect(QRectF(2.5, 2.5, 17, 17))
        p.setPen(Qt.NoPen)
        p.setBrush(acc)
        pos = {"lt": (4, 4), "rt": (12, 4), "hc": (8, 4), "vc": (4, 8), "c": (8, 8)}.get(kind)
        if kind == "auto":
            p.setPen(QPen(acc, 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(5, 5, 5, 5)); p.drawRect(QRectF(12, 5, 5, 5)); p.drawRect(QRectF(5, 12, 5, 5)); p.drawRect(QRectF(12, 12, 5, 5))
        else:
            p.drawRect(QRectF(pos[0], pos[1], 6, 6))
            p.setPen(QPen(acc, 1, Qt.DashLine))
            if kind == "hc":
                p.drawLine(11, 1, 11, 21)
            elif kind == "vc":
                p.drawLine(1, 11, 21, 11)
            elif kind == "c":
                p.drawLine(11, 1, 11, 21); p.drawLine(1, 11, 21, 11)
    p.end()
    return QIcon(pm)

def dspin(lo, hi, step, dec=2, w=90):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(dec)
    s.setFixedWidth(w)
    s.setKeyboardTracking(False)
    return s


def ispin(lo, hi, step=1, w=90):
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setFixedWidth(w)
    s.setKeyboardTracking(False)
    return s


class DropTable(QTableWidget):
    def __init__(self, on_files):
        super().__init__(0, 5)
        self.on_files = on_files
        self.setHorizontalHeaderLabels(["Artwork", "Width mm", "Copies", "Rotate", ""])
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.verticalHeader().setVisible(False)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        self.on_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])


INK_LABEL = {"K": "Black", "C": "Cyan", "M": "Magenta", "Y": "Yellow", "LC": "Light cyan", "LM": "Light magenta",
             "W1": "White 1", "W2": "White 2", "-": "Off"}
INK_RGB = {"K": "#222222", "C": "#1AA6D9", "M": "#D6248F", "Y": "#F5D90A", "LC": "#8ED8F2", "LM": "#F29BCB",
           "W1": "#F4F4F4", "W2": "#E0E0E0", "-": "#B8B0C8"}
THUMB_SIZE = 64


def ink_icon(ink, size=14):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(INK_RGB.get(ink, "#B8B0C8")))
    p.setPen(QPen(QColor("#6B5C86"), 1))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


def item_thumb(path, size=THUMB_SIZE):
    """small picture of an artwork file (pictures only; other types get a plain tile)"""
    pm = QPixmap(size, size)
    pm.fill(QColor("#EDE6F4"))
    rd = QImageReader(path)
    rd.setAutoTransform(True)
    sz = rd.size()
    if sz.isValid():
        k = min(size / max(sz.width(), 1), size / max(sz.height(), 1))
        rd.setScaledSize(QSize(max(1, int(sz.width() * k)), max(1, int(sz.height() * k))))
        img = rd.read()
        if not img.isNull():
            p = QPainter(pm)
            p.fillRect(0, 0, size, size, QColor("#FFFFFF"))
            p.drawImage((size - img.width()) // 2, (size - img.height()) // 2, img)
            p.end()
            return pm
    p = QPainter(pm)
    p.setPen(QColor("#6B5C86"))
    p.drawText(pm.rect(), Qt.AlignCenter, os.path.splitext(path)[1][1:].upper() or "FILE")
    p.end()
    return pm


class Section(QWidget):
    """a collapsible group with a title bar (used in the sublimation job properties)"""

    def __init__(self, title, body, opened=True):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.head = QPushButton(("v  " if opened else ">  ") + title)
        self.head.setStyleSheet("QPushButton{text-align:left; background:#4A1F6E; color:white; border:none; border-radius:0;"
                                "padding:7px 10px; font-weight:600;} QPushButton:hover{background:#5A2A80;}")
        self.title = title
        self.body = body
        body.setVisible(opened)
        self.head.clicked.connect(self.toggle)
        lay.addWidget(self.head)
        lay.addWidget(body)

    def toggle(self):
        on = not self.body.isVisible()
        self.body.setVisible(on)
        self.head.setText(("v  " if on else ">  ") + self.title)


class CanvasView(QWidget):
    """the page with rulers in centimetres; artwork can be dragged to any place"""
    RULER = 20
    moved = Signal(int, float, float)       # artwork index, x mm, y mm (top left on the page)
    picked = Signal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(200, 200)
        self.setMouseTracking(True)
        self.pix = None
        self.dpi = 40.0
        self.page_mm = (210.0, 297.0)
        self.boxes = []
        self.sel = -1
        self.drag = None                    # (index, grab dx mm, grab dy mm, cur x mm, cur y mm)

    def set_pixmap(self, pm, dpi=40.0):
        self.pix, self.dpi = pm, dpi
        self.update()

    def set_boxes(self, page, boxes):
        self.page_mm, self.boxes = tuple(page), list(boxes)
        self.update()

    def set_selected(self, i):
        self.sel = i
        self.update()

    def _page_rect(self):
        r = self.rect().adjusted(self.RULER + 14, self.RULER + 14, -14, -14)
        if not self.pix or r.width() < 10 or r.height() < 10:
            return None, 1.0
        k = min(r.width() / self.pix.width(), r.height() / self.pix.height())
        w, h = int(self.pix.width() * k), int(self.pix.height() * k)
        return QRect(r.left(), r.top(), w, h), k

    def _to_mm(self, pt, pr):
        return ((pt.x() - pr.left()) * self.page_mm[0] / max(1, pr.width()),
                (pt.y() - pr.top()) * self.page_mm[1] / max(1, pr.height()))

    def _box_rect(self, b, pr, x=None, y=None):
        sx, sy = pr.width() / self.page_mm[0], pr.height() / self.page_mm[1]
        x = b["x"] if x is None else x
        y = b["y"] if y is None else y
        return QRect(int(pr.left() + x * sx), int(pr.top() + y * sy), max(2, int(b["w"] * sx)), max(2, int(b["h"] * sy)))

    def _hit(self, pt, pr):
        for b in reversed(self.boxes):
            if self._box_rect(b, pr).contains(pt):
                return b
        return None

    def mousePressEvent(self, e):
        pr, _ = self._page_rect()
        if pr is None or e.button() != Qt.LeftButton:
            return
        b = self._hit(e.position().toPoint(), pr)
        if b:
            mx, my = self._to_mm(e.position().toPoint(), pr)
            self.drag = [b["index"], mx - b["x"], my - b["y"], b["x"], b["y"], b]
            self.sel = b["index"]
            self.picked.emit(b["index"])
            self.setCursor(Qt.ClosedHandCursor)
            self.update()

    def mouseMoveEvent(self, e):
        pr, _ = self._page_rect()
        if pr is None:
            return
        pt = e.position().toPoint()
        if self.drag:
            mx, my = self._to_mm(pt, pr)
            b = self.drag[5]
            self.drag[3] = min(max(0.0, mx - self.drag[1]), max(0.0, self.page_mm[0] - b["w"]))
            self.drag[4] = min(max(0.0, my - self.drag[2]), max(0.0, self.page_mm[1] - b["h"]))
            self.update()
        else:
            self.setCursor(Qt.OpenHandCursor if self._hit(pt, pr) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self.drag:
            i, _, _, x, y, b = self.drag
            self.drag = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            if abs(x - b["x"]) > 0.3 or abs(y - b["y"]) > 0.3:
                self.moved.emit(i, round(x, 2), round(y, 2))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#D9D2E6"))
        pr, k = self._page_rect()
        R = self.RULER
        p.fillRect(0, 0, self.width(), R, QColor("#EDE6F4"))
        p.fillRect(0, 0, R, self.height(), QColor("#EDE6F4"))
        p.setPen(QColor("#9C8FB5"))
        p.drawLine(0, R, self.width(), R)
        p.drawLine(R, 0, R, self.height())
        p.setPen(QColor("#4A3A66"))
        f = p.font()
        f.setPixelSize(10)
        p.setFont(f)
        p.drawText(2, 13, "cm")
        if pr is None:
            p.setPen(QColor("#6B5C86"))
            p.drawText(self.rect(), Qt.AlignCenter, "Add artwork to see the page")
            return
        # page with shadow
        p.fillRect(pr.translated(4, 4), QColor(60, 40, 90, 60))
        p.drawPixmap(pr, self.pix)
        p.setPen(QPen(QColor("#6B5C86"), 1))
        p.drawRect(pr.adjusted(0, 0, -1, -1))
        for b in self.boxes:
            if b["index"] == self.sel:
                p.setPen(QPen(QColor("#C2395C"), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(self._box_rect(b, pr))
        if self.drag is not None:
            _, _, _, x, y, b = self.drag
            p.setPen(QPen(QColor("#FF6F8E"), 2, Qt.DashLine))
            p.setBrush(QColor(255, 111, 142, 50))
            p.drawRect(self._box_rect(b, pr, x, y))
        # rulers: px per cm on screen
        pxcm = k * self.dpi / 2.54
        step = 1
        for cand in (1, 2, 5, 10):
            step = cand
            if pxcm * cand >= 26:
                break
        n_x = int(pr.width() / pxcm) + 1
        n_y = int(pr.height() / pxcm) + 1
        for i in range(0, n_x + 1, 1):
            x = pr.left() + i * pxcm
            if x > pr.right() + 1:
                break
            major = i % step == 0
            p.drawLine(int(x), R - (9 if major else 4), int(x), R)
            if major:
                p.drawText(int(x) + 2, R - 9, str(i))
        for i in range(0, n_y + 1, 1):
            y = pr.top() + i * pxcm
            if y > pr.bottom() + 1:
                break
            major = i % step == 0
            p.drawLine(R - (9 if major else 4), int(y), R, int(y))
            if major:
                p.save()
                p.translate(R - 10, int(y) + 2)
                p.rotate(-90)
                p.drawText(0, 0, str(i))
                p.restore()


class InkChannelDialog(QDialog):
    """Set up to 8 ink channels: which ink in which tank, in which order, and which are switched off."""

    def __init__(self, parent, channels, flavor, inks):
        super().__init__(parent)
        self.setWindowTitle("Ink channels")
        self.flavor = flavor
        self.inks = list(inks)                       # inks this program can print
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        t = QLabel("Choose the ink for each channel (tank 1 to 8, left to right as you look at the printer). "
                   "Use the arrows to change the order. Set a channel to Off if your printer has fewer inks.")
        t.setWordWrap(True)
        lay.addWidget(t)
        from . import rip as _rip
        self.rows = []
        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Channel</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Ink</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Order</b>"), 0, 2, 1, 2)
        chs = _rip.clean_channels(channels)
        for i in range(_rip.N_CHANNELS):
            cb = QComboBox()
            for k in self.inks + ["-"]:
                cb.addItem(ink_icon(k), INK_LABEL[k], k)
            cb.setCurrentIndex(max(0, cb.findData(chs[i])))
            cb.currentIndexChanged.connect(lambda _=0, r=i: self._changed(r))
            up, dn = QPushButton("Up"), QPushButton("Down")
            for b in (up, dn):
                b.setFixedWidth(68)
            up.clicked.connect(lambda _=False, r=i: self._swap(r, r - 1))
            dn.clicked.connect(lambda _=False, r=i: self._swap(r, r + 1))
            grid.addWidget(QLabel(f"  {i + 1}"), i + 1, 0)
            grid.addWidget(cb, i + 1, 1)
            grid.addWidget(up, i + 1, 2)
            grid.addWidget(dn, i + 1, 3)
            self.rows.append(cb)
        lay.addLayout(grid)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Quick set:"))
        presets = ([("4 inks", ["C", "M", "Y", "K"]), ("6 inks (+ light C/M)", ["C", "M", "Y", "K", "LC", "LM"])]
                   if flavor == "sub" else
                   [("Standard", ["Y", "K", "W1", "W2", "M", "C"]), ("Colour only", ["Y", "K", "M", "C"]),
                    ("8 inks", ["Y", "K", "W1", "W2", "M", "C", "LC", "LM"])])
        presets.insert(0, ("Reset all", list(_rip.DEFAULT_CHANNELS)))
        for label, vals in presets:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, v=vals: self.set_all(v))
            pr.addWidget(b)
        pr.addStretch()
        lay.addLayout(pr)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#6B5C86")
        lay.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.prev = self.values()
        self._refresh_note()

    def values(self):
        return [cb.currentData() for cb in self.rows]

    def set_all(self, vals):
        for i, cb in enumerate(self.rows):
            v = vals[i] if i < len(vals) else "-"
            cb.blockSignals(True)
            cb.setCurrentIndex(max(0, cb.findData(v)))
            cb.blockSignals(False)
        self.prev = self.values()
        self._refresh_note()

    def _swap(self, a, b):
        if 0 <= b < len(self.rows):
            va, vb = self.rows[a].currentData(), self.rows[b].currentData()
            for cb, v in ((self.rows[a], vb), (self.rows[b], va)):
                cb.blockSignals(True)
                cb.setCurrentIndex(max(0, cb.findData(v)))
                cb.blockSignals(False)
            self.prev = self.values()
            self._refresh_note()

    def _changed(self, row):
        v = self.rows[row].currentData()
        if v != "-":
            for i, cb in enumerate(self.rows):             # an ink can only be in one channel: the two channels swap inks
                if i != row and cb.currentData() == v:
                    cb.blockSignals(True)
                    cb.setCurrentIndex(max(0, cb.findData(self.prev[row])))
                    cb.blockSignals(False)
        self.prev = self.values()
        self._refresh_note()

    def _refresh_note(self):
        vals = self.values()
        on = [v for v in vals if v != "-"]
        txt = f"{len(on)} ink{'s' if len(on) != 1 else ''} switched on."
        if self.flavor == "dtf":
            txt += " The Epson L800 head has 6 channels, so only channels 1 to 6 are printed."
            if any(v != "-" for v in vals[6:]):
                txt += " <b>Channels 7 and 8 are ignored on this printer.</b>"
        else:
            txt += (" The printer driver decides how the inks are laid out in the printer; the list tells this program which inks "
                    "you have (it then offers amount and curve for exactly these).")
        self.note.setText(txt)


class InfoBox(QWidget):
    """image information of the selected artwork (size in pixels, dpi, colour mode, file size, size in mm)"""

    def __init__(self):
        super().__init__()
        self.lay = QFormLayout(self)
        self.lay.setContentsMargins(10, 8, 10, 8)
        self.vals = {}
        for k in ("Image size", "Resolution", "Mode", "File size", "Original size"):
            lb = QLabel("-")
            self.vals[k] = lb
            self.lay.addRow(k, lb)

    def show_item(self, it):
        if not it:
            for lb in self.vals.values():
                lb.setText("-")
            return
        path = it["path"]
        rd = QImageReader(path)
        sz = rd.size()
        self.vals["Image size"].setText(f"{sz.width()} x {sz.height()} pixel" if sz.isValid() else "vector / PDF")
        mode, dpi = "-", "-"
        if sz.isValid():
            img = QImage(path)
            if not img.isNull():
                mode = "RGBA" if img.hasAlphaChannel() else ("Gray" if img.isGrayscale() else "RGB")
                if img.dotsPerMeterX() > 0:
                    dpi = f"{round(img.dotsPerMeterX() * 0.0254)} DPI"
        self.vals["Resolution"].setText(dpi)
        self.vals["Mode"].setText(mode)
        try:
            self.vals["File size"].setText(f"{os.path.getsize(path) / 1024:.1f} KB")
        except OSError:
            self.vals["File size"].setText("-")
        self.vals["Original size"].setText(f"{it['nat_w']:.1f} x {it['nat_h']:.1f} mm" if it.get("nat_w") else "-")


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.api = Api()
        self.setAcceptDrops(True)
        self.holders = set()
        self.S = None
        self.view = "colour"
        self.filling = False
        self.busy_prev = False
        self.pending_items = QTimer(self, singleShot=True, interval=500, timeout=self.save_items)
        self.pending_settings = QTimer(self, singleShot=True, interval=400, timeout=self.save_settings)
        self.setWindowTitle(f"{TITLE} {__version__}")
        icon = ui.app_icon(HERE, SLUG)
        self.setWindowIcon(icon)
        self.pill = QLabel("...")
        self.pill.setStyleSheet("color:white;background:rgba(255,255,255,0.18);border-radius:9px;padding:3px 10px")
        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_print(), "Print")
        self.tabs.addTab(self.build_maint(), "Maintenance")
        self.tabs.addTab(self.build_settings(), "Settings")
        if FLAVOR == "sub":
            self.tabs.setTabVisible(1, False)          # the L800 head tools are not used here
            self.tabs.addTab(self.build_maint_sub(), "Maintenance")
            QTimer.singleShot(2500, self.weekly_check)
        self.maint_index = self.tabs.count() - 1 if FLAVOR == "sub" else 1
        self.tabs.tabBar().hide()                       # the left bar switches the pages now
        self.rail = ui.Rail(
            [("new", "new", "New job - remove all artwork", self.clear_items),
             ("open", "open", "Add artwork (PNG, JPG, TIFF, PDF, SVG …)", lambda: (self.tabs.setCurrentIndex(0), self.pick_files())),
             ("save", "save", "Save the print file", lambda: self.start_print(False)),
             ("print", "print", "Print", lambda: self.start_print(True)),
             None,
             ("job", "pages", "The print job", lambda: self.rail_page(0)),
             ("maint", "repair", "Maintenance (nozzle check, cleaning …)", lambda: self.rail_page(self.maint_index))],
            [("settings", "settings", "Settings - printer, connection, profiles", lambda: self.rail_page(2))])
        for k in ("job", "maint", "settings"):
            self.rail.button(k).setCheckable(True)
        self.rail_page(0)
        body = QWidget()
        bh = QHBoxLayout(body)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(0)
        bh.addWidget(self.rail)
        bh.addWidget(self.tabs, 1)
        ui.wrap_with_header(self, ui.make_header(TITLE, TAGLINE,
                                                 icon, self.about, self.pill), body)
        for cb in self.findChildren(QComboBox):            # drop-downs may shrink with the panel
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(8)
        ui.fit_window(self, 1280, 860)
        ui.file_menu(self)
        self.timer = QTimer(self, interval=1500, timeout=lambda: self.refresh(False))
        self.timer.start()
        if FLAVOR == "dtf":                             # white ink must not dry out: print a small stripe now and then
            self.ka_timer = QTimer(self, interval=300000, timeout=lambda: threading.Thread(
                target=backend.keepalive_tick, daemon=True).start())
            self.ka_timer.start()
        self.refresh(True)

    def rail_page(self, i):
        self.tabs.setCurrentIndex(i)
        for k, idx in (("job", 0), ("maint", self.maint_index), ("settings", 2)):
            self.rail.button(k).setChecked(idx == i)

    # ------------------------------------------------------------------ build: print
    def build_print(self):
        self._print_widgets()
        return self._arrange_sub() if FLAVOR == "sub" else self._arrange_dtf()

    def _print_widgets(self):
        from . import rip as _rip
        self.channels = list(_rip.DEFAULT_CHANNELS)
        self.table = DropTable(self.add_files)
        self.table.itemSelectionChanged.connect(self._sel_changed)
        self.add_btn = QPushButton("Add artwork ...")
        self.add_btn.clicked.connect(self.pick_files)
        self.clr_btn = QPushButton("Remove all")
        self.clr_btn.clicked.connect(self.clear_items)
        self.hint = QLabel("Drag PNG / TIFF / JPG / PDF / SVG files into the window." + ("" if FLAVOR == "sub" else " Use PNG with a transparent background for DTF."))
        self.hint.setStyleSheet("color:#6B5C86")
        self.hint.setWordWrap(True)
        self.preset = QComboBox()
        self.profile = QComboBox()
        self.intent = QComboBox()
        for t in ("Perceptual (photos)", "Relative (logos)", "Saturation (vivid)"):
            self.intent.addItem(t)
        self.white_mode = QComboBox()
        for t, k in (("Solid under artwork", "solid"), ("Follow transparency", "soft"), ("No white", "off")):
            self.white_mode.addItem(t, k)
        self.white_amount = QSlider(Qt.Horizontal)
        self.white_amount.setRange(50, 140)
        self.colour_amount = QSlider(Qt.Horizontal)
        self.colour_amount.setRange(50, 150)
        self.wav, self.cav = QLabel(), QLabel()
        self.choke = dspin(0, 2, 0.05)
        self.presetnote = QLabel("")
        self.presetnote.setStyleSheet("color:#6B5C86")
        self.presetnote.setWordWrap(True)
        self.out_dpi = QComboBox()
        for d_ in (300, 360, 600, 720, 1200):
            self.out_dpi.addItem(f"{d_} dpi", d_)
        self.sharpen = QCheckBox("Sharpen a little")
        self.checks = {}
        labels = {"white_w1": "White 1", "white_w2": "White 2",
                  "mirror": "Mirror (transfer paper)" if FLAVOR == "sub" else "Mirror (DTF)",
                  "white_bg_transparent": "Transparent = white", "no_white_under_black": "No white under black"}
        for key, text in labels.items():
            self.checks[key] = QCheckBox(text)
        # ink amount and curve (only the inks that are switched on are shown)
        self.dens, self.curves, self.ink_rows = {}, {}, {}
        self.densf = QGridLayout()
        self.densf.addWidget(QLabel("<b>Ink</b>"), 0, 0)
        self.densf.addWidget(QLabel("<b>Amount</b>"), 0, 1)
        self.densf.addWidget(QLabel("<b>Curve</b>"), 0, 2)
        inks = (("C", "Cyan"), ("M", "Magenta"), ("Y", "Yellow"), ("LC", "Light cyan"), ("LM", "Light magenta")) + (() if FLAVOR == "sub" else (("K", "Black"), ("W1", "White 1"), ("W2", "White 2")))
        for r, (k, lab) in enumerate(inks, 1):
            s = dspin(0.3, 1.6, 0.05, w=72)
            c = dspin(0.5, 2.5, 0.05, w=72)
            s.valueChanged.connect(self.settings_changed)
            c.valueChanged.connect(self.settings_changed)
            self.dens[k], self.curves[k] = s, c
            lb = QLabel(lab)
            self.densf.addWidget(lb, r, 0)
            self.densf.addWidget(s, r, 1)
            self.densf.addWidget(c, r, 2)
            self.ink_rows[k] = (lb, s, c)
        self.chan_chips = QLabel("")
        self.chan_chips.setWordWrap(True)
        self.chan_btn = QPushButton("Ink channels ...")
        self.chan_btn.setToolTip("Which ink is in which tank, the order, and which channels are off (up to 8)")
        self.chan_btn.clicked.connect(self.open_channels)
        # paper size (any size from A6 to A3, or your own width x length)
        self.page = QComboBox()
        for k in ("A3", "A4", "A5", "A6", "B4", "B5", "Letter", "Legal", "Tabloid"):
            self.page.addItem(k, k)
        self.page.addItem("Custom size ...", "custom")
        self.page_w = dspin(30, 1500, 1, 0, w=84)
        self.page_h = dspin(0, 5000, 1, 0, w=84)
        self.page_h.setSpecialValueText("auto")
        self.page.currentIndexChanged.connect(self._page_changed)
        for wd in (self.page_w, self.page_h):
            wd.valueChanged.connect(self.settings_changed)
        self.page_size_lbl = QLabel("")
        self.page_size_lbl.setStyleSheet("color:#6B5C86")
        # move buttons (arrows) and step
        self.step = dspin(0.1, 100, 0.5, 1, w=70)
        self.step.setValue(2.0)
        self.step.setSuffix(" mm")
        # printer setup (like AcroRIP): model, port, performance, dot sizes, white detail, feed
        self.model = QComboBox()
        for t in ("Epson L800 (Colorsun / DTF)", "Epson L805 (same print head)"):
            self.model.addItem(t)
        self.device = QComboBox()
        self.device.setEditable(True)
        self.device.lineEdit().setPlaceholderText("Auto  (/dev/usb/lp0 | cups:QUEUE | win:PRINTER NAME)")
        self.conninfo = QLabel("")
        self.conninfo.setWordWrap(True)
        self.conninfo.setStyleSheet("color:#6B5C86")
        self.colour_dot = QComboBox()
        for t, k in (("Variable (small / medium / large)", "var"), ("Small", "s"), ("Medium", "m"), ("Large", "l")):
            self.colour_dot.addItem(t, k)
        self.white_dot = QComboBox()
        for t, k in (("Variable (medium / large)", "var"), ("Medium", "m"), ("Large", "l")):
            self.white_dot.addItem(t, k)
        self.white_detail = QComboBox()
        for t, k in (("Same as colour", 1), ("Half", 2), ("Quarter", 4)):
            self.white_detail.addItem(t, k)
        self.res_lbl = QLabel("")
        self.res_lbl.setStyleSheet("color:#6B5C86")
        self.feed_mode = QComboBox()
        self.feed_mode.addItem("Rear tray (single sheets / film)", "tray")
        self.feed_mode.addItem("Roll (page as long as the artwork)", "roll")
        self.feed_mode.currentIndexChanged.connect(self.settings_changed)
        self.feed = ispin(0, 89, 2)
        self.passes = ispin(0, 12)
        self.white_passes = ispin(0, 12)
        self.feed.setToolTip("How far the film moves between two passes of the head. 0 = the program calculates it (recommended).")
        self.passes.setToolTip("How many times the head prints over the same strip with colour ink. More passes = smoother, slower. "
                               "0 = from the Quality setting.")
        self.white_passes.setToolTip("The same for the white ink. White usually needs as many passes as colour or more. "
                                     "0 = same as colour passes.")
        self.uni = QComboBox()
        for t, v_ in (("From performance", ""), ("Unidirectional (sharper)", "1"), ("Bidirectional (faster)", "0")):
            self.uni.addItem(t, v_)
        for wd in (self.feed, self.passes, self.white_passes):
            wd.valueChanged.connect(self.settings_changed)
        for wd in (self.colour_dot, self.white_dot, self.white_detail, self.uni):
            wd.currentIndexChanged.connect(self.settings_changed)
        self.info = InfoBox()
        self.sel_thumb = QLabel("")
        self.sel_thumb.setAlignment(Qt.AlignCenter)
        self.sel_thumb.setFixedHeight(170)
        self.sel_thumb.setStyleSheet("background:#FFFFFF; border:1px solid #D0C4E1")
        # view buttons, canvas, job controls
        self.viewbtns = {}
        for k, t in (("colour", "Art"), ("white", "White"), ("film", "Paper" if FLAVOR == "sub" else "Film")):
            b = QPushButton(t)
            b.setCheckable(True)
            b.setChecked(k == "colour")
            b.clicked.connect(lambda _=False, k=k: self.set_view(k))
            b.setToolTip({"colour": "Show the artwork", "white": "Show the white layer", "film": "Show it on the paper" if FLAVOR == "sub" else "Show it on the film"}[k])
            self.viewbtns[k] = b
            if FLAVOR == "sub" and k == "white":
                b.hide()
        self.pv = CanvasView()
        self.pv.moved.connect(self.on_drop)
        self.pv.picked.connect(self.on_pick)
        self.pv_pix = None
        self.layouterr = QLabel("")
        self.layouterr.setStyleSheet("color:#B00020")
        self.layouterr.setWordWrap(True)
        self.go = QPushButton("Print ..." if FLAVOR == "sub" else "Print")
        ui.primary(self.go)
        self.go.clicked.connect(lambda: self.start_print(True))
        self.save = QPushButton("Save print-ready PNG / PDF ..." if FLAVOR == "sub" else "Make print file only")
        self.save.clicked.connect(lambda: self.start_print(False))
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(lambda: self.call("/api/cancel", {}))
        self.cancel.setVisible(False)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.jobmsg = QLabel("")
        self.jobmsg.setWordWrap(True)
        self.dl = QPushButton("Save print file ...")
        self.dl.setVisible(False)
        self.dl.clicked.connect(self.download)
        for wdg in (self.preset, self.profile, self.intent, self.white_mode, self.out_dpi):
            wdg.currentIndexChanged.connect(self.settings_changed)
        self.sharpen.toggled.connect(self.settings_changed)
        for s in (self.white_amount, self.colour_amount):
            s.valueChanged.connect(self.settings_changed)
        self.choke.valueChanged.connect(self.settings_changed)
        for c in self.checks.values():
            c.toggled.connect(self.settings_changed)
        self.strip = None

    # ---- small helpers for the panels
    @staticmethod
    def _form(rows, parent=None):
        w = QWidget(parent)
        f = QFormLayout(w)
        f.setContentsMargins(10, 8, 10, 8)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        for lab, wd in rows:
            if isinstance(wd, QLayout):
                f.addRow(lab, wd)
            elif lab is None:
                f.addRow(wd)
            else:
                f.addRow(lab, wd)
        return w

    @staticmethod
    def _bar_title(text):
        lb = QLabel(text)
        lb.setStyleSheet("background:#4A1F6E; color:white; font-weight:600; padding:5px 10px;")
        return lb

    def _slider_row(self, slider, label):
        r = QHBoxLayout()
        r.addWidget(slider, 1)
        r.addWidget(label)
        return r

    def _ink_grid_widget(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 8, 10, 8)
        v.addLayout(self.densf)
        return w

    def _channels_widget(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 8, 10, 8)
        v.addWidget(self.chan_chips)
        v.addWidget(self.chan_btn, 0, Qt.AlignLeft)
        return w

    def _job_controls(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 6, 8, 8)
        v.addWidget(self.go)
        v.addWidget(self.save)
        v.addWidget(self.cancel)
        v.addWidget(self.bar)
        v.addWidget(self.jobmsg)
        v.addWidget(self.dl, 0, Qt.AlignLeft)
        return w

    def _view_row(self):
        r = QHBoxLayout()
        for b in self.viewbtns.values():
            r.addWidget(b)
        r.addStretch()
        return r

    def _center(self):
        c = QWidget()
        v = QVBoxLayout(c)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        if FLAVOR == "dtf":
            self.imgtabs = QTabBar()
            self.imgtabs.setTabsClosable(False)
            self.imgtabs.setExpanding(False)
            self.imgtabs.setUsesScrollButtons(True)
            self.imgtabs.setDrawBase(False)
            self.imgtabs.currentChanged.connect(self._imgtab_changed)
            self.imgtabs.tabCloseRequested.connect(self._imgtab_close)
            v.addWidget(self.imgtabs)
        else:
            v.addLayout(self._view_row())
        v.addWidget(self.pv, 1)
        v.addWidget(self.layouterr)
        note = QLabel(("Load sublimation paper (printable side as your driver expects). In the driver dialog set colour management to OFF."
                       if FLAVOR == "sub" else "Load DTF film (print side up) in the rear tray before printing."))
        note.setWordWrap(True)
        note.setMinimumWidth(0)
        v.addWidget(note)
        return c

    # ---- DTF: left image information, middle page, right tabs, bottom thumbnails
    def _selected_widget(self):
        """width / copies / rotation of the selected artwork (they act on the hidden list)"""
        self.ed_w = ispin(5, 1500, 1, 84)
        self.ed_c = ispin(1, 50, 1, 64)
        self.ed_r = QComboBox()
        self.ed_r.addItems(["0", "90", "180", "270"])
        self.ed_w.valueChanged.connect(lambda v: self._edit_sel(1, v))
        self.ed_c.valueChanged.connect(lambda v: self._edit_sel(2, v))
        self.ed_r.currentIndexChanged.connect(lambda i: self._edit_sel(3, i))
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(10, 6, 10, 0)
        g.setHorizontalSpacing(8)
        for wd in (self.ed_w, self.ed_c, self.ed_r):
            wd.setFixedWidth(110)
        g.addWidget(QLabel("Width mm"), 0, 0)
        g.addWidget(self.ed_w, 0, 1)
        g.addWidget(QLabel("Copies"), 1, 0)
        g.addWidget(self.ed_c, 1, 1)
        g.addWidget(QLabel("Rotate"), 2, 0)
        g.addWidget(self.ed_r, 2, 1)
        g.setColumnStretch(2, 1)
        return w

    def _edit_sel(self, col, val):
        if self.filling or getattr(self, "_ed_sync", False):
            return
        r = self._sel_row()
        if r < 0:
            return
        wd = self.table.cellWidget(r, col)
        if wd is not None:
            wd.setValue(val) if col != 3 else wd.setCurrentIndex(val)

    def _sync_editors(self):
        if not hasattr(self, "ed_w"):
            return
        r = self._sel_row()
        self._ed_sync = True
        try:
            ok = 0 <= r < self.table.rowCount() and all(self.table.cellWidget(r, c) is not None for c in (1, 2, 3))
            for wd in (self.ed_w, self.ed_c, self.ed_r):
                wd.setEnabled(ok)
            if ok:
                self.ed_w.setValue(self.table.cellWidget(r, 1).value())
                self.ed_c.setValue(self.table.cellWidget(r, 2).value())
                self.ed_r.setCurrentIndex(self.table.cellWidget(r, 3).currentIndex())
        finally:
            self._ed_sync = False

    def _imgtab_changed(self, i):
        if not self.filling and 0 <= i < self.table.rowCount() and self.table.currentRow() != i:
            self.table.selectRow(i)

    def _imgtab_close(self, i):
        if self.S and 0 <= i < len(self.S["items"]):
            self.remove_item(self.S["items"][i]["index"])

    def _fill_tabs(self):
        tb = getattr(self, "imgtabs", None)
        if tb is None or not self.S:
            return
        tb.blockSignals(True)
        while tb.count():
            tb.removeTab(0)
        for it in self.S["items"]:
            i = tb.addTab(QIcon(item_thumb(it["path"], 18)), (it.get("name") or it["file"])[:22])
            tb.setTabToolTip(i, it.get("name") or it["file"])
            x = QPushButton("\u00d7")
            x.setFixedSize(18, 18)
            x.setCursor(Qt.PointingHandCursor)
            x.setStyleSheet("QPushButton{background:transparent; color:#6B5C86; border:none; border-radius:9px; padding:0; font-size:15px;}"
                            "QPushButton:hover{background:#C2395C; color:white;}")
            x.clicked.connect(lambda _=False, idx=it["index"]: self.remove_item(idx))
            tb.setTabButton(i, QTabBar.RightSide, x)
        r = self._sel_row()
        if 0 <= r < tb.count():
            tb.setCurrentIndex(r)
        tb.blockSignals(False)

    def close_selected(self):
        r = self._sel_row()
        if r < 0 or not self.S or r >= len(self.S["items"]):
            return
        self.remove_item(self.S["items"][r]["index"])

    def _page_changed_quiet(self):
        custom = self.page.currentData() == "custom"
        self.page_w.setEnabled(custom)
        self._page_label()

    def _page_changed(self):
        custom = self.page.currentData() == "custom"
        for wd in (self.page_w, self.page_h):
            wd.setEnabled(custom or wd is self.page_h)
        if not self.filling and self.S:
            if not custom:
                wh = (self.S.get("pages") or {}).get(self.page.currentData())
                if wh:
                    self.page_w.blockSignals(True)
                    self.page_w.setValue(wh[0])
                    self.page_w.blockSignals(False)
            self.settings_changed()
        self._page_label()

    def _page_label(self):
        k = self.page.currentData()
        wh = (self.S or {}).get("pages", {}).get(k) if self.S else None
        if k == "custom":
            self.page_size_lbl.setText(f"{self.page_w.value():.0f} x {self.page_h.value() or 297:.0f} mm")
        elif wh:
            self.page_size_lbl.setText(f"{wh[0]:g} x {self.page_h.value() or wh[1]:g} mm")

    def _paper_widget(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(10, 6, 10, 0)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.page.setMinimumWidth(40)
        f.addRow("Paper size", self.page)
        row = QHBoxLayout()
        row.addWidget(QLabel("W"))
        row.addWidget(self.page_w)
        row.addWidget(QLabel("L"))
        row.addWidget(self.page_h)
        row.addStretch()
        for sp_ in (self.page_w, self.page_h):
            sp_.setFixedWidth(64)
        f.addRow("Width / length (mm)", row)
        f.addRow("", self.page_size_lbl)
        return w

    def _move_widget(self):
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(10, 4, 10, 0)
        g.setSpacing(3)
        g.addWidget(QLabel("Move"), 1, 0)
        for text, dx, dy, r, c in (("\u2191", 0, -1, 0, 2), ("\u2190", -1, 0, 1, 1), ("\u2192", 1, 0, 1, 3), ("\u2193", 0, 1, 2, 2)):
            b = QPushButton(text)
            b.setFixedSize(30, 26)
            b.setStyleSheet("QPushButton{padding:0; font-size:15px;}")
            b.setAutoRepeat(True)
            b.setAutoRepeatInterval(120)
            b.clicked.connect(lambda _=False, dx=dx, dy=dy: self.nudge(dx, dy))
            g.addWidget(b, r, c)
        g.addWidget(QLabel("Step"), 3, 0)
        self.step.setFixedWidth(96)
        g.addWidget(self.step, 3, 1, 1, 3)
        g.setColumnStretch(4, 1)
        return w

    def nudge(self, dx, dy):
        r = self._sel_row()
        if r < 0 or not self.S or r >= len(self.S["items"]):
            return
        k = self.step.value()
        self.call("/api/nudge", {"index": self.S["items"][r]["index"], "dx": dx * k, "dy": dy * k},
                  then=lambda _: self.after_move())

    def after_move(self):
        self.load_preview()
        self.load_boxes()

    def on_drop(self, index, x, y):
        self.call("/api/place", {"index": index, "x": x, "y": y},
                  then=lambda _: (self.refresh(True)), err=lambda m: (self.layouterr.setText(m), self.after_move()))

    def on_pick(self, index):
        if self.S:
            for r, it in enumerate(self.S["items"]):
                if it["index"] == index and self.table.currentRow() != r:
                    self.table.selectRow(r)
                    break

    def load_boxes(self):
        def cb(res, e):
            if not e and res:
                self.pv.set_boxes(res["page"], res["boxes"])
        run_async(self.holders, lambda: self.api.json("/api/layout"), cb)

    def _printer_widget(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._bar_title("Printer"))
        drv = QPushButton("Driver help ...")
        drv.clicked.connect(self.driver_help)
        sv = QPushButton("Save port")
        sv.clicked.connect(self.conn_save)
        port = QHBoxLayout()
        port.addWidget(self.device, 1)
        port.addWidget(sv)
        v.addWidget(self._form([("Model", self.model), ("Port", port), (None, self.conninfo)]))
        v.addWidget(self._bar_title("Performance"))
        v.addWidget(self._form([("Quality", self.preset), (None, self.presetnote)]))
        v.addWidget(self._bar_title("Dot size and white"))
        v.addWidget(self._form([("Colour dot", self.colour_dot), ("White dot", self.white_dot),
                                ("White DPI", self.white_detail)]))
        v.addWidget(self._bar_title("Paper feed"))
        v.addWidget(self._form([("Feed from", self.feed_mode)]))
        v.addWidget(self._bar_title("Passes (advanced)"))
        v.addWidget(self._form([("Colour passes", self.passes), ("White passes", self.white_passes),
                                ("Paper step", self.feed), ("Direction", self.uni)]))
        h = QLabel("Passes = how many times the head goes over the same strip (more = smoother, slower). "
                   "Paper step = how far the film moves between passes. 0 = automatic from the Quality setting.")
        h.setWordWrap(True)
        h.setStyleSheet("color:#6B5C86; padding:0 10px")
        v.addWidget(h)
        hb = QHBoxLayout()
        hb.setContentsMargins(10, 4, 10, 0)
        hb.addWidget(drv)
        hb.addStretch()
        v.addLayout(hb)
        v.addStretch()
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(w)
        return sc

    def _arrange_dtf(self):
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(8, 8, 8, 8)
        g.setSpacing(8)
        left = QWidget()
        left.setFixedWidth(250)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.sel_thumb)
        lv.addWidget(self._bar_title("Image information"))
        lv.addWidget(self.info)
        lv.addStretch()
        g.addWidget(left, 0, 0)
        g.addWidget(self._center(), 0, 1)
        # right: tabs at the bottom (Layout | Printer | Color | White), job controls under them
        right = QWidget()
        right.setFixedWidth(250)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        tabs.setTabPosition(QTabWidget.South)
        tabs.setUsesScrollButtons(False)
        tabs.tabBar().setExpanding(True)
        tabs.tabBar().setStyleSheet("QTabBar::tab{padding:6px 4px; min-width:0;}")
        # layout
        lay_tab = QWidget()
        lt = QVBoxLayout(lay_tab)
        lt.setContentsMargins(0, 0, 0, 0)
        lt.addWidget(self._bar_title("Layout"))
        ar = QHBoxLayout()
        ar.setContentsMargins(6, 6, 6, 0)
        ar.setSpacing(2)
        rot = QPushButton()
        rot.setIcon(tool_icon("rotate", 22))
        rot.setIconSize(QSize(20, 20))
        rot.setFixedSize(28, 28)
        rot.setStyleSheet("QPushButton{padding:0;}")
        rot.setToolTip("Rotate the selected artwork by 90 degrees")
        rot.clicked.connect(self.rotate_selected)
        ar.addWidget(rot)
        for text, mode, tip in self.ALIGN:
            b = QPushButton()
            b.setIcon(tool_icon(mode, 22))
            b.setIconSize(QSize(20, 20))
            b.setFixedSize(28, 28)
            b.setStyleSheet("QPushButton{padding:0;}")
            b.setToolTip(f"{text}: {tip}")
            b.clicked.connect(lambda _=False, m=mode: self.align(m))
            ar.addWidget(b)
        ar.addStretch()
        lt.addLayout(ar)
        lt.addWidget(self._selected_widget())
        lt.addWidget(self._move_widget())
        lt.addWidget(self._paper_widget())
        mr = QHBoxLayout()
        mr.setContentsMargins(10, 4, 10, 0)
        mr.addWidget(self.checks["mirror"])
        lt.addLayout(mr)
        hl = QHBoxLayout()
        hl.setContentsMargins(10, 0, 10, 0)
        hl.addWidget(self.hint)
        lt.addLayout(hl)
        lt.addStretch(1)
        self.table.setParent(self)             # the list lives on in the background; the picture strip below is the list now
        self.table.hide()
        tabs.addTab(lay_tab, "Layout")
        # printer
        tabs.addTab(self._printer_widget(), "Printer")
        # colour
        ct = QWidget()
        cv = QVBoxLayout(ct)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(self._bar_title("Color"))
        cv.addWidget(self._form([("Colour profile", self.profile), ("Colour intent", self.intent),
                                 ("Colour amount", self._slider_row(self.colour_amount, self.cav))]))
        cv.addWidget(self._bar_title("Ink amount and curve"))
        cv.addWidget(self._ink_grid_widget())
        cv.addWidget(self._bar_title("Ink channels"))
        cv.addWidget(self._channels_widget())
        cv.addStretch()
        tabs.addTab(ct, "Color")
        # white
        wt = QWidget()
        wv = QVBoxLayout(wt)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.addWidget(self._bar_title("Preview"))
        pvw = QWidget()
        pvl = QHBoxLayout(pvw)
        pvl.setContentsMargins(8, 8, 8, 8)
        pvl.setSpacing(4)
        for b in self.viewbtns.values():
            pvl.addWidget(b)
        wv.addWidget(pvw)
        wv.addWidget(self._bar_title("White"))
        rows = [("White layer", self.white_mode), ("White amount", self._slider_row(self.white_amount, self.wav)),
                ("White choke (mm)", self.choke)]
        rows += [(None, self.checks[k]) for k in ("white_w1", "white_w2", "white_bg_transparent", "no_white_under_black")]
        wv.addWidget(self._form(rows))
        wv.addStretch()
        tabs.addTab(wt, "White")
        rv.addWidget(tabs, 1)
        rv.addWidget(self._job_controls())
        fit_column(right)
        g.addWidget(right, 0, 2, 2, 1)
        # bottom strip of thumbnails
        self.strip = QListWidget()
        self.strip.setViewMode(QListWidget.IconMode)
        self.strip.setFlow(QListWidget.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListWidget.Static)
        self.strip.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.strip.setSpacing(6)
        self.strip.setFixedHeight(118)
        self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.strip.currentRowChanged.connect(self._strip_clicked)
        sb = QWidget()
        sbl = QHBoxLayout(sb)
        sbl.setContentsMargins(0, 0, 0, 0)
        sbl.setSpacing(8)
        sbl.addWidget(self.strip, 1)
        bc = QVBoxLayout()
        bc.setSpacing(6)
        self.close_btn = QPushButton("Close")
        self.close_btn.setToolTip("Take the selected artwork off the page")
        self.close_btn.clicked.connect(self.close_selected)
        for b in (self.add_btn, self.clr_btn, self.close_btn):
            b.setFixedWidth(130)
            bc.addWidget(b)
        bc.addStretch()
        sbl.addLayout(bc)
        g.addWidget(sb, 1, 0, 1, 2)
        g.setColumnStretch(1, 1)
        g.setRowStretch(0, 1)
        return w

    # ---- sublimation: job queue on the left, page in the middle, collapsible job properties on the right
    def _arrange_sub(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        tb = QFrame()
        tb.setObjectName("subbar")
        tb.setStyleSheet("#subbar{background:#EDE6F4; border-bottom:1px solid #D0C4E1;}")
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(10, 6, 10, 6)
        tl.addWidget(self.add_btn)
        tl.addWidget(self.clr_btn)
        tl.addSpacing(10)
        rot = QPushButton()
        rot.setIcon(tool_icon("rotate", 22))
        rot.setIconSize(QSize(22, 22))
        rot.setFixedSize(38, 34)
        rot.setToolTip("Rotate the selected artwork by 90 degrees")
        rot.clicked.connect(self.rotate_selected)
        tl.addWidget(rot)
        for text, mode, tip in self.ALIGN:
            ab = QPushButton()
            ab.setIcon(tool_icon(mode, 22))
            ab.setIconSize(QSize(22, 22))
            ab.setFixedSize(38, 34)
            ab.setToolTip(f"{text}: {tip}")
            ab.clicked.connect(lambda _=False, m=mode: self.align(m))
            tl.addWidget(ab)
        tl.addWidget(self.checks["mirror"])
        tl.addStretch()
        tl.addWidget(self.save)
        tl.addWidget(self.go)
        tl.addWidget(self.cancel)
        v.addWidget(tb)
        sp = QSplitter(Qt.Horizontal)
        # jobs
        jobs = QWidget()
        jv = QVBoxLayout(jobs)
        jv.setContentsMargins(0, 0, 0, 0)
        jv.setSpacing(0)
        jv.addWidget(self._bar_title("Jobs"))
        hl = QHBoxLayout()
        hl.setContentsMargins(8, 6, 8, 0)
        hl.addWidget(self.hint)
        jv.addLayout(hl)
        jv.addWidget(self.table, 1)
        jv.addWidget(self._bar_title("Image information"))
        jv.addWidget(self.info)
        sp.addWidget(jobs)
        c = QWidget()
        cl = QVBoxLayout(c)
        cl.setContentsMargins(6, 6, 6, 0)
        cl.addWidget(self._center(), 1)
        sp.addWidget(c)
        # properties
        props = QWidget()
        pv = QVBoxLayout(props)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(0)
        pv.addWidget(Section("Paper size", self._paper_widget()))
        pv.addWidget(Section("Move artwork", self._move_widget()))
        pv.addWidget(Section("Media and output", self._form([("Image resolution", self.out_dpi), (None, self.sharpen)])))
        pv.addWidget(Section("Colour management", self._form([("Colour profile", self.profile), ("Colour intent", self.intent),
                                                              ("Colour amount", self._slider_row(self.colour_amount, self.cav))])))
        pv.addWidget(Section("Ink channels", self._channels_widget()))
        pv.addWidget(Section("Ink amount and curve", self._ink_grid_widget(), opened=False))
        pv.addStretch()
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(props)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setFixedWidth(250)
        fit_column(props)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sp.addWidget(sc)
        sp.setStretchFactor(0, 3)
        sp.setStretchFactor(1, 5)
        sp.setStretchFactor(2, 3)
        sp.setSizes([400, 750, 250])
        self.sub_split = sp
        self.sub_user_moved = False
        sp.splitterMoved.connect(lambda *_: setattr(self, "sub_user_moved", True))
        v.addWidget(sp, 1)
        st = QFrame()
        st.setObjectName("substatus")
        st.setStyleSheet("#substatus{background:#EDE6F4; border-top:1px solid #D0C4E1;}")
        sl = QHBoxLayout(st)
        sl.setContentsMargins(10, 4, 10, 4)
        sl.addWidget(self.jobmsg, 1)
        sl.addWidget(self.bar)
        self.bar.setFixedWidth(180)
        sl.addWidget(self.dl)
        v.addWidget(st)
        return w

    # ---- selection, info, thumbnails
    def _sel_row(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else (0 if self.table.rowCount() else -1)

    def _sel_changed(self):
        if self.filling or not self.S:
            return
        r = self._sel_row()
        items = self.S["items"]
        it = items[r] if 0 <= r < len(items) else None
        self.info.show_item(it)
        self._sync_editors()
        self.pv.set_selected(it["index"] if it else -1)
        if it:
            pm = item_thumb(it["path"], 160)
            self.sel_thumb.setPixmap(pm)
        else:
            self.sel_thumb.clear()
        tb = getattr(self, "imgtabs", None)
        if tb is not None and 0 <= r < tb.count() and tb.currentIndex() != r:
            tb.blockSignals(True)
            tb.setCurrentIndex(r)
            tb.blockSignals(False)
        if self.strip is not None and 0 <= r < self.strip.count() and self.strip.currentRow() != r:
            self.strip.blockSignals(True)
            self.strip.setCurrentRow(r)
            self.strip.blockSignals(False)

    def _strip_clicked(self, row):
        if row >= 0 and not self.filling:
            self.table.selectRow(row)

    ALIGN = (("Left Top", "lt", "to the top left corner"), ("Right Top", "rt", "to the top right corner"),
             ("H Center", "hc", "centre left to right"), ("V Center", "vc", "centre top to bottom"),
             ("Center", "c", "centre of the page"), ("Auto", "auto", "back to automatic layout"))

    def align(self, mode):
        r = self._sel_row()
        if r < 0 or not self.S or r >= len(self.S["items"]):
            QMessageBox.information(self, TITLE, "Add artwork and select it first.")
            return
        self.call("/api/align", {"index": self.S["items"][r]["index"], "mode": mode}, then=lambda _: self.refresh(True))

    def rotate_selected(self):
        r = self._sel_row()
        if r < 0:
            return
        cb = self.table.cellWidget(r, 3)
        cb.setCurrentIndex((cb.currentIndex() + 1) % cb.count())

    # ---- ink channels
    def open_channels(self):
        from . import rip as _rip
        dlg = InkChannelDialog(self, self.channels, FLAVOR, _rip.CHANNEL_INKS)
        if dlg.exec() == QDialog.Accepted:
            self.channels = _rip.clean_channels(dlg.values())
            self.update_channels_ui()
            self.settings_changed()

    def update_channels_ui(self):
        parts = []
        for i, k in enumerate(self.channels, 1):
            if k == "-":
                parts.append(f"<span style='color:#9C8FB5'>{i}&nbsp;off</span>")
            else:
                parts.append(f"{i}&nbsp;<span style='color:{INK_RGB[k]}; font-size:15px'>&#9679;</span>&nbsp;{INK_LABEL[k]}")
        self.chan_chips.setText("&nbsp;&nbsp; ".join(parts))
        for k, (lb, s, c) in self.ink_rows.items():
            on = k in self.channels
            for wdg in (lb, s, c):
                wdg.setVisible(on)


    # ------------------------------------------------------------------ build: maintenance
    def build_maint(self):
        w = QScrollArea()
        w.setWidgetResizable(True)
        body = QWidget()
        g = QGridLayout(body)
        g.setContentsMargins(12, 12, 12, 12)
        # head
        b1 = QGroupBox("Print head")
        v = QVBoxLayout(b1)
        t = QLabel("Put DTF film in the rear tray for the nozzle check and look at it against something dark, otherwise you cannot see "
                   "the white lines.")
        t.setWordWrap(True)
        v.addWidget(t)
        r = QHBoxLayout()
        for text, act in (("Nozzle check", "nozzle"), ("Head cleaning", "clean"), ("Power cleaning", "power")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, a=act: self.maint(a))
            r.addWidget(b)
        r.addStretch()
        v.addLayout(r)
        n = QLabel("Power cleaning uses a lot of ink and fills the waste pad. Use it only if 2-3 normal cleanings did not help.")
        n.setStyleSheet("color:gray")
        n.setWordWrap(True)
        v.addWidget(n)
        self.mmsg = QLabel("")
        self.mmsg.setWordWrap(True)
        v.addWidget(self.mmsg)
        g.addWidget(b1, 0, 0)
        # alignment
        b2 = QGroupBox("Head alignment")
        v = QVBoxLayout(b2)
        t = QLabel("1. Print the pattern on plain paper.  2. In each of the 4 blocks pick the number of the smoothest pair of lines "
                   "(1-15).  3. Save.")
        t.setWordWrap(True)
        v.addWidget(t)
        pb = QPushButton("Print alignment pattern")
        pb.clicked.connect(lambda: self.maint("align_print"))
        v.addWidget(pb, 0, Qt.AlignLeft)
        r = QHBoxLayout()
        self.aln = [ispin(1, 15, 1, 70) for _ in range(4)]
        for i, s in enumerate(self.aln):
            s.setValue(8)
            r.addWidget(QLabel(f"#{i + 1}"))
            r.addWidget(s)
        sv = QPushButton("Save to printer")
        sv.clicked.connect(self.align_save)
        r.addWidget(sv)
        r.addStretch()
        v.addLayout(r)
        g.addWidget(b2, 1, 0)
        # tests
        b3 = QGroupBox("Test prints")
        v = QVBoxLayout(b3)
        t = QLabel("Channel check prints one labelled block per ink to confirm " + ("C M Y K (light inks optional)" if FLAVOR == "sub" else "Y K W W M C") + ". The density chart shows how much ink each "
                   "channel can take before it floods the film.")
        t.setWordWrap(True)
        v.addWidget(t)
        r = QHBoxLayout()
        for text, k in (("Channel check", "channels"), ("Ink density chart", "density"), ("Keep-alive stripe now", "keepalive")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, k=k: self.test_print(k))
            r.addWidget(b)
        r.addStretch()
        v.addLayout(r)
        g.addWidget(b3, 2, 0)
        # ink
        b4 = QGroupBox("Ink counters")
        v = QVBoxLayout(b4)
        t = QLabel("What the printer believes is left. After refilling, reset the counters on the printer itself (hold the ink button "
                   "until the light changes).")
        t.setWordWrap(True)
        v.addWidget(t)
        rb = QPushButton("Read from printer")
        rb.clicked.connect(self.read_ink)
        v.addWidget(rb, 0, Qt.AlignLeft)
        self.inkrow = QHBoxLayout()
        v.addLayout(self.inkrow)
        self.inkmsg = QLabel("")
        v.addWidget(self.inkmsg)
        g.addWidget(b4, 0, 1, 2, 1)
        # keepalive
        b5 = QGroupBox("Keep-alive (stops the white ink from clogging)")
        f = QFormLayout(b5)
        self.ka_enabled = QCheckBox("Enabled")
        self.ka_hours = ispin(4, 96)
        self.ka_height = ispin(2, 20)
        self.ka_white = QCheckBox("White only")
        self.ka_qf = ispin(0, 23)
        self.ka_qt = ispin(0, 23)
        f.addRow("", self.ka_enabled)
        f.addRow("Idle hours before stripe", self.ka_hours)
        f.addRow("Stripe height (mm)", self.ka_height)
        f.addRow("", self.ka_white)
        f.addRow("Quiet from (hour)", self.ka_qf)
        f.addRow("Quiet until (hour)", self.ka_qt)
        sv = QPushButton("Save")
        sv.clicked.connect(self.ka_save)
        f.addRow("", sv)
        self.kamsg = QLabel("")
        f.addRow("", self.kamsg)
        g.addWidget(b5, 2, 1)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        g.setRowStretch(3, 1)
        w.setWidget(body)
        return w

    # ------------------------------------------------------------------ build: settings
    def build_settings(self):
        w = QScrollArea()
        w.setWidgetResizable(True)
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(12, 12, 12, 0)
        sb = QGroupBox("Printer status")                  # the same as in the header, so it is there when the header is hidden
        sv = QHBoxLayout(sb)
        self.pill2 = QLabel(self.pill.text())
        self.pill2.setStyleSheet("color:#4A1F6E; font-size:14px; font-weight:600; padding:4px 2px")
        sv.addWidget(self.pill2)
        sv.addStretch()
        outer.addWidget(sb)
        inner = QWidget()
        outer.addWidget(inner)
        g = QGridLayout(inner)
        g.setContentsMargins(0, 12, 0, 12)
        b1 = QGroupBox("Printer colour profile")
        v = QVBoxLayout(b1)
        t = QLabel("Colour profiles are not included with Digonto. Add the CMYK colour profile (.icc / .icm) of your own printer - "
                   "from your printer or ink seller, or your RIP software - with the button below. Then choose it in the Print tab.")
        t.setWordWrap(True)
        v.addWidget(t)
        pb = QPushButton("Add profile (.icc / .icm) ...")
        pb.clicked.connect(self.add_profile)
        v.addWidget(pb, 0, Qt.AlignLeft)
        self.iccmsg = QLabel("")
        v.addWidget(self.iccmsg)
        g.addWidget(b1, 0, 0)
        b3 = QGroupBox("Advanced")
        f = QFormLayout(b3)
        self.til = ispin(100, 400, 10)
        self.alpha = ispin(1, 99)
        self.colour_first = QCheckBox("Colour first, white on top (DTF)")
        for lab, wd in (("Total colour ink limit (%)", self.til), ("Transparency cut-off (%)", self.alpha)):
            f.addRow(lab, wd)
        f.addRow("", self.colour_first)
        for wd in (self.til, self.alpha):
            wd.valueChanged.connect(self.settings_changed)
        self.colour_first.toggled.connect(self.settings_changed)
        if FLAVOR == "sub":
            b3.hide()
        g.addWidget(b3, 0, 1, 2, 1)
        b4 = QGroupBox("Layout")
        f = QFormLayout(b4)
        self.spacing = dspin(0, 30, 0.5)
        self.top = dspin(0, 50, 0.5)
        f.addRow("Gap between images (mm)", self.spacing)
        f.addRow("Top margin (mm)", self.top)
        for wd in (self.spacing, self.top):
            wd.valueChanged.connect(self.settings_changed)
        g.addWidget(b4, 2, 0)
        b5 = QGroupBox("Printer driver setup")
        v5 = QVBoxLayout(b5)
        t5 = QLabel("Steps to install and check the printer driver for your system (Windows, macOS, Ubuntu), with links to the Epson "
                    "support page and the system printer settings. The printer model and port are set in the Print tab > Printer."
                    if FLAVOR == "dtf" else
                    "Steps to install the Epson driver and the right driver settings for sublimation (colour management off, 100 % scale), "
                    "with links to the Epson support page and the system printer settings.")
        t5.setWordWrap(True)
        v5.addWidget(t5)
        drv = QPushButton("Printer / driver setup help ...")
        drv.clicked.connect(self.driver_help)
        v5.addWidget(drv, 0, Qt.AlignLeft)
        g.addWidget(b5, 2, 1)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        g.setRowStretch(4, 1)
        w.setWidget(body)
        return w

    # ---- sublimation maintenance (through the driver)
    def build_maint_sub(self):
        from . import subtools as T
        self.T = T
        st = T.load_state()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 14, 14, 14)
        b = QGroupBox("Printer")
        f = QFormLayout(b)
        self.drv_printer = QComboBox()
        from PySide6.QtPrintSupport import QPrinterInfo
        names = [p.printerName() for p in QPrinterInfo.availablePrinters()]
        self.drv_printer.addItem("Ask me every time", "")
        for n in names:
            self.drv_printer.addItem(n, n)
        self.drv_printer.setCurrentIndex(max(0, self.drv_printer.findData(st["printer"])))
        self.drv_printer.currentIndexChanged.connect(self.drv_save)
        f.addRow("Print maintenance pages on", self.drv_printer)
        v.addWidget(b)
        b2 = QGroupBox("Weekly keep-alive")
        f2 = QFormLayout(b2)
        self.drv_weekly = QCheckBox("Remind me once a week to print the keep-alive page (stops the nozzles drying out)")
        self.drv_weekly.setChecked(st["weekly"])
        self.drv_weekly.toggled.connect(self.drv_save)
        self.drv_last = QLabel("")
        f2.addRow(self.drv_weekly)
        f2.addRow("Last done", self.drv_last)
        v.addWidget(b2)
        b3 = QGroupBox("Now")
        h = QHBoxLayout(b3)
        for text, fn in (("Print nozzle check", lambda: self.drv_print_page("check")),
                         ("Print keep-alive page", lambda: self.drv_print_page("keepalive")),
                         ("Open Epson head cleaning ...", self.drv_open_epson)):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            h.addWidget(btn)
        v.addWidget(b3)
        t = QLabel(T.HELP)
        t.setWordWrap(True)
        t.setStyleSheet("color:#444")
        v.addWidget(t)
        v.addStretch(1)
        self.drv_show_last()
        return w

    def drv_show_last(self):
        st = self.T.load_state()
        self.drv_last.setText(time.strftime("%d.%m.%Y %H:%M", time.localtime(st["last"])) if st["last"] else "never")

    def drv_save(self, *_):
        st = self.T.load_state()
        st["printer"] = self.drv_printer.currentData() or ""
        st["weekly"] = self.drv_weekly.isChecked()
        self.T.save_state(st)

    def drv_print_page(self, kind):
        from PySide6.QtCore import QMarginsF, QRectF, QSizeF
        from PySide6.QtGui import QPageLayout, QPageSize, QPainter
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
        from PIL.ImageQt import ImageQt
        pil = self.T.nozzle_check() if kind == "check" else self.T.keepalive_page()
        img = QImage(ImageQt(pil))
        name = self.drv_printer.currentData() or ""
        info = QPrinterInfo.printerInfo(name) if name else QPrinterInfo()
        pr = QPrinter(info, QPrinter.HighResolution) if name else QPrinter(QPrinter.HighResolution)
        pr.setFullPage(True)
        pr.setPageSize(QPageSize(QSizeF(210, 297), QPageSize.Millimeter))
        pr.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Millimeter)
        if not name:
            dlg = QPrintDialog(pr, self)
            if dlg.exec() != QPrintDialog.Accepted:
                return
        p = QPainter(pr)
        t = pr.pageRect(QPrinter.DevicePixel)
        p.drawImage(QRectF(0, 0, t.width(), t.height()), img)
        p.end()
        st = self.T.load_state()
        st["last"] = time.time()
        self.T.save_state(st)
        self.drv_show_last()

    def drv_open_epson(self):
        if not self.T.open_driver_maintenance(self.drv_printer.currentData() or ""):
            QMessageBox.information(self, TITLE, self.T.HELP)

    def weekly_check(self):
        st = self.T.load_state()
        if self.T.due(st) and QMessageBox.question(
                self, TITLE, "The weekly keep-alive is due (it stops the nozzles from drying out).\n\nPrint the keep-alive page now? "
                             "Use scrap paper.") == QMessageBox.Yes:
            self.drv_print_page("keepalive")

    def driver_help(self):
        if FLAVOR == "sub":
            txt = ("This program prepares the page (layout, mirror, colour profile, ink fine-tuning) and hands it to your printer's normal "
                   "Epson driver. Do this once:\n\n"
                   "1. Install the Epson driver for your printer (Epson support page, or Windows / macOS / Linux printer settings).\n"
                   "2. When you press Print, the driver dialog opens. Set colour management / colour adjustment to OFF (\"No colour adjustment\" "
                   "or ICM off) and choose the best quality and the paper type you use for sublimation.\n"
                   "3. Keep the page size and scale at 100 % (no 'fit to page').\n\n"
                   "For the best colours add an RGB ICC profile made for your printer + sublimation ink + paper (Settings > Add profile). "
                   "Without one, only your ink fine-tuning is applied.")
            sysurl = "ms-settings:printers" if sys.platform == "win32" else (
                "x-apple.systempreferences:com.apple.preference.printfax" if sys.platform == "darwin" else "")
        elif sys.platform == "win32":
            txt = ("Windows: install the Epson L800 driver once, so the printer exists in Windows.\n\n"
                   "1. Download the 'Epson L800' driver from Epson support and install it (printer connected and switched on).\n"
                   "2. Check that 'EPSON L800 Series' appears in Settings > Printers & scanners.\n"
                   "3. Come back here: Printer shows it in the list (win:EPSON L800 Series).\n\n"
                   "Digonto sends the print data raw through the Windows spooler, so the Epson driver does not change your colours.")
            sysurl = "ms-settings:printers"
        elif sys.platform == "darwin":
            txt = ("macOS: add the printer once.\n\n"
                   "1. System Settings > Printers & Scanners > Add Printer, choose the Epson L800 (the Epson driver downloads by itself).\n"
                   "2. Come back here: Printer shows it in the list (cups:NAME).\n\n"
                   "Digonto sends the data raw to that queue, so the driver does not change your colours.")
            sysurl = "x-apple.systempreferences:com.apple.preference.printfax"
        else:
            txt = ("Linux: no driver is needed - Digonto talks to the printer over USB directly (/dev/usb/lp0).\n\n"
                   "If it says 'No permission', the installer added you to the 'lp' group: log out and in once.\n"
                   "Unplug and re-plug the USB cable, or switch the printer off and on, if it is not found.")
            sysurl = ""
        m = QMessageBox(self)
        m.setWindowTitle("Printer / driver setup")
        m.setText(txt)
        b1 = m.addButton("Open Epson support page", QMessageBox.ActionRole)
        b2 = m.addButton("Open system printer settings", QMessageBox.ActionRole) if sysurl else None
        m.addButton("Close", QMessageBox.RejectRole)
        m.exec()
        if m.clickedButton() is b1:
            QDesktopServices.openUrl(QUrl("https://www.epson.de/de_DE/support"))
        elif b2 is not None and m.clickedButton() is b2:
            QDesktopServices.openUrl(QUrl(sysurl))

    # ------------------------------------------------------------------ state
    def call(self, path, data=None, then=None, err=None):
        def cb(res, e):
            if e:
                (err or (lambda m: QMessageBox.warning(self, TITLE, m)))(e)
            elif then:
                then(res)
        run_async(self.holders, lambda: self.api.json(path, data), cb)

    def set_status(self, text):
        self.pill.setText(text)
        if getattr(self, "pill2", None) is not None:
            self.pill2.setText(text)

    def refresh(self, full=True):
        def cb(res, e):
            if e:
                self.set_status("Engine error")
                return
            self.S = res
            self.show_state(full)
        run_async(self.holders, lambda: self.api.json("/api/state", timeout=15), cb)

    def show_state(self, full):
        S = self.S
        p, j = S["printer"], S["job"]
        if FLAVOR == "sub":
            self.set_status("Prints through your printer driver")
        elif j["stage"] == "printing":
            self.set_status(f"Printing {int(j['progress'] * 100)}%")
        elif not p.get("connected"):
            self.set_status("Printer not found")
        elif not p.get("writable"):
            self.set_status(f"No permission for {p.get('device')}")
        else:
            self.set_status(f"Ready - {p.get('device')}")
        self.show_job()
        busy = j["stage"] in BUSY
        if full or (self.busy_prev and not busy):
            self.fill_all()
        self.busy_prev = busy

    def fill_all(self):
        S = self.S
        s = S["settings"]
        self.filling = True
        self.preset.clear()
        for k, v in S["presets"].items():
            self.preset.addItem(v["label"], k)
        self.preset.setCurrentIndex(max(0, self.preset.findData(s["preset"])))
        self.presetnote.setText(S["presets"].get(s["preset"], {}).get("note", ""))
        self.profile.clear()
        self.profile.addItem("Built-in (no profile)", "")
        for pr in S["profiles"]:
            self.profile.addItem(pr, pr)
        self.profile.setCurrentIndex(max(0, self.profile.findData(s["profile"])))
        self.intent.setCurrentIndex(min(2, int(s["intent"])))
        self.white_mode.setCurrentIndex(max(0, self.white_mode.findData(s["white_mode"])))
        self.white_amount.setValue(int(round(s["white_amount"] * 100)))
        self.colour_amount.setValue(int(round(s["colour_amount"] * 100)))
        self.wav.setText(f"{self.white_amount.value()}%")
        self.cav.setText(f"{self.colour_amount.value()}%")
        self.choke.setValue(s["choke_mm"])
        for k, c in self.checks.items():
            c.setChecked(bool(s[k]))
        self.colour_first.setChecked(bool(s["colour_first"]))
        for k, sp in self.dens.items():
            sp.setValue(s["density"].get(k, 1.0))
        self.out_dpi.setCurrentIndex(max(0, self.out_dpi.findData(int(s.get("out_dpi", 600)))))
        self.sharpen.setChecked(bool(s.get("sharpen", False)))
        for k, sp in self.curves.items():
            sp.setValue((s.get("curve") or {}).get(k, 1.0))
        from .rip import clean_channels
        self.channels = clean_channels(s.get("channels"))
        self.update_channels_ui()
        self.til.setValue(int(round(s["total_ink_limit"] * 100)))
        self.alpha.setValue(int(round(s["alpha_threshold"] * 100)))
        self.feed.setValue(int(s["feed"]))
        self.passes.setValue(int(s["passes"]))
        self.white_passes.setValue(int(s["white_passes"]))
        u = s["unidirectional"]
        self.uni.setCurrentIndex(0 if u is None else (1 if u else 2))
        self.spacing.setValue(s["spacing_mm"])
        self.top.setValue(s["top_mm"])
        self.page.setCurrentIndex(max(0, self.page.findData(s.get("page", "A4"))))
        pw_ = (S.get("pages") or {}).get(s.get("page", "A4"))
        self.page_w.setValue(s.get("page_width_mm") or (pw_[0] if pw_ else 210))
        self.page_h.setValue(s["page_height_mm"])
        self._page_changed_quiet()
        self.feed_mode.setCurrentIndex(max(0, self.feed_mode.findData(s.get("feed_mode", "tray"))))
        self.colour_dot.setCurrentIndex(max(0, self.colour_dot.findData(s.get("colour_dot", "var"))))
        self.white_dot.setCurrentIndex(max(0, self.white_dot.findData(s.get("white_dot", "var"))))
        self.white_detail.setCurrentIndex(max(0, self.white_detail.findData(int(s.get("white_detail", 1)))))
        pr_ = S["presets"].get(s["preset"], {})
        ka = S["keepalive"]
        self.ka_enabled.setChecked(bool(ka.get("enabled")))
        self.ka_hours.setValue(int(ka.get("hours", 24)))
        self.ka_height.setValue(int(ka.get("height_mm", 4)))
        self.ka_white.setChecked(bool(ka.get("white_only")))
        self.ka_qf.setValue(int(ka.get("quiet_from", 22)))
        self.ka_qt.setValue(int(ka.get("quiet_to", 8)))
        self.device.clear()
        for d in S.get("devices") or []:
            self.device.addItem(d[0])
        self.device.setEditText(S.get("device_override") or "")
        pr = S["printer"]
        self.conninfo.setText(f"Device: {pr.get('device') or '-'}\nPrinter says: {(pr.get('id') or '-').replace(';', '; ')}\n"
                              f"Version: {S['version']}")
        for i, v in enumerate(S.get("alignment") or []):
            if i < 4:
                self.aln[i].setValue(int(v))
        self.filling = False
        self.fill_items()
        self.filling = False
        self._sync_editors()
        self.load_preview()

    # ------------------------------------------------------------------ items
    def fill_items(self):
        t = self.table
        keep = t.currentRow()
        t.setRowCount(0)
        self.filling = True
        for it in self.S["items"]:
            r = t.rowCount()
            t.insertRow(r)
            cell = QWidget()
            cell.setAttribute(Qt.WA_TransparentForMouseEvents)
            ch = QHBoxLayout(cell)
            ch.setContentsMargins(4, 2, 4, 2)
            th = QLabel()
            th.setPixmap(item_thumb(it["path"], 38))
            ch.addWidget(th)
            lab = QLabel(f"{it.get('name') or it['file']}<br><span style='color:#6B5C86'>{it['w']:.0f} x {it['h']:.0f} mm</span>")
            ch.addWidget(lab, 1)
            t.setCellWidget(r, 0, cell)
            wsp = ispin(5, 1500, 1, 66)
            wsp.setValue(int(round(it["width_mm"])))
            csp = ispin(1, 50, 1, 56)
            csp.setValue(int(it["copies"]))
            rot = QComboBox()
            rot.addItems(["0", "90", "180", "270"])
            rot.setCurrentText(str(int(it["rotate"])))
            rm = QPushButton("\u00d7")
            rm.setFixedWidth(28)
            rm.setStyleSheet("QPushButton{padding:2px 0; font-size:16px;}")
            rm.setToolTip("Remove this artwork")
            rm.clicked.connect(lambda _=False, i=it["index"]: self.remove_item(i))
            for c, wd in ((1, wsp), (2, csp), (3, rot), (4, rm)):
                t.setCellWidget(r, c, wd)
            wsp.valueChanged.connect(self.items_changed)
            csp.valueChanged.connect(self.items_changed)
            rot.currentIndexChanged.connect(self.items_changed)
            t.setRowHeight(r, 48)
        t.setColumnWidth(1, 78)
        t.setColumnWidth(2, 66)
        t.setColumnWidth(3, 62)
        t.setColumnWidth(4, 34)
        if self.strip is not None:
            self.strip.clear()
            for it in self.S["items"]:
                li = QListWidgetItem()
                li.setToolTip(it.get("name") or it["file"])
                li.setSizeHint(QSize(THUMB_SIZE + 22, THUMB_SIZE + 38))
                self.strip.addItem(li)
                self.strip.setItemWidget(li, self._thumb_card(it))
        if t.rowCount():
            row = keep if 0 <= keep < t.rowCount() else 0
            t.selectRow(row)
            if self.strip is not None:
                self.strip.setCurrentRow(row)
        self.filling = False
        self._fill_tabs()
        self._sel_changed()

    def _thumb_card(self, it):
        w = QWidget()
        w.setFixedSize(THUMB_SIZE + 16, THUMB_SIZE + 32)
        pic = QLabel(w)
        pic.setPixmap(item_thumb(it["path"]))
        pic.move(8, 8)
        pic.setAttribute(Qt.WA_TransparentForMouseEvents)
        nm = QLabel((it.get("name") or it["file"])[:13], w)
        nm.setAlignment(Qt.AlignCenter)
        nm.setGeometry(0, THUMB_SIZE + 12, THUMB_SIZE + 16, 18)
        nm.setAttribute(Qt.WA_TransparentForMouseEvents)
        x = QPushButton("\u00d7", w)
        x.setFixedSize(20, 20)
        x.move(THUMB_SIZE + 16 - 20, 0)
        x.setCursor(Qt.PointingHandCursor)
        x.setToolTip("Remove this artwork")
        x.setStyleSheet("QPushButton{background:#C2395C; color:white; border:none; border-radius:10px; padding:0; font-size:14px;}"
                        "QPushButton:hover{background:#8A2150;}")
        x.clicked.connect(lambda _=False, i=it["index"]: self.remove_item(i))
        return w

    def items_changed(self, *_):
        if not self.filling:
            self.pending_items.start()

    def save_items(self):
        items = []
        for r in range(self.table.rowCount()):
            items.append({"index": self.S["items"][r]["index"], "width_mm": self.table.cellWidget(r, 1).value(),
                          "copies": self.table.cellWidget(r, 2).value(), "rotate": int(self.table.cellWidget(r, 3).currentText())})
        self.call("/api/items", {"items": items}, then=lambda _: self.refresh(True))

    def remove_item(self, index):
        items = [{"index": it["index"], "remove": it["index"] == index, "width_mm": it["width_mm"], "copies": it["copies"],
                  "rotate": it["rotate"]} for it in self.S["items"]]
        self.call("/api/items", {"items": items}, then=lambda _: self.refresh(True))

    def clear_items(self):
        if self.S and self.S["items"] and QMessageBox.question(self, TITLE, "Remove all artwork?") == QMessageBox.Yes:
            self.call("/api/clear", {}, then=lambda _: self.refresh(True))

    def pick_files(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "Choose artwork", "", "Artwork (*.png *.jpg *.jpeg *.tif *.tiff *.pdf *.svg *.webp *.bmp)")
        if fs:
            self.add_files(fs)

    def add_files(self, files):
        files = [f for f in files if os.path.isfile(f)]
        if not files:
            return
        self.jobmsg.setText("Adding artwork ...")

        def work():
            for f in files:
                self.api.upload("/api/upload", "files", f)
        run_async(self.holders, work, lambda _r, e: (self.jobmsg.setText(""), QMessageBox.warning(self, TITLE, e) if e else None,
                                                     self.refresh(True)))

    # ------------------------------------------------------------------ settings
    def settings_changed(self, *_):
        if self.filling:
            return
        self.wav.setText(f"{self.white_amount.value()}%")
        self.cav.setText(f"{self.colour_amount.value()}%")
        self.pending_settings.start()

    def collect(self):
        u = self.uni.currentData()
        d = dict(preset=self.preset.currentData() or "high", profile=self.profile.currentData() or "", white_mode=self.white_mode.currentData(),
                 intent=self.intent.currentIndex(), feed=self.feed.value(), passes=self.passes.value(),
                 white_passes=self.white_passes.value(), white_amount=self.white_amount.value() / 100,
                 colour_amount=self.colour_amount.value() / 100, choke_mm=self.choke.value(), spacing_mm=self.spacing.value(),
                 top_mm=self.top.value(), page_height_mm=self.page_h.value(), page=self.page.currentData(),
                 page_width_mm=self.page_w.value() if self.page.currentData() == "custom" else 0.0,
                 feed_mode=self.feed_mode.currentData(), colour_dot=self.colour_dot.currentData(), white_dot=self.white_dot.currentData(),
                 white_detail=self.white_detail.currentData(), colour_first=self.colour_first.isChecked(),
                 total_ink_limit=self.til.value() / 100, alpha_threshold=self.alpha.value() / 100,
                 unidirectional=None if u == "" else (u == "1"), density={k: s.value() for k, s in self.dens.items()}, out_dpi=self.out_dpi.currentData() or 600, sharpen=self.sharpen.isChecked(),
                 curve={k: s.value() for k, s in self.curves.items()}, channels=list(self.channels))
        for k, c in self.checks.items():
            d[k] = c.isChecked()
        return d

    def save_settings(self):
        self.call("/api/settings", self.collect(), then=lambda _: self.refresh(True))

    # ------------------------------------------------------------------ preview
    def set_view(self, v):
        self.view = v
        for k, b in self.viewbtns.items():
            b.setChecked(k == v)
        self.load_preview()

    def load_preview(self):
        def work():
            data, hdr = self.api.req(f"/api/preview.png?view={self.view}", want_headers=True, timeout=120)
            return data, hdr.get("X-Layout-Error", "")

        def cb(res, e):
            if e:
                self.layouterr.setText(e)
                return
            img = QImage.fromData(res[0])
            self.pv_pix = QPixmap.fromImage(img)
            self.layouterr.setText(res[1])
            self.fit_preview()
            self.load_boxes()
        run_async(self.holders, work, cb)

    def fit_preview(self):
        if self.pv_pix:
            self.pv.set_pixmap(self.pv_pix, 40.0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        sp = getattr(self, "sub_split", None)
        if sp is not None and not self.sub_user_moved:        # the three columns follow the window size
            w = max(600, sp.width())
            sp.setSizes([int(w * 0.28), int(w * 0.44), int(w * 0.28)])
        self.fit_preview()

    # ------------------------------------------------------------------ jobs
    def show_job(self):
        j = self.S["job"]
        busy = j["stage"] in BUSY
        self.go.setEnabled(not busy)
        self.save.setEnabled(not busy)
        self.cancel.setVisible(busy)
        names = {"layout": "Preparing artwork", "dots": "Calculating dots", "printfile": "Building print file", "printing": "Sending to printer"}
        pct = j["progress"]
        if j["stage"] == "dots":
            pct = 0.1 + 0.6 * pct
        elif j["stage"] == "printfile":
            pct = 0.7 + 0.3 * pct
        self.bar.setValue(int(pct * 100) if busy else (100 if j["stage"] == "done" else 0))
        msg, self.last_file = "", None
        if busy:
            msg = names.get(j["stage"], "") + " ..."
        elif j["stage"] == "done" and j.get("result"):
            r = j["result"]
            msg = f"Done: {j.get('kind')}, {r['size_mm'][0]} x {r['size_mm'][1]} mm, {r['note']}"
            self.last_file = r["file"]
        elif j["stage"] == "error":
            msg = f"<span style='color:#B00020'>{j['error']}</span>"
        self.jobmsg.setText(msg)
        self.dl.setVisible(bool(self.last_file))

    def start_print(self, send):
        if send and self.S and not self.S["items"]:
            QMessageBox.information(self, TITLE, "Add artwork first.")
            return
        if FLAVOR == "sub":
            return self.driver_print() if send else self.driver_save()
        self.call("/api/print", {"send": send}, then=lambda _: self.refresh(False))

    # ---- sublimation: print through the printer driver
    def _fetch_page(self, fmt, then):
        self.jobmsg.setText("Preparing the print-ready page ...")
        dpi = self.out_dpi.currentData() or 600

        def work():
            self.api.json("/api/settings", self.collect())
            return self.api.req(f"/api/driver_image.{fmt}?dpi={dpi}", timeout=600, want_headers=True)

        def done(res, e):
            self.jobmsg.setText("")
            if e:
                QMessageBox.warning(self, TITLE, e)
                return
            then(res[0], res[1], dpi)
        run_async(self.holders, work, done)

    def driver_print(self):
        def go(data, hdr, dpi):
            from PySide6.QtCore import QRectF, QSizeF
            from PySide6.QtGui import QPageLayout, QPageSize, QPainter
            from PySide6.QtPrintSupport import QPrintDialog, QPrinter
            warn = (hdr.get("X-Warnings") or hdr.get("x-warnings") or "").strip()
            if warn:
                QMessageBox.information(self, TITLE, warn)
            img = QImage.fromData(data, "PNG")
            wmm, hmm = [float(x) for x in (hdr.get("X-Page-Mm") or hdr.get("x-page-mm")).split("x")]
            pr = QPrinter(QPrinter.HighResolution)
            pr.setFullPage(True)
            pr.setPageSize(QPageSize(QSizeF(wmm, hmm), QPageSize.Millimeter))
            pr.setPageMargins(__import__("PySide6.QtCore", fromlist=["QMarginsF"]).QMarginsF(0, 0, 0, 0), QPageLayout.Millimeter)
            dlg = QPrintDialog(pr, self)
            dlg.setWindowTitle("Print (turn the driver's colour management OFF)")
            if dlg.exec() != QPrintDialog.Accepted:
                return
            painter = QPainter(pr)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            target = pr.pageRect(QPrinter.DevicePixel)
            painter.drawImage(QRectF(0, 0, target.width(), target.height()), img)
            painter.end()
            self.jobmsg.setText("Sent to the printer driver.")
        self._fetch_page("png", go)

    def driver_save(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save print-ready page", "print-ready.png", "PNG image (*.png);;PDF (*.pdf)")
        if not p:
            return
        fmt = "pdf" if p.lower().endswith(".pdf") else "png"

        def go(data, hdr, dpi):
            with open(p, "wb") as f:
                f.write(data)
            self.jobmsg.setText(f"Saved {p}")
        self._fetch_page(fmt, go)

    def download(self):
        if not self.last_file:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save print file", self.last_file)
        if not p:
            return
        name = self.last_file

        def work():
            with open(p, "wb") as f:
                f.write(self.api.req(f"/api/download/{name}", timeout=300))
        run_async(self.holders, work, lambda _r, e: QMessageBox.warning(self, TITLE, e) if e else None)

    # ------------------------------------------------------------------ maintenance
    def maint(self, action):
        if action == "power" and QMessageBox.question(self, "Power cleaning", "Power cleaning uses a lot of ink. Continue?") != QMessageBox.Yes:
            return
        self.mmsg.setText("")
        self.call(f"/api/maint/{action}", {}, then=lambda _: self.mmsg.setText("Sent to printer."),
                  err=lambda m: self.mmsg.setText(f"<span style='color:#B00020'>{m}</span>"))

    def align_save(self):
        ch = [s.value() for s in self.aln]
        self.call("/api/maint/align_apply", {"choices": ch},
                  then=lambda _: self.mmsg.setText("Alignment saved. Print the pattern again: the best pair should now be in the middle (8)."))

    def test_print(self, kind):
        self.call(f"/api/test/{kind}", {"send": True}, then=lambda _: (self.tabs.setCurrentIndex(0), self.refresh(False)))

    def read_ink(self):
        self.inkmsg.setText("Reading ...")

        def then(res):
            self.inkmsg.setText("")
            while self.inkrow.count():
                w = self.inkrow.takeAt(0).widget()
                if w:
                    w.deleteLater()
            for k in ("Yellow", "Black", "White 1", "White 2", "Magenta", "Cyan"):
                if k in res["levels"]:
                    col = QWidget()
                    cv = QVBoxLayout(col)
                    bar = QProgressBar()
                    bar.setOrientation(Qt.Vertical)
                    bar.setRange(0, 100)
                    bar.setValue(int(res["levels"][k]))
                    bar.setTextVisible(False)
                    bar.setMinimumHeight(110)
                    bar.setFixedWidth(26)
                    bar.setStyleSheet(f"QProgressBar::chunk{{background:{INK_COL[k]};border:1px solid #888}}")
                    cv.addWidget(QLabel(f"{res['levels'][k]}%"), 0, Qt.AlignHCenter)
                    cv.addWidget(bar, 1, Qt.AlignHCenter)
                    cv.addWidget(QLabel(k), 0, Qt.AlignHCenter)
                    self.inkrow.addWidget(col)
        self.call("/api/maint/ink", {}, then=then, err=lambda m: self.inkmsg.setText(f"<span style='color:#B00020'>{m}</span>"))

    def ka_save(self):
        self.call("/api/config", {"keepalive": {"enabled": self.ka_enabled.isChecked(), "hours": self.ka_hours.value(),
                                                "height_mm": self.ka_height.value(), "white_only": self.ka_white.isChecked(),
                                                "quiet_from": self.ka_qf.value(), "quiet_to": self.ka_qt.value()}},
                  then=lambda _: self.kamsg.setText("Saved."))

    def conn_save(self):
        self.call("/api/config", {"device": self.device.currentText().strip()},
                  then=lambda _: (QMessageBox.information(self, TITLE, "Saved."),
                                  self.refresh(True)))

    def add_profile(self):
        p, _ = QFileDialog.getOpenFileName(self, "Printer profile", "", "ICC profile (*.icc *.icm)")
        if not p:
            return
        run_async(self.holders, lambda: self.api.upload("/api/profile", "file", p),
                  lambda r, e: (self.iccmsg.setText(e if e else f"Added {r['name']}"), self.refresh(True)))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def closeEvent(self, e):
        super().closeEvent(e)
        ui.force_exit_soon()      

    def about(self):
        QMessageBox.about(
            self, "About " + TITLE,
            f"<h3>{TITLE} {__version__}</h3>"
            "<p>RIP and print program for the Epson L800 DTF printer: colour and white in one run, ICC colour, white underbase, "
            "keep-alive for the white ink.</p>"
            "<p>Everything runs inside this program. Built with Qt for Python (PySide6), NumPy, Pillow and PyMuPDF. "
            "Nothing is sent over the internet.</p>")


def run():
    app = QApplication.instance() or QApplication(sys.argv)
    ui.setup_app(app, TITLE, SLUG, ui.app_icon(HERE, SLUG), theme=True)
    w = Window()
    w.show()
    return app.exec()
