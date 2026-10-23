import c4d
from c4d import gui, bitmaps, storage, documents
import os
import json
import base64
import tempfile
import urllib.request
import urllib.error
import ssl
import time
from datetime import datetime
import traceback
import sys
import subprocess


# ---------------------------------------------------------------------------
# Console noise filter
# ---------------------------------------------------------------------------

class _NullSpamFilterStream(object):
    """Pass-through text stream that drops common useless console spam lines.

    Some Cinema/Python setups occasionally spew endless lines like `(null)`, `null`
    or `0` into the Console. Keep everything else untouched.
    """

    __slots__ = ("_stream", "_buf")

    def __init__(self, stream):
        self._stream = stream
        self._buf = ""

    def _should_drop_line(self, line):
        if line is None:
            return True
        s = str(line).replace("\\x00", "").replace("\x00", "").strip()
        if not s:
            return True
        if s in ("(null)", "null", "None", "0"):
            return True
        return False

    def write(self, data):
        if data is None:
            return 0
        try:
            chunk = str(data)
        except Exception:
            chunk = data
        self._buf += chunk
        written = 0
        while True:
            pos = self._buf.find("\n")
            if pos < 0:
                break
            line = self._buf[:pos]
            self._buf = self._buf[pos + 1:]
            if self._should_drop_line(line):
                continue
            try:
                self._stream.write(line + "\n")
                written += len(line) + 1
            except Exception:
                pass
        return written

    def flush(self):
        if self._buf:
            if not self._should_drop_line(self._buf):
                try:
                    self._stream.write(self._buf)
                except Exception:
                    pass
            self._buf = ""
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        try:
            return self._stream.encoding
        except Exception:
            return "utf-8"

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _install_console_noise_filter():
    try:
        if sys.stdout is not None and not isinstance(sys.stdout, _NullSpamFilterStream):
            sys.stdout = _NullSpamFilterStream(sys.stdout)
    except Exception:
        pass
    try:
        if sys.stderr is not None and not isinstance(sys.stderr, _NullSpamFilterStream):
            sys.stderr = _NullSpamFilterStream(sys.stderr)
    except Exception:
        pass


_install_console_noise_filter()


# ---------------------------------------------------------------------------
# SSL (workaround): disable certificate verification for urllib in some setups
# ---------------------------------------------------------------------------

try:
    _SSL_UNVERIFIED_CTX = ssl._create_unverified_context()
except Exception:
    _SSL_UNVERIFIED_CTX = None


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

# Main areas
IDC_UA_MAIN           = 100
IDC_UA_REFS           = 101
IDC_CHATLOG           = 102
IDC_PROMPT            = 103

# Toolbar / actions
IDC_BTN_RENDER        = 110
IDC_BTN_LOAD          = 111
IDC_BTN_APIKEY        = 112
IDC_AR_COMBO          = 113

IDC_BTN_ADD_REF       = 115
IDC_BTN_GENERATE      = 116
IDC_BTN_HISTORY       = 117


IDC_TOOLBAR_SPACER   = 118
# Right history
IDC_HISTORY_SCROLL    = 200
IDC_HISTORY_BODY_UA   = 201


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SETTINGS_FILENAME = ".nano_pro_demo_settings.json"
STATE_FILENAME    = "nano_pro_demo_state.json"

# UI sizes
REFS_AREA_H       = 74
REF_THUMB_H       = 64
HISTORY_THUMB_W   = 200
HISTORY_GAP       = 10

ASPECT_RATIO_ITEMS = [
    "auto",
    "1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"
]



# ---------------------------------------------------------------------------
# Google Nano Banana 2 / Gemini 3.1 Flash Image (built-in)
# ---------------------------------------------------------------------------

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent"


def _banana_build_payload(prompt, main_b64, refs_b64, aspect_ratio="auto"):
    """Build a Gemini 3 image request using the current GenerateContent schema."""
    parts = []

    if (prompt or "").strip():
        parts.append({"text": (prompt or "").strip()})

    if main_b64:
        parts.append({
            "inlineData": {"mimeType": "image/png", "data": main_b64}
        })

    for b64 in (refs_b64 or []):
        if not b64:
            continue
        parts.append({
            "inlineData": {"mimeType": "image/png", "data": b64}
        })

    req = {
        "contents": [{"role": "user", "parts": parts}],
    }

    # Gemini 3 image models use generationConfig.responseFormat.image.
    # When AR is auto we omit it so the model follows the input image, or 1:1 for text-only.
    if aspect_ratio and aspect_ratio != "auto":
        req["generationConfig"] = {
            "responseFormat": {
                "image": {"aspectRatio": aspect_ratio}
            }
        }

    return json.dumps(req).encode("utf-8")


def _banana_extract_images_from_response(data):
    images = []
    candidates = data.get("candidates") or []
    for cand in candidates:
        parts = (cand.get("content") or {}).get("parts") or []
        for p in parts:
            inline = p.get("inline_data") or p.get("inlineData") or {}
            b64 = inline.get("data")
            if b64:
                try:
                    images.append(base64.b64decode(b64))
                except Exception:
                    pass
    return images


def banana_generate(args):
    """
    Returns:
      {"chat_lines":[...], "images":[raw_bytes,...]}
    """
    chat = []
    api_key = (args.get("api_key") or "").strip()
    if not api_key:
        return {"chat_lines": ["<< Google API key is empty."], "images": []}

    prompt = args.get("prompt") or ""
    aspect_ratio = args.get("aspect_ratio") or "auto"
    main_b64 = args.get("main_b64")
    refs_b64 = args.get("refs_b64") or []

    # Enforce max input images = 3 (includes main)
    max_in = 3
    used = 1 if main_b64 else 0
    allowed_refs = max(0, max_in - used)
    refs_b64 = (refs_b64 or [])[:allowed_refs]

    http_post = args.get("http_post")
    if not callable(http_post):
        return {"chat_lines": ["<< http_post helper is missing."], "images": []}

    payload = _banana_build_payload(prompt, main_b64, refs_b64, aspect_ratio=aspect_ratio)
    url = GEMINI_ENDPOINT
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "x-goog-api-key": api_key,
    }

    status, body = http_post(url, payload, headers)

    if status not in (200, 201):
        chat.append(f"<< Google Nano Banana 2 HTTP {status}:")
        chat.append((body or "")[:2000])
        return {"chat_lines": chat, "images": []}

    try:
        data = json.loads(body or "{}")
    except Exception as e:
        chat.append("<< Failed to parse JSON: " + str(e))
        chat.append((body or "")[:2000])
        return {"chat_lines": chat, "images": []}

    # Optional text chunks
    try:
        candidates = data.get("candidates") or []
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text_chunks = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
            text_answer = "\n".join(text_chunks).strip()
            if text_answer:
                chat.append("NANO BANANA:\n" + text_answer)
    except Exception:
        pass

    images = _banana_extract_images_from_response(data)

    if not images:
        chat.append("<< No images in response.")
        return {"chat_lines": chat, "images": []}

    return {"chat_lines": chat, "images": images}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_script_dir():
    """Best-effort directory of this script."""
    try:
        # Works for Script Manager
        return os.path.dirname(__file__)
    except Exception:
        try:
            return os.path.dirname(os.path.abspath(sys.argv[0]))
        except Exception:
            return os.path.expanduser("~")


def ensure_images_dir(script_dir):
    """Folder where we store outputs and state."""
    # Prefer a subfolder near the script if possible.
    d1 = os.path.join(script_dir, "nanopro_demo_images")
    try:
        os.makedirs(d1, exist_ok=True)
        return d1
    except Exception:
        pass

    # Fallback to user home.
    d2 = os.path.join(os.path.expanduser("~"), "nanopro_demo_images")
    try:
        os.makedirs(d2, exist_ok=True)
    except Exception:
        pass
    return d2


def load_bitmap(path):
    """Loads an image into a BaseBitmap."""
    if not path or not os.path.isfile(path):
        return None
    bmp = bitmaps.BaseBitmap()
    res, _ = bmp.InitWith(path)
    if res != c4d.IMAGERESULT_OK:
        return None
    return bmp


class SlotEntry:
    __slots__ = ("path", "bmp")

    def __init__(self, path=None, bmp=None):
        self.path = path
        self.bmp = bmp


# ---------------------------------------------------------------------------
# UI Areas
# ---------------------------------------------------------------------------

class PreviewArea(gui.GeUserArea):
    """Main preview area. Draws the current bitmap with auto-fit or manual zoom."""

    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        self._bg = gui.GetGuiWorldColor(c4d.COLOR_BG)

    def _effective_zoom(self, w, h, bw, bh):
        if not self.dlg.zoom_manual:
            fit = min(float(w) / float(bw), float(h) / float(bh))
            return 1.0 if (bw <= w and bh <= h) else fit
        return max(0.05, min(8.0, float(self.dlg.zoom)))

    def _get_draw_params(self):
        bmp = self.dlg.view_bmp
        if not bmp:
            return None
        bw, bh = bmp.GetSize()
        if bw <= 0 or bh <= 0:
            return None
        w = max(1, self.GetWidth() or 1)
        h = max(1, self.GetHeight() or 1)
        z = self._effective_zoom(w, h, bw, bh)
        dw = max(1, int(bw * z))
        dh = max(1, int(bh * z))
        x = (w - dw) // 2
        y = (h - dh) // 2
        return bw, bh, z, x, y, w, h

    def GetMinSize(self):
        bmp = self.dlg.view_bmp
        if not bmp:
            return (600, 340)
        bw, bh = bmp.GetSize()
        if bw <= 0 or bh <= 0:
            return (600, 340)
        w = max(1, self.GetWidth() or 800)
        h = max(1, self.GetHeight() or 450)
        z = self._effective_zoom(w, h, bw, bh)
        return (max(1, int(bw * z)), max(1, int(bh * z)))

    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        w = max(1, self.GetWidth())
        h = max(1, self.GetHeight())
        self.DrawSetPen(self._bg)
        self.DrawRectangle(0, 0, w, h)

        bmp = self.dlg.view_bmp
        if not bmp:
            return True

        params = self._get_draw_params()
        if not params:
            return True

        bw, bh, z, x, y, w, h = params
        dw = max(1, int(bw * z))
        dh = max(1, int(bh * z))

        self.DrawBitmap(bmp, x, y, dw, dh, 0, 0, bw, bh, c4d.BMP_NORMAL)
        return True

    def InputEvent(self, msg):
        # Wheel zoom: keeps behavior minimal and optional.
        try:
            if msg.GetLong(c4d.BFM_INPUT_CHANNEL) == c4d.BFM_INPUT_MOUSEWHEEL:
                delta = msg.GetLong(c4d.BFM_INPUT_VALUE)
                if delta != 0:
                    cur = float(self.dlg.zoom or 1.0)
                    factor = 1.1 if delta > 0 else (1.0 / 1.1)
                    self.dlg.zoom = max(0.05, min(8.0, cur * factor))
                    self.dlg.zoom_manual = True
                    self.Redraw()
                    self.dlg._save_state()
                    return True
        except Exception:
            pass

        # RMB context menu: copy / paste / save as / clear
        try:
            if msg.GetLong(c4d.BFM_INPUT_CHANNEL) == c4d.BFM_INPUT_MOUSERIGHT and msg.GetLong(c4d.BFM_INPUT_VALUE) == 1:
                CTX_MAIN_COPY = 5301
                CTX_MAIN_PASTE = 5302
                CTX_MAIN_SAVEAS = 5303
                CTX_MAIN_CLEAR = 5304

                bc = c4d.BaseContainer()
                if self.dlg.view_bmp:
                    bc.SetString(CTX_MAIN_COPY, "Copy")
                    bc.SetString(CTX_MAIN_SAVEAS, "Save As...")
                    bc.SetString(CTX_MAIN_CLEAR, "Clear")
                bc.SetString(CTX_MAIN_PASTE, "Paste")

                res = gui.ShowPopupDialog(cd=self.dlg, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)

                if res == CTX_MAIN_COPY:
                    self.dlg.copy_main_to_clipboard()
                    return True
                if res == CTX_MAIN_PASTE:
                    self.dlg.paste_main_from_clipboard()
                    return True
                if res == CTX_MAIN_SAVEAS:
                    self.dlg._save_main_image_dialog()
                    return True
                if res == CTX_MAIN_CLEAR:
                    self.dlg.clear_main_image()
                    return True
        except Exception:
            pass

        return False


class ReferencesArea(gui.GeUserArea):
    """Horizontal reference thumbnails area with selection + context menu."""

    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        self._bg = gui.GetGuiWorldColor(c4d.COLOR_BG)
        self._text = gui.GetGuiWorldColor(c4d.COLOR_TEXT)
        # Prefer highlight color if available
        try:
            self._hl = gui.GetGuiWorldColor(c4d.COLOR_HIGHLIGHT)
        except Exception:
            self._hl = self._text
        self._thumb_rects = []  # (x1, y1, x2, y2, idx)

    def GetMinSize(self):
        return (100, REFS_AREA_H)

    def DrawMsg(self, x1, y1, x2, y2, msg):
        """Draws reference thumbnails and caches hit-test rectangles."""
        self.OffScreenOn()
        w = max(1, self.GetWidth())
        h = max(1, self.GetHeight())
        self.DrawSetPen(self._bg)
        self.DrawRectangle(0, 0, w, h)

        refs = self.dlg.refs
        x = 10
        max_h = REFS_AREA_H - 2
        self._thumb_rects = []

        for idx, entry in enumerate(refs):
            if not entry.bmp:
                continue
            bw, bh = entry.bmp.GetSize()
            if bw <= 0 or bh <= 0:
                continue

            scale = float(max_h - 2) / float(bh)
            dw = max(1, int(bw * scale))
            dh = max(1, int(bh * scale))
            if x + dw > w - 10:
                break

            draw_y = (h - dh) // 2

            self.DrawBitmap(entry.bmp, x, draw_y, dw, dh, 0, 0, bw, bh, c4d.BMP_NORMAL)
            self._thumb_rects.append((x, draw_y, x + dw, draw_y + dh, idx))

            # Selection frame
            if idx in (self.dlg.refs_sel or set()):
                self.DrawSetPen(self._hl)
                # Slight inset for nicer look
                self.DrawRectangle(x - 2, draw_y - 2, x + dw + 2, draw_y + dh + 2)

            x += dw + 10

        if not refs:
            self.DrawSetTextCol(self._text, self._bg)
            self.DrawText("Add reference images using + Ref", 10, 10, c4d.DRAWTEXT_STD_ALIGN)

        return True

    def _hit_test(self, x, y):
        """Returns the reference index under the cursor (or nearest by X)."""
        if not self._thumb_rects:
            return None

        # Favor horizontal picking (keeps behavior stable when strip is tall)
        best_idx = None
        best_dist = None
        for rx1, ry1, rx2, ry2, idx in self._thumb_rects:
            if rx1 <= x <= rx2 and ry1 <= y <= ry2:
                return idx
            cx = (rx1 + rx2) * 0.5
            dist = abs(x - cx)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def InputEvent(self, msg):
        if msg.GetId() != c4d.BFM_INPUT:
            return False
        if msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE:
            return False

        ch = msg[c4d.BFM_INPUT_CHANNEL]

        # LMB: selection (supports Shift/Ctrl), optional hold-to-preview on plain click.
        if ch == c4d.BFM_INPUT_MOUSELEFT:
            if msg[c4d.BFM_INPUT_VALUE] != 1:
                return True
            if not self.dlg.refs:
                return True

            x = msg.GetInt32(c4d.BFM_INPUT_X)
            y = msg.GetInt32(c4d.BFM_INPUT_Y)
            hit_idx = self._hit_test(x, y)
            if hit_idx is None:
                return False  # allow scroll outside thumbnails

            qual = int(msg.GetInt32(c4d.BFM_INPUT_QUALIFIER) or 0)
            is_shift = (qual & c4d.QSHIFT) != 0
            is_ctrl = (qual & c4d.QCTRL) != 0

            self.dlg._refs_select(hit_idx, shift=is_shift, ctrl=is_ctrl)
            self.Redraw()

            # Keep original hold-preview behavior only on plain click.
            if not is_shift and not is_ctrl:
                entry = self.dlg.refs[hit_idx]
                if entry and entry.path:
                    self.dlg._hold_preview_show_path(entry.path)

            return True

        # RMB: context menu (Delete). If selection exists - delete selection.
        if ch == c4d.BFM_INPUT_MOUSERIGHT:
            if msg[c4d.BFM_INPUT_VALUE] != 1:
                return True
            if not self.dlg.refs:
                return True

            x = msg.GetInt32(c4d.BFM_INPUT_X)
            y = msg.GetInt32(c4d.BFM_INPUT_Y)
            hit_idx = self._hit_test(x, y)

            CTX_REF_DELETE = 5101
            bc = c4d.BaseContainer()
            bc.SetString(CTX_REF_DELETE, "Remove")

            res = gui.ShowPopupDialog(cd=self.dlg, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)

            if res == CTX_REF_DELETE:
                # If something is selected, delete selection; else delete hit.
                if self.dlg.refs_sel:
                    self.dlg.delete_selected_references()
                else:
                    if hit_idx is None:
                        hit_idx = len(self.dlg.refs) - 1
                    self.dlg.delete_reference_at(hit_idx)
                self.Redraw()

            return True

        return False


class HistoryArea(gui.GeUserArea):
    """Vertical history thumbnails area with a context menu."""

    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        self._bg = gui.GetGuiWorldColor(c4d.COLOR_BG)
        self._text = gui.GetGuiWorldColor(c4d.COLOR_TEXT)
        self._thumb_rects = []  # (x1, y1, x2, y2, idx)

    def GetMinSize(self):
        # Keep old min width, but allow drawing to clamp to actual width.
        height = HISTORY_GAP
        for entry in self.dlg.history:
            bmp = entry.bmp
            if not bmp:
                continue
            bw, bh = bmp.GetSize()
            if bw <= 0 or bh <= 0:
                continue
            scale = float(HISTORY_THUMB_W) / float(bw)
            dh = max(1, int(bh * scale))
            height += dh + HISTORY_GAP
        if height < 200:
            height = 200
        return (HISTORY_THUMB_W + 10, height)

    def DrawMsg(self, x1, y1, x2, y2, msg):
        """Draws history thumbnails and caches hit-test rectangles."""
        self.OffScreenOn()
        w = max(1, self.GetWidth())
        h = max(1, self.GetHeight())
        self.DrawSetPen(self._bg)
        self.DrawRectangle(0, 0, w, h)

        y = HISTORY_GAP
        self._thumb_rects = []

        # Clamp draw width so thumbs never run under the right border/scroll bar.
        draw_w = max(50, min(HISTORY_THUMB_W, w - 10))

        for idx, entry in enumerate(self.dlg.history):
            bmp = entry.bmp
            if not bmp:
                continue
            bw, bh = bmp.GetSize()
            if bw <= 0 or bh <= 0:
                continue

            scale = float(draw_w) / float(bw)
            dw = int(draw_w)
            dh = max(1, int(bh * scale))

            x = 5
            self.DrawBitmap(bmp, x, y, dw, dh, 0, 0, bw, bh, c4d.BMP_NORMAL)
            self._thumb_rects.append((x, y, x + dw, y + dh, idx))
            y += dh + HISTORY_GAP

        if not self.dlg.history:
            self.DrawSetTextCol(self._text, self._bg)
            self.DrawText("History will appear here", 8, 8, c4d.DRAWTEXT_STD_ALIGN)

        return True

    def _hit_test(self, x, y):
        if not self._thumb_rects:
            return None
        for rx1, ry1, rx2, ry2, idx in self._thumb_rects:
            if rx1 <= x <= rx2 and ry1 <= y <= ry2:
                return idx
        return None

    def InputEvent(self, msg):
        if msg.GetId() != c4d.BFM_INPUT:
            return False
        if msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE:
            return False

        ch = msg[c4d.BFM_INPUT_CHANNEL]

        if ch == c4d.BFM_INPUT_MOUSELEFT:
            if msg[c4d.BFM_INPUT_VALUE] != 1:
                return True
            if not self.dlg.history:
                return True

            x = msg.GetInt32(c4d.BFM_INPUT_X)
            y = msg.GetInt32(c4d.BFM_INPUT_Y)

            # Compensate for scrolling/embedding (matches NanoPro.py behavior)
            g2l = self.Global2Local()
            if g2l:
                x += g2l.get("x", 0)
                y += g2l.get("y", 0)

            hit_idx = self._hit_test(x, y)
            if hit_idx is None:
                return False  # allow scroll outside thumbnails

            entry = self.dlg.history[hit_idx]
            if entry and entry.path:
                self.dlg._hold_preview_show_path(entry.path)
            return True

        if ch == c4d.BFM_INPUT_MOUSERIGHT:
            if msg[c4d.BFM_INPUT_VALUE] != 1:
                return True
            if not self.dlg.history:
                return True

            x = msg.GetInt32(c4d.BFM_INPUT_X)
            y = msg.GetInt32(c4d.BFM_INPUT_Y)

            # Compensate for scrolling/embedding (matches NanoPro.py behavior)
            g2l = self.Global2Local()
            if g2l:
                x += g2l.get("x", 0)
                y += g2l.get("y", 0)

            hit_idx = self._hit_test(x, y)

            CTX_HIST_SET_MAIN = 5201
            CTX_HIST_DELETE = 5202
            CTX_HIST_CLEAR_ALL = 5203
            CTX_HIST_SEND_REFS = 5204
            CTX_HIST_SHOW_IN_FOLDER = 5205

            bc = c4d.BaseContainer()
            bc.SetString(CTX_HIST_SET_MAIN, "Set as main")
            bc.SetString(CTX_HIST_SEND_REFS, "Send to refs")
            bc.SetString(CTX_HIST_DELETE, "Remove")
            bc.SetString(CTX_HIST_CLEAR_ALL, "Clear all history")
            bc.SetString(CTX_HIST_SHOW_IN_FOLDER, "Show in folder")

            res = gui.ShowPopupDialog(cd=self.dlg, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)

            # Default to last (most recent) if user clicked empty area.
            if hit_idx is None:
                hit_idx = len(self.dlg.history) - 1

            if hit_idx < 0 or hit_idx >= len(self.dlg.history):
                return True

            if res == CTX_HIST_SET_MAIN:
                self.dlg.set_main_image_from_path(self.dlg.history[hit_idx].path)
                return True
            if res == CTX_HIST_SEND_REFS:
                self.dlg.send_history_to_refs(hit_idx)
                return True
            if res == CTX_HIST_DELETE:
                self.dlg.delete_history_at(hit_idx)
                return True
            if res == CTX_HIST_CLEAR_ALL:
                self.dlg.clear_all_history()
                return True
            if res == CTX_HIST_SHOW_IN_FOLDER:
                p = self.dlg.history[hit_idx].path
                if p and os.path.isfile(p):
                    self.dlg.show_in_folder(p)
                return True

            return True

        return False



# ---------------------------------------------------------------------------
# API key dialog (Google only)
# ---------------------------------------------------------------------------

class ApiKeyDialog(gui.GeDialog):
    """Google API key dialog (OK/Cancel, persists via caller)."""

    IDC_EDIT   = 3100
    IDC_OK     = 3101
    IDC_CANCEL = 3102

    def __init__(self, current_key):
        super().__init__()
        self.current_key = (current_key or "").strip()
        self.result_key = None  # None => cancelled

    def CreateLayout(self):
        self.SetTitle("Google API key")

        self.GroupBegin(10, c4d.BFH_SCALEFIT | c4d.BFV_TOP, cols=1, rows=0)
        self.AddStaticText(0, c4d.BFH_LEFT, name="Enter Google AI Studio API key:")
        self.AddEditText(self.IDC_EDIT, c4d.BFH_SCALEFIT, initw=420)
        self.GroupEnd()

        self.GroupBegin(20, c4d.BFH_RIGHT, cols=2, rows=1)
        self.AddButton(self.IDC_OK, c4d.BFH_RIGHT, name="OK")
        self.AddButton(self.IDC_CANCEL, c4d.BFH_RIGHT, name="Cancel")
        self.GroupEnd()
        return True

    def InitValues(self):
        self.SetString(self.IDC_EDIT, self.current_key)
        return True

    def Command(self, mid, bc):
        if mid == self.IDC_OK:
            self.result_key = (self.GetString(self.IDC_EDIT) or "").strip()
            self.Close()
            return True
        if mid == self.IDC_CANCEL:
            self.result_key = None
            self.Close()
            return True
        return False



# ---------------------------------------------------------------------------
# Background render thread
# ---------------------------------------------------------------------------

class NanoProRenderThread(c4d.threading.C4DThread):
    """Background renderer that writes into a shared bitmap."""

    def __init__(self, src_doc, rd_container, target_bmp):
        super().__init__()
        self._src_doc = src_doc
        self._rd = rd_container
        self._bmp = target_bmp
        self._ok = False
        self._err = None

    def Main(self):
        try:
            # Clone document to reduce risk when rendering in a thread.
            doc = self._src_doc.GetClone(c4d.COPYFLAGS_0)
            if not doc:
                self._err = "Failed to clone document"
                return

            res = documents.RenderDocument(doc, self._rd, self._bmp, c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_NODOCUMENTCLONE, self.Get())
            self._ok = (res == c4d.RENDERRESULT_OK)
            if not self._ok:
                self._err = "RenderDocument failed: %s" % str(res)
        except Exception as e:
            self._ok = False
            self._err = str(e)

    @property
    def ok(self):
        return self._ok

    @property
    def err(self):
        return self._err


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------

class NanoProDemoDialog(gui.GeDialog):

    def _set_status(self, text):
        try:
            c4d.StatusSetText(text or "")
        except Exception:
            pass
        c4d.EventAdd()

    def __init__(self):
        super().__init__()

        self.script_dir = get_script_dir()
        self.images_dir = ensure_images_dir(self.script_dir)

        self.settings_path = os.path.join(os.path.expanduser("~"), SETTINGS_FILENAME)
        self.state_path = os.path.join(self.images_dir, STATE_FILENAME)

        self.google_api_key = ""

        self.view_bmp = None
        self.view_path = ""

        self.refs = []
        self.history = []
        self.refs_sel = set()
        self._refs_last_idx = None


        # Hold-to-preview (history/refs)
        self._hold_preview_restore = None  # (bmp, path, zoom, zoom_manual)
        self._hold_preview_path = ""

        # UI state
        self.ar_index = 0
        self.history_visible = True
        self.zoom = 1.0
        self.zoom_manual = False

        self.ua_main = None
        self.ua_refs = None
        self.ua_history = None

        # Async render
        self._render_thread = None
        self._render_bmp = None
        self._render_preview_bmp = None
        self._render_rd = None
        self._render_prev_view_bmp = None
        self._render_prev_view_path = ""
        self._render_done = False
        self._render_err = None

        # Async generation
        self._gen_in_progress = False
        self._gen_thread = None
        self._gen_result = None
        self._gen_error = None
        self._gen_started_at = 0.0
        self._gen_last_elapsed = -1

        self.chatlog_text = ""

        self._load_settings()
        self._load_state()

    # ---------------- paths / state ----------------

    def _safe_slug(self, s):
        s = (s or "").strip()
        out = []
        for ch in s:
            if ch.isalnum() or ch in ("_", "-"):
                out.append(ch)
            else:
                out.append("_")
        t = "".join(out).strip("_")
        return t or "out"

    def _get_unique_image_path(self, prefix="img", ext=".png"):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = "%s_%s" % (prefix, ts)
        n = 1
        while True:
            name = "%s_%03d%s" % (base, n, ext)
            p = os.path.join(self.images_dir, name)
            if not os.path.exists(p):
                return p
            n += 1

    def _save_bitmap_to_images_dir(self, bmp, prefix="img"):
        """Saves bitmap to the demo output folder and logs the full path."""
        if not bmp:
            return ""
        out_path = self._get_unique_image_path(prefix=prefix, ext=".png")
        try:
            res = bmp.Save(out_path, c4d.FILTER_PNG)
            if res == c4d.IMAGERESULT_OK:
                # Log full path for any saved image (requested).
                try:
                    label = "Saved"
                    if prefix == "render":
                        label = "Render saved"
                    elif prefix == "paste":
                        label = "Paste saved"
                    elif prefix == "banana2":
                        label = "Output saved"
                    self._append_chat(f"<< {label}: {out_path}")
                except Exception:
                    pass
                return out_path
        except Exception:
            pass
        return ""

    def _load_settings(self):
        try:
            if not os.path.isfile(self.settings_path):
                return
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            return
        self.google_api_key = (data.get("google_api_key") or "").strip()

    def _save_settings(self):
        try:
            data = {"google_api_key": (self.google_api_key or "").strip()}
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_state(self):
        try:
            if not os.path.isfile(self.state_path):
                return
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            return

        self.ar_index = int(data.get("ar_index", 0))
        self.history_visible = bool(data.get("history_visible", True))
        self.zoom = float(data.get("zoom", 1.0) or 1.0)
        self.zoom_manual = bool(data.get("zoom_manual", False))
        self.view_path = (data.get("view_path") or "").strip()

        if self.view_path and os.path.isfile(self.view_path):
            self.view_bmp = load_bitmap(self.view_path)

        self.refs = []
        for p in (data.get("refs") or []):
            if p and os.path.isfile(p):
                self.refs.append(SlotEntry(path=p, bmp=load_bitmap(p)))

        self.history = []
        for p in (data.get("history") or []):
            if p and os.path.isfile(p):
                self.history.append(SlotEntry(path=p, bmp=load_bitmap(p)))

        self.chatlog_text = data.get("chatlog", "") or ""

    def _save_state(self):
        try:
            try:
                chatlog = self.GetString(IDC_CHATLOG) or ""
            except Exception:
                chatlog = self.chatlog_text or ""
            data = {
                "ar_index": int(self.ar_index),                "history_visible": bool(self.history_visible),
                "zoom": float(self.zoom),
                "zoom_manual": bool(self.zoom_manual),
                "view_path": self.view_path,
                "refs": [e.path for e in self.refs if e.path],
                "history": [e.path for e in self.history if e.path],
                "chatlog": chatlog,
            }
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


    def show_in_folder(self, path):
        """Reveal a file in the OS file manager and preselect it (Win/macOS)."""
        if not path:
            return
        try:
            path = os.path.normpath(path)
        except Exception:
            pass

        # First try Cinema 4D helper (works on many setups)
        try:
            if os.path.isfile(path):
                storage.ShowInFinder(path)
                return
        except Exception:
            pass

        # OS-specific fallback with explicit selection
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select," + path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            # Last resort: open containing folder
            try:
                folder = os.path.dirname(path)
                if folder and os.path.isdir(folder):
                    storage.ShowInFinder(folder)
            except Exception:
                pass

    # ---------------- main image / slots ----------------

    def set_main_image_from_path(self, path):
        if not path or not os.path.isfile(path):
            return False
        bmp = load_bitmap(path)
        if not bmp:
            return False
        self.view_bmp = bmp
        self.view_path = path
        if self.ua_main:
            self.ua_main.Redraw()
        self._save_state()
        return True

    def clear_main_image(self):
        self.view_bmp = None
        self.view_path = ""
        if self.ua_main:
            self.ua_main.Redraw()
        self._save_state()

    def set_main_image_from_bitmap(self, bmp, prefix="img", add_to_history=True, push_old_to_history=True):
        """Sets the main image from a bitmap (saves it into images_dir first)."""
        if bmp is None:
            return False

        # Optionally push current main to history before replacing it.
        if push_old_to_history and self.view_path and os.path.isfile(self.view_path):
            try:
                self.add_history_entry(self.view_path)
            except Exception:
                pass

        path = self._save_bitmap_to_images_dir(bmp, prefix=prefix or "img")
        if path and os.path.isfile(path):
            ok = self.set_main_image_from_path(path)
            if ok and add_to_history:
                try:
                    self.add_history_entry(path)
                except Exception:
                    pass
            return ok

        # Fallback: keep bitmap in memory (no path)
        self.view_bmp = bmp
        self.view_path = ""
        if self.ua_main:
            self.ua_main.Redraw()
        self._save_state()
        return True

    # ---------------- Clipboard helpers ----------------

    def copy_main_to_clipboard(self):
        """Copies the main image to the system clipboard."""
        if not self.view_bmp:
            gui.MessageDialog("No image to copy.")
            return
        try:
            c4d.CopyBitmapToClipboard(self.view_bmp, 0)
        except Exception as e:
            gui.MessageDialog("Failed to copy bitmap to clipboard:\n{}".format(e))

    def paste_main_from_clipboard(self):
        """Pastes an image from the system clipboard into the main view."""
        try:
            bmp = c4d.GetBitmapFromClipboard()
        except Exception as e:
            gui.MessageDialog("Failed to get bitmap from clipboard:\n{}".format(e))
            return

        if bmp is None:
            gui.MessageDialog("Clipboard does not contain a bitmap.")
            return

        self.set_main_image_from_bitmap(
            bmp,
            prefix="paste",
            add_to_history=True,
            push_old_to_history=True,
        )

    def _save_main_image_dialog(self):
        """Save As... for the current main image."""
        bmp = self.view_bmp
        if bmp is None:
            gui.MessageDialog("No image to save.")
            return

        try:
            fn = storage.SaveDialog(type=c4d.FILESELECTTYPE_IMAGES, title="Save image as")
        except TypeError:
            try:
                fn = storage.SaveDialog(c4d.FILESELECTTYPE_IMAGES, "Save image as")
            except TypeError:
                fn = storage.SaveDialog()

        if not fn:
            return

        fn = os.path.normpath(fn)
        root, ext = os.path.splitext(fn)
        ext_l = (ext or "").lower()

        if not ext_l:
            ext_l = ".png"
            fn = fn + ext_l

        def _get_filter(ext_lower):
            mapping = {
                ".png": "FILTER_PNG",
                ".jpg": "FILTER_JPG",
                ".jpeg": "FILTER_JPG",
                ".tif": "FILTER_TIF",
                ".tiff": "FILTER_TIF",
                ".tga": "FILTER_TGA",
                ".bmp": "FILTER_BMP",
                ".psd": "FILTER_PSD",
                ".exr": "FILTER_EXR",
                ".hdr": "FILTER_HDR",
                ".webp": "FILTER_WEBP",
            }
            name = mapping.get(ext_lower, "FILTER_PNG")
            return getattr(c4d, name, c4d.FILTER_PNG)

        flt = _get_filter(ext_l)

        try:
            res = bmp.Save(fn, flt)
        except Exception as e:
            gui.MessageDialog("Failed to save image:\n" + str(e))
            return

        if res != c4d.IMAGERESULT_OK:
            gui.MessageDialog("Failed to save image (code {}).".format(res))
            return

        # Log full path of saved file.
        try:
            self._append_chat(f"<< Saved As: {fn}")
        except Exception:
            pass

    def clear_all_history(self):
        """Clears all history entries."""
        self.history = []
        if self.ua_history:
            self.ua_history.Redraw()
            try:
                self.LayoutChanged(IDC_HISTORY_BODY_UA)
                self.LayoutChanged(IDC_HISTORY_SCROLL)
            except Exception:
                pass
        self._save_state()
        return True


    def add_history_entry(self, path):
        """Adds an image path to the history list (avoids duplicates)."""
        if not path or not os.path.isfile(path):
            return False

        # Skip if this path is already in history
        try:
            for entry in self.history:
                if entry.path == path:
                    return True
        except Exception:
            pass

        bmp = load_bitmap(path)
        if not bmp:
            return False

        self.history.insert(0, SlotEntry(path=path, bmp=bmp))

        if self.ua_history:
            self.ua_history.Redraw()
            try:
                self.LayoutChanged(IDC_HISTORY_BODY_UA)
                self.LayoutChanged(IDC_HISTORY_SCROLL)
            except Exception:
                pass

        self._save_state()
        return True

    def delete_history_at(self, idx):
        try:
            idx = int(idx)
        except Exception:
            return False
        if idx < 0 or idx >= len(self.history):
            return False
        self.history.pop(idx)
        if self.ua_history:
            self.ua_history.Redraw()
            try:
                self.LayoutChanged(IDC_HISTORY_BODY_UA)
                self.LayoutChanged(IDC_HISTORY_SCROLL)
            except Exception:
                pass
        self._save_state()
        return True

    def send_history_to_refs(self, idx):
        """Adds a history item to the refs strip (Send to refs)."""
        try:
            idx = int(idx)
        except Exception:
            return False
        if idx < 0 or idx >= len(self.history):
            return False
        p = getattr(self.history[idx], "path", "")
        if not p or not os.path.isfile(p):
            return False
        ok = self.add_reference_from_path(p)
        if ok:
            self._append_chat(f"<< Sent to refs: {p}")
        return ok

    def add_reference_from_path(self, path):
        if not path or not os.path.isfile(path):
            return False
        bmp = load_bitmap(path)
        if not bmp:
            return False
        self.refs.append(SlotEntry(path=path, bmp=bmp))
        if self.ua_refs:
            self.ua_refs.Redraw()
        self._update_refs_visibility()
        self._save_state()
        return True

    def delete_reference_at(self, idx):
        try:
            idx = int(idx)
        except Exception:
            return False
        if idx < 0 or idx >= len(self.refs):
            return False
        self.refs.pop(idx)
        if self.ua_refs:
            self.ua_refs.Redraw()
        self.refs_sel = set()
        self._refs_last_idx = None
        self._update_refs_visibility()
        self._save_state()
        return True

    def _hold_preview_show_path(self, path):
        """Temporarily show an image in the main view while LMB is held."""
        if not path or not os.path.isfile(path):
            return False

        bmp = load_bitmap(path)
        if not bmp:
            return False

        self._hold_preview_restore = (self.view_bmp, self.view_path, self.zoom, self.zoom_manual)
        self._hold_preview_path = path

        self.view_bmp = bmp
        self.view_path = path
        if self.ua_main:
            self.ua_main.Redraw()

        bc = c4d.BaseContainer()
        while True:
            if not gui.GetInputState(c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_MOUSELEFT, bc):
                break
            if bc.GetLong(c4d.BFM_INPUT_VALUE) == 0:
                break

        try:
            if self._hold_preview_restore and self.view_path == self._hold_preview_path:
                old_bmp, old_path, old_zoom, old_zoom_manual = self._hold_preview_restore
                self.view_bmp = old_bmp
                self.view_path = old_path
                self.zoom = old_zoom
                self.zoom_manual = old_zoom_manual
                if self.ua_main:
                    self.ua_main.Redraw()
        finally:
            self._hold_preview_restore = None
            self._hold_preview_path = ""

        self._save_state()
        return True

    # ---------------- chat ----------------

    def _chat_scroll_bottom(self):
        """Moves caret to bottom of the chat log (auto-scrolls if needed)."""
        try:
            s = self.GetString(IDC_CHATLOG) or ""
        except Exception:
            return

        lines = s.splitlines()
        if not lines:
            line_idx = 0
            col_idx = 0
        else:
            line_idx = len(lines) - 1
            col_idx = len(lines[-1])

        try:
            self.SetMultiLinePos(IDC_CHATLOG, line_idx, col_idx)
        except Exception:
            pass

    def _append_chat(self, lines):
        if not lines:
            return
        if isinstance(lines, str):
            lines = [lines]
        cur = ""
        try:
            cur = self.GetString(IDC_CHATLOG) or ""
        except Exception:
            cur = self.chatlog_text or ""
        if cur and not cur.endswith("\n"):
            cur += "\n"
        cur += "\n".join([str(x) for x in lines]) + "\n"
        self.chatlog_text = cur
        try:
            self.SetString(IDC_CHATLOG, cur)
        except Exception:
            pass

        try:
            self._chat_scroll_bottom()
        except Exception:
            pass

        # Persist chat log to state on every append.
        try:
            self._save_state()
        except Exception:
            pass

    def _update_history_button_label(self):
        try:
            self.SetString(IDC_BTN_HISTORY, "Hide history" if self.history_visible else "Show history")
        except Exception:
            pass

    
    # ---------------- refs visibility / selection ----------------

    def _update_refs_visibility(self):
        """Hide refs strip when empty (keeps startup clean, matches NanoPro behavior)."""
        try:
            self.HideElement(IDC_UA_REFS, not bool(self.refs))
        except Exception:
            pass
        try:
            self.LayoutChanged(IDC_UA_REFS)
            self.LayoutChanged(1100)
        except Exception:
            pass

    def _refs_select(self, idx, shift=False, ctrl=False):
        """Update refs selection (Shift range, Ctrl toggle)."""
        try:
            idx = int(idx)
        except Exception:
            return
        if idx < 0 or idx >= len(self.refs):
            return

        if self.refs_sel is None:
            self.refs_sel = set()

        if shift and self._refs_last_idx is not None and 0 <= self._refs_last_idx < len(self.refs):
            a = min(self._refs_last_idx, idx)
            b = max(self._refs_last_idx, idx)
            rng = set(range(a, b + 1))
            if ctrl:
                # toggle range
                for i in rng:
                    if i in self.refs_sel:
                        self.refs_sel.remove(i)
                    else:
                        self.refs_sel.add(i)
            else:
                self.refs_sel = rng
        elif ctrl:
            if idx in self.refs_sel:
                self.refs_sel.remove(idx)
            else:
                self.refs_sel.add(idx)
        else:
            self.refs_sel = {idx}

        self._refs_last_idx = idx
        self._save_state()

    def delete_selected_references(self):
        """Delete currently selected references."""
        if not self.refs_sel:
            return False
        idxs = sorted([i for i in self.refs_sel if 0 <= i < len(self.refs)], reverse=True)
        if not idxs:
            return False
        for i in idxs:
            try:
                self.refs.pop(i)
            except Exception:
                pass
        self.refs_sel = set()
        self._refs_last_idx = None
        if self.ua_refs:
            self.ua_refs.Redraw()
        self._update_refs_visibility()
        self._save_state()
        return True

    # ---------------- native file dialog helpers ----------------

    def _native_pick_image_files(self, title="Select image(s)"):
        """Native multi-select file picker via subprocess bridge (PowerShell / AppleScript).
        Returns list of absolute file paths. Empty list on cancel/failure.
        """
        plat = (sys.platform or "").lower()

        if plat.startswith("win"):
            return self._pick_files_windows_powershell(title=title)
        if plat == "darwin":
            return self._pick_files_macos_osascript(title=title)

        # Fallback: C4D dialog (single selection)
        fn = storage.LoadDialog(
            type=c4d.FILESELECTTYPE_IMAGES,
            title=title,
            flags=c4d.FILESELECT_LOAD,
        )
        return [fn] if fn else []

    def _pick_files_windows_powershell(self, title="Select image(s)"):
        """Windows native OpenFileDialog with Multiselect via PowerShell/.NET."""
        safe_title = (title or "Select image(s)").replace("'", "''")

        ps_script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$f.Title = '{safe_title}'; "
            "$f.Multiselect = $true; "
            "$f.Filter = 'Image files (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff;*.gif)|*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff;*.gif|All Files (*.*)|*.*'; "
            "$res = $f.ShowDialog(); "
            "if ($res -eq 'OK') { $f.FileNames }"
        )

        cmd = ["powershell", "-NoProfile", "-STA", "-Command", ps_script]

        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.check_output(cmd, creationflags=creationflags)
        except Exception:
            return []

        try:
            s = out.decode("utf-8", errors="replace")
        except Exception:
            return []

        paths = []
        for line in s.splitlines():
            p = (line or "").strip()
            if p:
                paths.append(p)
        return paths

    def _pick_files_macos_osascript(self, title="Select image(s)"):
        """macOS native choose file dialog (multiple selection) via osascript."""
        safe_title = (title or "Select image(s)").replace('"', '\\"')
        script = (
            'set _files to choose file with prompt "%s" of type {"public.image"} with multiple selections allowed\n'
            'set _out to ""\n'
            'repeat with f in _files\n'
            'set _out to _out & (POSIX path of f) & "\\n"\n'
            'end repeat\n'
            'return _out\n' % safe_title
        )
        cmd = ["osascript", "-e", script]
        try:
            out = subprocess.check_output(cmd)
        except Exception:
            return []
        try:
            s = out.decode("utf-8", errors="replace")
        except Exception:
            return []
        paths = []
        for line in s.splitlines():
            p = (line or "").strip()
            if p:
                paths.append(p)
        return paths

# ---------------- HTTP ----------------

    def _http_post(self, url, data, headers):
        if headers is None:
            headers = {}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300, context=_SSL_UNVERIFIED_CTX) as resp:
                body = resp.read()
                return resp.getcode(), body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(e)
            return getattr(e, "code", 0) or 0, body
        except Exception as e:
            return 0, str(e)

    # ---------------- encoding ----------------

    def _encode_bitmap_to_base64_png(self, bmp):
        if not bmp:
            return None
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            res = bmp.Save(tmp_path, c4d.FILTER_PNG)
            if res != c4d.IMAGERESULT_OK:
                return None
            with open(tmp_path, "rb") as f:
                raw = f.read()
            return base64.b64encode(raw).decode("ascii")
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def _encode_file_to_base64(self, path):
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as f:
                raw = f.read()
            return base64.b64encode(raw).decode("ascii")
        except Exception:
            return None

    # ---------------- outputs ----------------

    def _detect_image_ext_from_bytes(self, raw):
        if not raw:
            return ".png"
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if raw[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return ".webp"
        return ".png"

    def _get_next_output_path(self, ext=".png"):
        return self._get_unique_image_path(prefix="banana2", ext=ext)

    # ---------------- render ----------------
    def _bake_ocio_view_transform(self, linear_bmp, render_data=None):
        """Bake the project OCIO display/view transform after a raw render.

        This is the Monster Framebuffer workflow: render into a render bitmap first,
        then call BakeOcioViewToBitmap on the Cinema 4D main thread. The render data
        used for the render is reused for the bake so the result matches the project.
        """
        if linear_bmp is None:
            return None

        bake_fn = getattr(documents, "BakeOcioViewToBitmap", None)
        if bake_fn is None:
            return linear_bmp

        rd = render_data
        if rd is None:
            doc = documents.GetActiveDocument()
            if not doc:
                return linear_bmp
            try:
                rd = doc.GetActiveRenderData().GetDataInstance().GetClone(c4d.COPYFLAGS_0)
            except Exception:
                try:
                    rd = c4d.BaseContainer(doc.GetActiveRenderData().GetDataInstance())
                except Exception:
                    return linear_bmp

        # The working Monster/NanoPro path forces a display-oriented format before bake.
        try:
            rd_for_bake = rd.GetClone(c4d.COPYFLAGS_0)
        except Exception:
            try:
                rd_for_bake = c4d.BaseContainer(rd)
            except Exception:
                rd_for_bake = rd

        try:
            rd_for_bake[c4d.RDATA_FORMAT] = c4d.FILTER_JPG
        except Exception:
            pass

        baked_bmp = None
        try:
            baked_bmp = bake_fn(linear_bmp, rd_for_bake, c4d.SAVEBIT_NONE)
        except TypeError:
            # Compatibility fallback for builds/wrappers with a fussier argument type.
            try:
                doc = documents.GetActiveDocument()
                baked_bmp = bake_fn(
                    linear_bmp,
                    doc.GetActiveRenderData().GetDataInstance(),
                    c4d.SAVEBIT_NONE,
                ) if doc else None
            except Exception:
                baked_bmp = None
        except Exception:
            baked_bmp = None

        out_bmp = baked_bmp if baked_bmp else linear_bmp

        # Once the view transform is baked into pixels, do not let C4D apply it again.
        try:
            null_prof = c4d.bitmaps.ColorProfile()
            out_bmp.SetColorProfile(null_prof, c4d.COLORPROFILE_INDEX_RENDERSPACE)
            out_bmp.SetColorProfile(null_prof, c4d.COLORPROFILE_INDEX_DISPLAYSPACE)
            out_bmp.SetColorProfile(null_prof, c4d.COLORPROFILE_INDEX_VIEW_TRANSFORM)
        except Exception:
            pass

        return out_bmp


    def _update_live_render_preview(self):
        """Show a color-managed snapshot of the framebuffer without touching raw render pixels.

        Called by the dialog timer while the renderer is running. The render thread keeps
        writing into self._render_bmp; the UI only ever receives a cloned snapshot.
        """
        raw_bmp = self._render_bmp
        if raw_bmp is None:
            return False

        # Never bake the framebuffer the renderer owns. Work on a snapshot instead.
        try:
            snapshot = raw_bmp.GetClone()
        except Exception:
            snapshot = None
        if snapshot is None:
            return False

        try:
            preview = self._bake_ocio_view_transform(snapshot, render_data=self._render_rd)
        except Exception:
            preview = snapshot
        if preview is None:
            preview = snapshot

        self._render_preview_bmp = preview
        self.view_bmp = preview
        self.view_path = ""
        if self.ua_main:
            self.ua_main.Redraw()
        return True


    def _allocate_render_bitmap(self, rd_bc, w, h):
        """Allocate a MultipassBitmap like Monster Framebuffer, with old-C4D fallbacks."""
        # Cinema 4D 2026.2+: lets C4D allocate the exact render framebuffer it wants.
        try:
            alloc = getattr(bitmaps, "AllocateRenderBitmap", None)
            if callable(alloc):
                bmp = alloc(rd_bc)
                if bmp is not None:
                    return bmp
        except Exception:
            pass

        # Older versions: explicit floating-point MultipassBitmap.
        try:
            bmp = bitmaps.MultipassBitmap(int(w), int(h), c4d.COLORMODE_RGBf)
            if bmp is not None:
                try:
                    bmp.AddChannel(True, True)
                except Exception:
                    pass
                return bmp
        except Exception:
            pass

        # Last fallback keeps rendering functional even if MultipassBitmap allocation fails.
        bmp = bitmaps.BaseBitmap()
        try:
            init_res = bmp.Init(int(w), int(h), 96)
        except Exception:
            init_res = bmp.Init(int(w), int(h), 32)
        if isinstance(init_res, tuple):
            init_res = init_res[0]
        if init_res != c4d.IMAGERESULT_OK:
            return None
        return bmp


    def _start_render(self):
        doc = documents.GetActiveDocument()
        if not doc:
            self._append_chat("<< No active document.")
            return

        rd = doc.GetActiveRenderData()
        if not rd:
            self._append_chat("<< No render settings.")
            return

        try:
            rd_bc = rd.GetDataInstance().GetClone(c4d.COPYFLAGS_0)
        except Exception:
            try:
                rd_bc = c4d.BaseContainer(rd.GetDataInstance())
            except Exception:
                rd_bc = c4d.BaseContainer(rd.GetData())

        w = int(rd_bc.GetInt32(c4d.RDATA_XRES)) or 1280
        h = int(rd_bc.GetInt32(c4d.RDATA_YRES)) or 720

        # Raw render. Project display/view transform is baked only when render finishes/stops.
        try:
            rd_bc[c4d.RDATA_BAKE_OCIO_VIEW_TRANSFORM_RENDER] = False
        except Exception:
            pass

        bmp = self._allocate_render_bitmap(rd_bc, w, h)
        if bmp is None:
            self._append_chat("<< Failed to allocate render bitmap.")
            return

        # Keep the previous image only for a failed render. During rendering the shared
        # framebuffer is the main preview, so Cinema's buckets/tiles appear progressively.
        self._render_prev_view_bmp = self.view_bmp
        self._render_prev_view_path = self.view_path
        self._render_bmp = bmp
        self._render_preview_bmp = None
        self._render_rd = rd_bc
        self._render_done = False
        self._render_err = None

        self.view_bmp = bmp
        self.view_path = ""
        if self.ua_main:
            self.ua_main.Redraw()

        self._render_thread = NanoProRenderThread(doc, rd_bc, bmp)
        self._render_thread.Start()

        try:
            self.SetString(IDC_BTN_RENDER, "Stop")
        except Exception:
            pass

        self._set_status("Rendering...")


    def _commit_render_bitmap(self, raw_bmp, render_data, failed_message=None):
        """Bake OCIO and commit a complete or partial render into main/history."""
        if raw_bmp is None:
            if failed_message:
                self._append_chat(failed_message)
            self.view_bmp = self._render_prev_view_bmp
            self.view_path = self._render_prev_view_path
            if self.ua_main:
                self.ua_main.Redraw()
            return False

        try:
            self._set_status("Baking OCIO...")
        except Exception:
            pass

        try:
            baked = self._bake_ocio_view_transform(raw_bmp, render_data=render_data)
        except Exception:
            baked = raw_bmp
        if baked is None:
            baked = raw_bmp

        path = self._save_bitmap_to_images_dir(baked, prefix="render")
        if path:
            self.set_main_image_from_path(path)
            self.add_history_entry(path)
        else:
            self.view_bmp = baked
            self.view_path = ""
            if self.ua_main:
                self.ua_main.Redraw()
            self._append_chat("<< Render done (not saved).")
        return True


    def _clear_render_state(self):
        self._render_thread = None
        self._render_bmp = None
        self._render_preview_bmp = None
        self._render_rd = None
        self._render_prev_view_bmp = None
        self._render_prev_view_path = ""
        self._render_done = False
        self._render_err = None
        try:
            self.SetString(IDC_BTN_RENDER, "Render")
        except Exception:
            pass
        self._set_status("")


    def _stop_render(self):
        """Stop rendering and keep the current partially rendered framebuffer, like NanoPro."""
        th = self._render_thread
        raw_bmp = self._render_bmp
        render_data = self._render_rd

        if th:
            try:
                th.End()
            except Exception:
                pass
            try:
                th.Wait(True)
            except Exception:
                pass

        try:
            self._commit_render_bitmap(raw_bmp, render_data)
        finally:
            self._clear_render_state()
            if self.ua_main:
                self.ua_main.Redraw()

    # ---------------- generate ----------------

    def DoGenerate(self):
        if self._gen_in_progress:
            return

        if not self.view_bmp:
            self._append_chat("<< No main image. Render or Load first.")
            return

        prompt = (self.GetString(IDC_PROMPT) or "").strip()

        # Log user prompt (persisted in state).
        if prompt:
            self._append_chat([">> " + prompt])
        else:
            self._append_chat([">> (empty prompt)"])

        aspect_ratio = "auto"
        if 0 <= self.ar_index < len(ASPECT_RATIO_ITEMS):
            aspect_ratio = ASPECT_RATIO_ITEMS[self.ar_index] or "auto"

        main_b64 = self._encode_bitmap_to_base64_png(self.view_bmp)

        # If user selected refs (Shift/Ctrl), use selection. Otherwise use all.
        refs_b64 = []
        if self.refs:
            if self.refs_sel:
                indices = [i for i in sorted(self.refs_sel) if 0 <= i < len(self.refs)]
            else:
                indices = list(range(len(self.refs)))

            for i in indices:
                e = self.refs[i]
                if e.path:
                    b = self._encode_file_to_base64(e.path)
                    if b:
                        refs_b64.append(b)

        job = {
            "api_key": (self.google_api_key or "").strip(),
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "main_b64": main_b64,
            "refs_b64": refs_b64,
        }

        self._start_generate_job(job)

        # Clear refs after отправки на генерацию (requested).
        try:
            self.refs = []
            self.refs_sel = set()
            self._refs_last_idx = None
            if self.ua_refs:
                self.ua_refs.Redraw()
            self._update_refs_visibility()
            self._save_state()
        except Exception:
            pass

    def _start_generate_job(self, job):
        self._gen_in_progress = True
        self._gen_result = None
        self._gen_error = None
        self._gen_started_at = time.time()
        self._gen_last_elapsed = -1

        try:
            self.Enable(IDC_BTN_GENERATE, False)
        except Exception:
            pass
        try:
            self.SetString(IDC_BTN_GENERATE, "Generating 0s")
        except Exception:
            pass

        self._set_status("Generating...")

        def _worker():
            try:
                self._gen_result = self._run_generate_job(job)
            except Exception as e:
                self._gen_error = str(e) + "\n" + traceback.format_exc()
            finally:
                self._gen_in_progress = False

        import threading
        self._gen_thread = threading.Thread(target=_worker, daemon=True)
        self._gen_thread.start()

    def _run_generate_job(self, job):
        # Must not touch C4D UI.
        args = {
            "api_key": (job.get("api_key") or "").strip(),
            "prompt": (job.get("prompt") or "").strip(),
            "aspect_ratio": job.get("aspect_ratio") or "auto",
            "output_count": int(job.get("output_count") or 1),
            "main_b64": job.get("main_b64"),
            "refs_b64": job.get("refs_b64") or [],
            "http_post": self._http_post,
        }

        result = banana_generate(args) or {}
        chat_lines = result.get("chat_lines") or []
        images = result.get("images") or []

        out_paths = []
        for raw in images:
            if not raw:
                continue
            ext = self._detect_image_ext_from_bytes(raw)
            out_path = self._get_next_output_path(ext=ext)
            try:
                with open(out_path, "wb") as f:
                    f.write(raw)
                out_paths.append(out_path)
                chat_lines.append("<< Output saved: " + out_path)
            except Exception as e:
                chat_lines.append("<< Failed to save output: " + str(e))

        if not out_paths:
            return {"status": "No output.", "chat_lines": chat_lines, "out_paths": []}
        return {"status": "Success.", "chat_lines": chat_lines, "out_paths": out_paths}

    # ---------------- UI lifecycle ----------------

    def CreateLayout(self):
        self.SetTitle("Nano C4D 1.5")

        self.GroupBegin(1000, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, cols=2, rows=1)

        # LEFT
        self.GroupBegin(1100, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, cols=1, rows=5)

        # Toolbar
        self.GroupBegin(1110, c4d.BFH_SCALEFIT, cols=6, rows=1)

        self.AddButton(IDC_BTN_RENDER, c4d.BFH_LEFT, name="Render")
        self.AddButton(IDC_BTN_LOAD, c4d.BFH_LEFT, name="Load")
        self.AddButton(IDC_BTN_APIKEY, c4d.BFH_LEFT, name="API key...")

        self.AddComboBox(IDC_AR_COMBO, c4d.BFH_LEFT, initw=140, allowfiltering=False)
        for i, name in enumerate(ASPECT_RATIO_ITEMS):
            self.AddChild(IDC_AR_COMBO, i, "Aspect ratio %s" % name)

        self.AddStaticText(IDC_TOOLBAR_SPACER, c4d.BFH_SCALEFIT, initw=0, name="")

        self.AddButton(IDC_BTN_HISTORY, c4d.BFH_RIGHT, name="Hide history")

        self.GroupEnd()

        # Main preview in scroll group
        self.ScrollGroupBegin(1120,
                              c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_HORIZ | c4d.SCROLLGROUP_BORDERIN,
                              0, 0)
        self.AddUserArea(IDC_UA_MAIN, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT)
        self.ua_main = PreviewArea(self)
        self.AttachUserArea(self.ua_main, IDC_UA_MAIN)
        self.GroupEnd()

        # Chat
        self.AddMultiLineEditText(IDC_CHATLOG, c4d.BFH_SCALEFIT | c4d.BFV_BOTTOM, initw=300, inith=120)

        # Refs
        self.AddUserArea(IDC_UA_REFS, c4d.BFH_SCALEFIT | c4d.BFV_BOTTOM, inith=REFS_AREA_H)
        self.ua_refs = ReferencesArea(self)
        self.AttachUserArea(self.ua_refs, IDC_UA_REFS)

        # Prompt row
        self.GroupBegin(1130, c4d.BFH_SCALEFIT | c4d.BFV_BOTTOM, cols=3, rows=1)
        self.AddEditText(IDC_PROMPT, c4d.BFH_SCALEFIT, initw=300)
        self.AddButton(IDC_BTN_ADD_REF, c4d.BFH_LEFT, name="+ Ref")
        self.AddButton(IDC_BTN_GENERATE, c4d.BFH_RIGHT, initw=160, name="Generate")
        self.GroupEnd()

        self.GroupEnd()  # LEFT

        # RIGHT history
        self.ScrollGroupBegin(IDC_HISTORY_SCROLL,
                              c4d.BFH_LEFT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_BORDERIN,
                              0, 0)
        self.AddUserArea(IDC_HISTORY_BODY_UA, c4d.BFH_LEFT | c4d.BFV_TOP, initw=210, inith=200)
        self.ua_history = HistoryArea(self)
        self.AttachUserArea(self.ua_history, IDC_HISTORY_BODY_UA)
        self.GroupEnd()

        self.GroupEnd()  # main two-column
        return True

    def InitValues(self):
        if not self.chatlog_text.strip():
            header_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            self.chatlog_text = "[%s] Nano C4D 1.6 loaded. Model: Nano Banana 2\n" % header_time

        self.SetString(IDC_CHATLOG, self.chatlog_text)

        try:
            self._chat_scroll_bottom()
        except Exception:
            pass
        self.SetString(IDC_PROMPT, "")

        self.SetInt32(IDC_AR_COMBO, max(0, min(len(ASPECT_RATIO_ITEMS) - 1, self.ar_index)))

        self._update_history_button_label()
        self._apply_history_visibility()

        # 2 Hz is enough for both bucket refresh and OCIO preview baking.
        self.SetTimer(500)
        return True

    def _apply_history_visibility(self):
        try:
            if self.ua_history:
                self.ua_history.Redraw()
                self.LayoutChanged(IDC_HISTORY_BODY_UA)
        except Exception:
            pass

        try:
            self.HideElement(IDC_HISTORY_SCROLL, not bool(self.history_visible))
        except Exception:
            pass

        # Force layout refresh so the left side expands/contracts immediately.
        try:
            self.LayoutChanged(IDC_HISTORY_SCROLL)
            self.LayoutChanged(1000)
        except Exception:
            pass

        self._update_refs_visibility()

    def Timer(self, msg):
        # Live render preview + completion.
        try:
            if self._render_thread:
                # Twice per second, snapshot the raw framebuffer and bake the Project OCIO
                # view transform onto that copy. The renderer's own bitmap stays untouched.
                if self._render_thread.IsRunning():
                    self._update_live_render_preview()

                if not self._render_thread.IsRunning():
                    ok = self._render_thread.ok
                    err = self._render_thread.err
                    bmp = self._render_bmp
                    render_data = self._render_rd

                    if ok and bmp:
                        self._commit_render_bitmap(bmp, render_data)
                    else:
                        self._commit_render_bitmap(
                            None,
                            render_data,
                            failed_message="<< Render failed: %s" % (err or "unknown error"),
                        )

                    self._clear_render_state()
        except Exception:
            pass

        # Generation elapsed and completion
        if self._gen_in_progress:
            try:
                elapsed = int(time.time() - float(self._gen_started_at or time.time()))
                if elapsed != self._gen_last_elapsed:
                    self._gen_last_elapsed = elapsed
                    try:
                        self.SetString(IDC_BTN_GENERATE, "Generating %ds" % elapsed)
                    except Exception:
                        pass
            except Exception:
                pass
            return

        if self._gen_result is not None or self._gen_error is not None:
            result = self._gen_result
            err = self._gen_error
            self._gen_result = None
            self._gen_error = None

            try:
                self.Enable(IDC_BTN_GENERATE, True)
            except Exception:
                pass
            try:
                self.SetString(IDC_BTN_GENERATE, "Generate")
            except Exception:
                pass

            if err:
                self._append_chat("<< Generation error:\n" + str(err))
                self._set_status("")
                return

            if isinstance(result, dict):
                self._append_chat(result.get("chat_lines") or [])
                out_paths = result.get("out_paths") or []
                if out_paths:
                    # Show first output as main and add all to history
                    self.set_main_image_from_path(out_paths[0])
                    for p in out_paths:
                        self.add_history_entry(p)

                    try:
                        if self.ua_history:
                            self.ua_history.Redraw()
                            self.LayoutChanged(IDC_HISTORY_BODY_UA)
                            self.LayoutChanged(IDC_HISTORY_SCROLL)
                    except Exception:
                        pass
            else:
                self._append_chat("<< Generation finished.")
            self._set_status("")

    # ---------------- commands ----------------

    def Command(self, mid, bc):
        if mid == IDC_BTN_RENDER:
            if self._render_thread and self._render_thread.IsRunning():
                self._stop_render()
            else:
                self._start_render()
            return True

        if mid == IDC_BTN_LOAD:
            self.DoLoad()
            return True

        if mid == IDC_BTN_APIKEY:
            dlg = ApiKeyDialog(self.google_api_key)
            dlg.Open(c4d.DLG_TYPE_MODAL, defaultw=520, defaulth=0)
            if dlg.result_key is not None:
                self.google_api_key = dlg.result_key
                self._save_settings()
                self._append_chat("<< Google API key updated.")
            return True

        if mid == IDC_AR_COMBO:
            self.ar_index = int(self.GetInt32(IDC_AR_COMBO))
            self._save_state()
            return True

        if mid == IDC_BTN_HISTORY:
            self.history_visible = not self.history_visible
            self._apply_history_visibility()
            self._update_history_button_label()
            self._save_state()
            return True

        if mid == IDC_BTN_ADD_REF:
            self.DoAddRef()
            return True

        if mid == IDC_BTN_GENERATE:
            self.DoGenerate()
            return True

        if mid == IDC_CHATLOG:
            try:
                self.chatlog_text = self.GetString(IDC_CHATLOG) or ""
            except Exception:
                pass
            self._save_state()
            return True

        return False

    def DoLoad(self):
        path = storage.LoadDialog(title="Load image", flags=c4d.FILESELECT_LOAD, force_suffix="")
        if not path:
            return
        if self.set_main_image_from_path(path):
            self._append_chat("<< Loaded: %s" % os.path.basename(path))
            self.add_history_entry(path)

    def DoAddRef(self):
        paths = self._native_pick_image_files(title="Add reference image(s)")
        if not paths:
            return

        added = 0
        for p in paths:
            if self.add_reference_from_path(p):
                added += 1

        if added == 1:
            self._append_chat("<< Ref added: %s" % os.path.basename(paths[0]))
        elif added > 1:
            self._append_chat("<< Refs added: %d" % added)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_dlg = None


def main():
    global _dlg
    if _dlg is None:
        _dlg = NanoProDemoDialog()
    _dlg.Open(c4d.DLG_TYPE_ASYNC, defaultw=1100, defaulth=720)


if __name__ == "__main__":
    main()