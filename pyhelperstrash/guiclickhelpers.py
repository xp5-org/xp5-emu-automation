"""OCR-to-click for PC guests with a relative PS/2 mouse (Windows 3.1 under QEMU; the HMP monitor can only move by a delta).
Placement: 1. peg to the top-left corner (Windows clamps at 0,0); 2. move by target/gain, screenshot, measure where the cursor ended up; 3. correct by the remaining error with gain re-estimated, until within `tol` px.
Measurement: the diff of two screenshots is the union of the old and new 12x20 cursor patches (no template).
ocr_find(): phrase -> click point via tesseract word boxes on the upscaled screenshot.
"""

import difflib
import re
import time

import numpy as np
from PIL import Image, ImageOps
import pytesseract

try:
    from qemuhelpers import TESSERACT_TESSDATA_ARGS
except Exception:
    TESSERACT_TESSDATA_ARGS = ""

# Windows 3.1 arrow cursor, hot spot top-left, 12x20
CURSOR_W = 12
CURSOR_H = 20

# send large HMP mouse deltas in chunks
MOVE_CHUNK = 100
PEG_CHUNKS = 10
SETTLE_S = 0.35
INITIAL_GAIN = 2.0


FACE_RGB = (192, 199, 200)
FACE_L = 197
BUTTON_MIN_W, BUTTON_MAX_W = 28, 220
BUTTON_MIN_H, BUTTON_MAX_H = 16, 44


def _norm(s):
    """Lowercase alphanumerics: '&File' / 'File:' -> 'file'; 0 folds into o (tesseract reads 'OK' as '0K'), also applied to the phrase."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower()).replace("0", "o")


def _word_eq(a, b):
    """Close-match test for words long enough to be unambiguous."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    short, long_ = sorted((a, b), key=len)
    if long_.startswith(short) and len(long_) <= len(short) + 8:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def ocr_words(img, region=None, scale=3, psm=11):
    """Word boxes for a PIL image in native pixels. region: (x0, y0, x1, y1) crop; boxes stay relative to the full image. psm 11 (sparse text) for menu bars and buttons."""
    x0, y0 = (region[0], region[1]) if region else (0, 0)
    crop = img.crop(region) if region else img
    big = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    cfg = f"--psm {psm} {TESSERACT_TESSDATA_ARGS}".strip()
    data = pytesseract.image_to_data(big, config=cfg,
                                     output_type=pytesseract.Output.DICT)
    words = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text or float(data["conf"][i]) < 0:
            continue
        words.append({
            "text": text, "n": _norm(text),
            "x": data["left"][i] / scale + x0,
            "y": data["top"][i] / scale + y0,
            "w": data["width"][i] / scale,
            "h": data["height"][i] / scale,
        })
    return words


def _dark_runs(row, min_len):
    """(start, end) of runs of True at least min_len long in a 1-D bool row."""
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(s, e) for s, e in zip(edges[::2], edges[1::2]) if e - s >= min_len]


def _find_rects(a, interior, min_w, max_w, min_h, max_h, frac):
    """Bordered rectangles (x, y, w, h) in an RGB array `a`: two long dark horizontal edges of equal width, min_h..max_h apart, with `interior` (fill-colour mask) covering at least `frac` between them."""
    dark = a.mean(axis=2) < 60
    runs = {y: _dark_runs(dark[y], min_w) for y in range(dark.shape[0])}
    found = []
    for y0, r0 in runs.items():
        for (s0, e0) in r0:
            if e0 - s0 > max_w:
                continue
            for y1 in range(y0 + min_h, min(y0 + max_h, dark.shape[0] - 1) + 1):
                if not any(abs(s1 - s0) <= 2 and abs(e1 - e0) <= 2 for s1, e1 in runs.get(y1, ())):
                    continue
                inner = interior[y0 + 3:y1 - 2, s0 + 3:e0 - 3]
                if inner.size and inner.mean() >= frac:
                    found.append((s0, y0, e0 - s0, y1 - y0 + 1))
                    break
    found.sort()
    out = []
    for r in found:
        if not any(abs(r[0] - o[0]) <= 3 and abs(r[1] - o[1]) <= 3 for o in out):
            out.append(r)
    return out


def find_buttons(img):
    """Push-button rectangles: black outline around a grey 0xC0C7C8 face; callers OCR each interior separately."""
    a = np.asarray(img.convert("RGB")).astype(int)
    face = abs(a - np.array(FACE_RGB)).max(axis=2) < 12
    return _find_rects(a, face, BUTTON_MIN_W, BUTTON_MAX_W, BUTTON_MIN_H, BUTTON_MAX_H, 0.55)


def find_panels(img):
    """Menus and dialogs: black outline around mostly-white, smallest first; skips anything covering most of the screen (OCR each cropped on its own)."""
    a = np.asarray(img.convert("RGB")).astype(int)
    white = (a >= 250).all(axis=2)
    screen = a.shape[0] * a.shape[1]
    panels = [r for r in _find_rects(a, white, 60, a.shape[1], 40, 400, 0.5)
              if r[2] * r[3] < 0.6 * screen]
    panels.sort(key=lambda r: r[2] * r[3])
    return panels[:6]


def ocr_buttons(img, scale=4):
    """[([readings], (x, y, w, h))] read from inside each push button (inset a fraction of the width, wide first); every reading is kept."""
    gray = img.convert("L")
    found = []
    for (x, y, w, h) in find_buttons(img):
        readings = []
        for frac in (0.22, 0.12):
            ix, iy = max(6, int(w * frac)), 5
            inner = gray.crop((x + ix, y + iy, x + w - ix, y + h - iy))
            padded = ImageOps.expand(inner, border=10, fill=FACE_L)
            big = padded.resize((padded.width * scale, padded.height * scale), Image.LANCZOS)
            for psm in (8, 7):
                text = pytesseract.image_to_string(
                    big, config=f"--psm {psm} {TESSERACT_TESSDATA_ARGS}".strip()).strip()
                if len(_norm(text)) >= 2:
                    readings.append(text)
        found.append((readings, (x, y, w, h)))
    return found


def ocr_lines(img, region, scale=4):
    """Word boxes from `region` read one text line at a time (psm 7); lines found by row projection."""
    x0, y0, x1, y1 = region
    gray = np.asarray(img.convert("L"))[y0:y1, x0:x1]
    ink = (gray < 100).any(axis=1)
    words = []
    y = 0
    while y < len(ink):
        if not ink[y]:
            y += 1
            continue
        top = y
        while y < len(ink) and ink[y]:
            y += 1
        if y - top < 6:
            continue
        t, b = max(0, top - 2), min(len(ink), y + 2)
        words += ocr_words(img, region=(x0, y0 + t, x1, y0 + b), scale=scale, psm=7)
    return words


def _match_words(words, tokens, occurrence):
    """Boxes (x, y, w, h) where `tokens` occur as consecutive words, reading order; the occurrence-th, or None."""
    hits = []
    for i in range(len(words) - len(tokens) + 1):
        run = words[i:i + len(tokens)]
        if not all(_word_eq(tok, w["n"]) for tok, w in zip(tokens, run)):
            continue
        # multi-word phrase must sit on one text line
        if len(run) > 1 and max(abs(w["y"] - run[0]["y"]) for w in run) > run[0]["h"]:
            continue
        x = min(w["x"] for w in run)
        y = min(w["y"] for w in run)
        hits.append((x, y,
                     max(w["x"] + w["w"] for w in run) - x,
                     max(w["y"] + w["h"] for w in run) - y))
    hits.sort(key=lambda b: (round(b[1] / 8), b[0]))
    return hits[int(occurrence) - 1] if len(hits) >= int(occurrence) else None


def ocr_find(img, phrase, region=None, occurrence=1, scale=3):
    """Locate `phrase` on screen -> (found, box_or_reason, all_words). box = (x, y, w, h) around the phrase; occurrence is 1-based in reading order.
    A miss falls through: other psm/scale combinations, then each menu/dialog panel, then push-button labels.
    """
    tokens = [t for t in (_norm(w) for w in phrase.split()) if t]
    if not tokens:
        return False, "empty phrase", []

    all_words = []
    for sc, psm in ((scale, 11), (scale, 6), (4, 11), (2, 11), (4, 6)):
        words = ocr_words(img, region=region, scale=sc, psm=psm)
        all_words = words
        box = _match_words(words, tokens, occurrence)
        if box:
            return True, box, words

    def in_region(b):
        return not region or (region[0] <= b[0] <= region[2] and region[1] <= b[1] <= region[3])

    for panel in find_panels(img):
        if not in_region(panel):
            continue
        x, y, w, h = panel
        for words in (ocr_words(img, region=(x, y, x + w, y + h), scale=3, psm=6),
                      ocr_words(img, region=(x, y, x + w, y + h), scale=4, psm=6),
                      ocr_lines(img, (x + 2, y + 2, x + w - 2, y + h - 2))):
            box = _match_words(words, tokens, occurrence)
            if box:
                return True, box, words

    want = _norm(phrase)
    hits = [b for readings, b in ocr_buttons(img)
            if any(_word_eq(want, _norm(t)) for t in readings) and in_region(b)]
    hits.sort(key=lambda b: (round(b[1] / 8), b[0]))
    if len(hits) >= int(occurrence):
        return True, hits[int(occurrence) - 1], all_words
    return False, f"'{phrase}' not found in OCR words, panels or button labels", all_words


class RelativeMouse:
    """Closed-loop pointer placement + click for one QemuInstance."""

    def __init__(self, instance, shot_path, log=None):
        self.inst = instance
        self.shot_path = shot_path
        self.log = log if log is not None else []
        self.gain = [INITIAL_GAIN, INITIAL_GAIN]
        self.pos = None


    def _hmp(self, cmd):
        ok, out = self.inst._monitor_send(cmd)
        if not ok:
            raise RuntimeError(out)
        return out

    def _snap(self):
        ok, msg = self.inst.take_screenshot(filename=self.shot_path)
        if not ok:
            raise RuntimeError(f"screenshot failed: {msg}")
        return np.array(Image.open(self.shot_path).convert("RGB")).astype(int)

    def _move(self, dx, dy):
        """Relative move in HMP units, chunked."""
        dx, dy = int(dx), int(dy)
        while dx or dy:
            cx = max(-MOVE_CHUNK, min(MOVE_CHUNK, dx))
            cy = max(-MOVE_CHUNK, min(MOVE_CHUNK, dy))
            self._hmp(f"mouse_move {cx} {cy}")
            dx -= cx
            dy -= cy
            time.sleep(0.05)

    def peg(self):
        """Drive the pointer into the top-left corner: (0, 0) exactly."""
        for _ in range(PEG_CHUNKS):
            self._hmp(f"mouse_move -{MOVE_CHUNK} -{MOVE_CHUNK}")
            time.sleep(0.05)
        time.sleep(SETTLE_S)
        self.pos = (0.0, 0.0)


    @staticmethod
    def _moved(before, after, sx, sy):
        """Cursor movement between two frames from the diff bbox: union of the two CURSOR_W x CURSOR_H patches = (CURSOR_W + |mx|) x (CURSOR_H + |my|); direction from the commanded (sx, sy in -1/0/+1). Returns (mx, my) or None if nothing changed."""
        diff = (before != after).any(axis=2)
        ys, xs = np.nonzero(diff)
        if not len(xs):
            return None
        bw = xs.max() - xs.min() + 1
        bh = ys.max() - ys.min() + 1
        return sx * max(0, bw - CURSOR_W), sy * max(0, bh - CURSOR_H)


    def goto(self, x, y, tol=3, max_iter=10, peg=True):
        """Put the cursor tip within `tol` px of (x, y) -> (ok, msg). peg=False when the caller already pegged (self.pos is (0, 0))."""
        if peg or self.pos is None:
            self.peg()
        frame = self._snap()
        for it in range(max_iter):
            ex, ey = x - self.pos[0], y - self.pos[1]
            if abs(ex) <= tol and abs(ey) <= tol:
                self.log.append(f"cursor at ({self.pos[0]:.0f},{self.pos[1]:.0f}) "
                                f"for target ({x:.0f},{y:.0f}) after {it} move(s)")
                return True, self.log[-1]
            cx = 0 if abs(ex) <= tol else int(round(ex / self.gain[0])) or (1 if ex > 0 else -1)
            cy = 0 if abs(ey) <= tol else int(round(ey / self.gain[1])) or (1 if ey > 0 else -1)
            self._move(cx, cy)
            time.sleep(SETTLE_S)
            after = self._snap()
            moved = self._moved(frame, after, (cx > 0) - (cx < 0), (cy > 0) - (cy < 0))
            if moved is None:
                self.log.append(f"move {it}: ({cx},{cy}) changed nothing on screen")
                return False, "pointer did not move (no mouse in guest?)"
            mx, my = moved
            if abs(cx) >= 6 and mx:
                self.gain[0] = min(4.0, max(0.5, abs(mx) / abs(cx)))
            if abs(cy) >= 6 and my:
                self.gain[1] = min(4.0, max(0.5, abs(my) / abs(cy)))
            self.pos = (self.pos[0] + mx, self.pos[1] + my)
            self.log.append(f"move {it}: HMP ({cx},{cy}) -> guest ({mx:+.0f},{my:+.0f}), "
                            f"now ({self.pos[0]:.0f},{self.pos[1]:.0f}), "
                            f"gain=({self.gain[0]:.2f},{self.gain[1]:.2f})")
            frame = after
        return False, (f"cursor still at ({self.pos[0]:.0f},{self.pos[1]:.0f}) after "
                       f"{max_iter} moves, wanted ({x:.0f},{y:.0f})")

    def click(self, button=1, clicks=1, hold=0.12, gap=0.12):
        for i in range(int(clicks)):
            self._hmp(f"mouse_button {button}")
            time.sleep(hold)
            self._hmp("mouse_button 0")
            if i + 1 < int(clicks):
                time.sleep(gap)
