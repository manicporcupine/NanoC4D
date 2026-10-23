# -*- coding: utf-8 -*-
# Nano C4D — final minimal patch (fit + anti-flicker + progress)
# Взято ровно из твоего baseline и точечно допатчено:
# 1) Вернул чекбокс у main image (IDC_CB_A) и включил его по умолчанию.
# 2) Большое превью не «обрезается»: всегда вписываем картинку в видимую область.
# 3) При удалении — чистая заливка и жёсткий одинарный полный редроу (без расколбаса лэйаута).
# 4) Вернул индикаторы прогресса: Render и Generate — статус-спиннер/бар.
# 5) ✅ Добавлен ComboBox справа от "Generate" со списком aspect ratio (Gemini 2.5 Flash Image) + default "auto".

import c4d
from c4d import gui, documents, bitmaps, storage
import os, glob, re, subprocess, shutil, json, time

# ---------------- IDs / constants ----------------
IDC_UA_MAIN      = 1000
IDC_CHATLOG      = 1005

IDC_RIGHT_SCROLL = 1090
IDC_RIGHT_BODY   = 1091

IDC_UA_A         = 1010
IDC_UA_B         = 1011
IDC_UA_C         = 1012
IDC_UA_D         = 1013

# ✅ main checkbox возвращён
IDC_CB_A         = 1020
IDC_CB_B         = 1021
IDC_CB_C         = 1022
IDC_CB_D         = 1023

IDC_LBL_A        = 1040
IDC_LBL_B        = 1041
IDC_LBL_C        = 1042
IDC_LBL_D        = 1043

IDC_PROMPT       = 1030

IDC_BTN_GENERATE = 1032
IDC_BTN_RENDER   = 1033       # было REFRESH/Capture
IDC_BTN_APIKEY   = 1034

# ✅ Новый ComboBox для выбора aspect ratio (только UI, без сохранения/отправки)
IDC_AR_COMBO     = 1060
# Список из актуального API Gemini 2.5 Flash Image (+ 'auto' первым):
ASPECT_RATIO_ITEMS = [
    "auto",
    "1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"
]

# -------------- sizes / layout ----------------
RIGHT_W     = 160
CB_W        = 18
PREV_MAX    = 120.0
V_PAD       = 6
ROW_H_MIN   = int(PREV_MAX) + V_PAD*2
TARGET_CENTER_X = RIGHT_W / 2.0  # 80

LOG_INIT_H  = 5*18 + 8

SETTINGS_FILENAME = ".nano_gemini_settings.json"
STATE_FILENAME    = "state.json"

SLOT_A, SLOT_B, SLOT_C, SLOT_D = 0, 1, 2, 3

# ---------------- utils ----------------
def get_script_dir():
    """
    Возвращает папку, где лежит сам скрипт .py.
    1) Пытаемся через __file__ (если скрипт действительно загружен с диска).
    2) Если скрипт запущен из Script Manager и __file__ нет — используем
       стандартную папку скриптов в PREFS (где скрипт обычно и лежит).
    3) Как крайний случай — текущую рабочую папку, НО никогда не Windows\System32.
    """
    # 1) __file__ если доступен
    try:
        p = os.path.abspath(__file__)
        if os.path.isfile(p):
            return os.path.dirname(p)
    except Exception:
        pass

    # 2) PREFS\library\scripts — стандартное место для пользовательских скриптов
    try:
        prefs = storage.GeGetC4DPath(c4d.C4D_PATH_PREFS)
        if prefs:
            sdir = os.path.join(prefs, "library", "scripts")
            if os.path.isdir(sdir):
                return sdir
    except Exception:
        pass

    # 3) Fallback: CWD, но с защитой от System32
    cwd = os.getcwd()
    win = os.environ.get("WINDIR", r"C:\Windows")
    if os.path.normcase(os.path.abspath(cwd)).startswith(os.path.normcase(os.path.abspath(win))):
        # Если внезапно System32 — уйдём в домашнюю, чтобы не ловить WinError 5
        return os.path.expanduser("~")
    return cwd


def ensure_images_dir(base_dir):
    """
    Строго создаём nanoc4d_images рядом со скриптом.
    Если base_dir «вдруг» попал в Windows-директорию, пересчитаем через get_script_dir().
    """
    win = os.environ.get("WINDIR", r"C:\Windows")
    base = base_dir
    if os.path.normcase(os.path.abspath(base_dir)).startswith(os.path.normcase(os.path.abspath(win))):
        # Перестрахуемся на случай неизвестной рабочей папки
        base = get_script_dir()

    d = os.path.join(base, "nanoc4d_images")
    os.makedirs(d, exist_ok=True)
    return d


def _next_index(images_dir, prefix):
    mx = 0
    for p in glob.glob(os.path.join(images_dir, f"{prefix}_*.jpg")):
        m = re.search(rf"{re.escape(prefix)}_(\d+)\.jpg$", os.path.basename(p), re.I)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1

def numbered_path(images_dir, prefix):
    return os.path.join(images_dir, f"{prefix}_{_next_index(images_dir, prefix):03d}.jpg")

def load_bitmap(path):
    if not path or not os.path.isfile(path):
        return None
    bmp = bitmaps.BaseBitmap()
    res, _ = bmp.InitWith(path)
    return bmp if res == c4d.IMAGERESULT_OK else None

def save_bitmap_jpg(bmp, path):
    return bool(bmp) and (bmp.Save(path, c4d.FILTER_JPG, None) == c4d.IMAGERESULT_OK)

def _mix(c1, c2, a):
    return c1*(1.0-a) + c2*a

def _mul(c, s):
    return c4d.Vector(min(c.x*s,1.0), min(c.y*s,1.0), min(c.z*s,1.0))

# ---------- data model ----------
class SlotEntry:
    __slots__ = ("path","bmp")
    def __init__(self, path=None, bmp=None):
        self.path = path
        self.bmp  = bmp

# ---------- spacers ----------
class WidthSpacer(gui.GeUserArea):
    def GetMinSize(self): return (RIGHT_W, 1)
    def DrawMsg(self, *args): return True

# ---------- left preview ----------
class PreviewArea(gui.GeUserArea):
    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        self._bg = gui.GetGuiWorldColor(c4d.COLOR_BG)

    def _effective_zoom(self, w, h, bw, bh):
        """AUTO: 100% если влазит, иначе Fit. MANUAL: текущее значение."""
        if not getattr(self.dlg, 'zoom_manual', False):
            fit = min(w/float(bw), h/float(bh))
            return 1.0 if (bw <= w and bh <= h) else fit
        return float(max(0.05, min(8.0, self.dlg.zoom)))

    def GetMinSize(self):
        # Возвращаем размеры с учётом текущего масштабирования, чтобы ScrollGroup показывал скроллбары при зуме >100%
        bmp = self.dlg.view_bmp
        if not bmp:
            return (600, 340)
        bw, bh = bmp.GetSize()
        w = max(1, self.GetWidth())
        h = max(1, self.GetHeight())
        if w <= 1 or h <= 1:
            w, h = 800, 450
        z = self._effective_zoom(w, h, bw, bh)
        dw, dh = max(1, int(bw*z)), max(1, int(bh*z))
        return (dw, dh)

    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        w = max(1, self.GetWidth()); h = max(1, self.GetHeight())
        self.SetClippingRegion(0,0,w,h)
        self.DrawSetPen(self._bg); self.DrawRectangle(0,0,w,h)

        bmp = self.dlg.view_bmp
        if not bmp:
            self.DrawSetTextCol(gui.GetGuiWorldColor(c4d.COLOR_TEXT), self._bg)
            self.DrawText("Warning! Rendering (especially progressive/very long) can freeze Cinema 4D until it finishes.", 12, 12, c4d.DRAWTEXT_STD_ALIGN)
            self.ClearClippingRegion(); return True

        bw, bh = bmp.GetSize()
        z = self._effective_zoom(w, h, bw, bh)
        dw, dh = max(1,int(bw*z)), max(1,int(bh*z))
        x = (w - dw)//2; y = (h - dh)//2
        self.DrawBitmap(bmp, x, y, dw, dh, 0, 0, bw, bh, c4d.BMP_NORMAL)
        self.ClearClippingRegion(); return True

    def InputEvent(self, msg):
        if msg.GetId() != c4d.BFM_INPUT:
            return False
        if msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_MOUSE and msg[c4d.BFM_INPUT_CHANNEL] == c4d.BFM_INPUT_MOUSEWHEEL:
            bmp = self.dlg.view_bmp
            if not bmp:
                return False
            bw, bh = bmp.GetSize(); w = max(1, self.GetWidth()); h = max(1, self.GetHeight())
            cur = self._effective_zoom(w, h, bw, bh) if not getattr(self.dlg, 'zoom_manual', False) else float(self.dlg.zoom)
            delta = msg.GetInt32(c4d.BFM_INPUT_VALUE)
            step = 1.1 ** (delta/120.0) if delta else 1.0
            self.dlg.zoom = max(0.05, min(8.0, cur * step))
            self.dlg.zoom_manual = True
            self.dlg.LayoutChanged(IDC_UA_MAIN)
            self.Redraw()
            return True
        return False

# ---------- right: label UA ----------
class LabelArea(gui.GeUserArea):
    def __init__(self, dlg, text, chk_id=None):
        super().__init__()
        self.dlg=dlg
        self.text=text
        self.chk_id=chk_id
        self._bg = gui.GetGuiWorldColor(c4d.COLOR_BG)
        self._text_base = _mul(gui.GetGuiWorldColor(c4d.COLOR_TEXT), 0.85)

    def GetMinSize(self):
        return (RIGHT_W - CB_W, 16)

    def DrawMsg(self, x1,y1,x2,y2,msg):
        self.OffScreenOn()
        w = max(1,x2-x1); h = max(1,y2-y1)
        self.DrawSetPen(self._bg); self.DrawRectangle(0,0,w,h)
        on = False
        if self.chk_id is not None:
            try: on = self.dlg.GetBool(self.chk_id)
            except Exception: on = False
        col = c4d.Vector(1,1,1) if on else self._text_base
        self.DrawSetTextCol(col, self._bg)
        self.DrawText(self.text, 0, 0, c4d.DRAWTEXT_STD_ALIGN)
        return True

# ---------- right: preview row UA ----------
class RowArea(gui.GeUserArea):
    """
    Старое поведение + фиксы:
      — даблклик: системный и таймаут-fallback;
      — при пустом слоте заливаем фон БЕЗ рамки (анти-мерцание);
      — Delete: обнуляем слот и локально перерисовываем.
    """
    def __init__(self, dlg, slot_idx, left_margin):
        super().__init__()
        self.dlg = dlg
        self.slot_idx = slot_idx
        self._bg  = gui.GetGuiWorldColor(c4d.COLOR_BG)
        self._pen = gui.GetGuiWorldColor(c4d.COLOR_TEXT)
        self._left_margin = int(left_margin)
        # для fallback-даблклика
        self._last_click_ts = 0.0

    def GetMinSize(self):
        return (RIGHT_W - CB_W, ROW_H_MIN)

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        w = max(1, self.GetWidth()); h = max(1, self.GetHeight())
        # Полная перерисовка: клип + заливка всей области
        self.SetClippingRegion(0,0,w,h)
        entry = self.dlg.slots[self.slot_idx]
        bmp = entry.bmp if entry else None

        # Сплошная заливка без рамок — чтобы при удалении ничего не «мигало»
        self.DrawSetPen(_mix(self._bg, c4d.Vector(1,1,1), 0.02))
        self.DrawRectangle(0,0,w,h)
        if not bmp:
            self.ClearClippingRegion()
            return True

        # Нормальная миниатюра
        bw, bh = bmp.GetSize()
        base = min(PREV_MAX / float(max(bw, bh)), 1.0)
        dw = max(1, int(round(bw * base)))
        dh = max(1, int(round(bh * base)))
        local_center = TARGET_CENTER_X - self._left_margin
        x = int(round(local_center - dw/2))
        x = self._clamp(x, 1, max(1, w - dw - 1))
        y = max(V_PAD, (h - dh)//2)
        self.DrawBitmap(bmp, x, y, dw, dh, 0, 0, bw, bh, c4d.BMP_NORMAL)
        self.ClearClippingRegion()
        return True

    def _open_picker(self):
        self.dlg.sel_idx = self.slot_idx
        self.Redraw()
        self.dlg._replace_slot_from_dialog(self.slot_idx)

    def InputEvent(self, msg):
        if msg.GetId()!=c4d.BFM_INPUT:
            return False
        dev = msg[c4d.BFM_INPUT_DEVICE]
        ch  = msg[c4d.BFM_INPUT_CHANNEL]

        # 1) системный двойной клик
        if dev==c4d.BFM_INPUT_MOUSE and ch == c4d.BFM_INPUT_DOUBLECLICK:
            self._open_picker()
            return True

        # 2) fallback: два быстрых ЛКМ
        if dev==c4d.BFM_INPUT_MOUSE and ch == c4d.BFM_INPUT_MOUSELEFT:
            now = time.time()
            if now - self._last_click_ts <= 0.35:
                self._open_picker()
                self._last_click_ts = 0.0
                return True
            self._last_click_ts = now
            self.dlg.sel_idx = self.slot_idx
            self.Redraw()
            return True

        # Delete/Backspace — удалить
        if dev==c4d.BFM_INPUT_KEYBOARD:
            kc = msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
            asc = msg.GetInt32(c4d.BFM_INPUT_ASC)
            if kc in (c4d.KEY_DELETE, c4d.KEY_BACKSPACE) or asc in (8,127):
                if self.slot_idx == SLOT_A:
                    self.dlg.slots[SLOT_A] = SlotEntry()
                    # ✅ аккуратная очистка большого превью без расколбаса Layout
                    self.dlg._clear_main_fast()
                elif self.slot_idx == SLOT_B:
                    self.dlg.slots[SLOT_B] = SlotEntry()
                elif self.slot_idx == SLOT_C:
                    self.dlg.slots[SLOT_C] = self.dlg.slots[SLOT_D]
                    self.dlg.slots[SLOT_D] = SlotEntry()
                elif self.slot_idx == SLOT_D:
                    self.dlg.slots[SLOT_D] = SlotEntry()
                # локальные перерисовки
                for ua in (self.dlg.ua_main, self.dlg.rowA, self.dlg.rowB, self.dlg.rowC, self.dlg.rowD):
                    if ua: ua.Redraw()
                self.dlg._save_state()
                return True
        return False

# ---------- API key dialog ----------
class ApiKeyDialog(gui.GeDialog):
    IDC_EDIT, IDC_OK, IDC_CANCEL = 2001, 2002, 2003
    def __init__(self, initial=""):
        super().__init__()
        self._initial=initial.strip()
        self.value=None
    def CreateLayout(self):
        self.SetTitle("Set API key")
        self.GroupBegin(10, c4d.BFH_SCALEFIT, cols=1, rows=2)
        self.AddStaticText(0, c4d.BFH_LEFT, name="Enter your API key (stored locally):")
        self.AddEditText(self.IDC_EDIT, c4d.BFH_SCALEFIT, initw=320)
        self.GroupEnd()
        self.GroupBegin(20, 0, cols=2, rows=1)
        self.AddButton(self.IDC_OK, c4d.BFH_LEFT, name="OK")
        self.AddButton(self.IDC_CANCEL, c4d.BFH_RIGHT, name="Cancel")
        self.GroupEnd()
        return True
    def InitValues(self):
        self.SetString(self.IDC_EDIT, self._initial or "")
        return True
    def Command(self, mid, bc):
        if mid==self.IDC_OK:
            self.value=(self.GetString(self.IDC_EDIT) or "").strip()
            self.Close(); return True
        if mid==self.IDC_CANCEL:
            self.value=None; self.Close(); return True
        return False

# ---------- main dialog ----------
class NanoDialog(gui.GeDialog):
    def __init__(self):
        super().__init__()
        self.script_dir = get_script_dir()
        self.images_dir = ensure_images_dir(self.script_dir)
        self.slots = [SlotEntry(), SlotEntry(), SlotEntry(), SlotEntry()]
        self.sel_idx = SLOT_A
        self.zoom = 1.0
        self.zoom_manual = False
        self.zoom_manual = False
        self.view_bmp = None
        self.zoom_manual = False
        self.zoom = 1.0
        # даблклик трекинг для fallback
        self._last_click_ts = 0.0
        self._last_click_idx = None
        # external + settings
        self.external_py = os.path.join(self.script_dir, "external_gen.txt")
        self.settings_path = os.path.join(os.path.expanduser("~"), SETTINGS_FILENAME)
        self.api_key = ""
        self._load_settings()
        # user areas
        self.ua_main = None
        self.rowA = self.rowB = self.rowC = self.rowD = None
        self.lblA = self.lblB = self.lblC = self.lblD = None
        # state
        self.state_path = os.path.join(self.images_dir, STATE_FILENAME)
        self._load_state()

    # settings I/O
    def _load_settings(self):
        try:
            if os.path.isfile(self.settings_path):
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    self.api_key = (json.load(f).get("api_key", "") or "")
        except Exception:
            pass
    def _save_settings(self):
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump({"api_key": self.api_key}, f)
        except Exception:
            pass

    # process helpers — как в старом (ключ видится SDK гарантированно)
    def _pick_system_python(self):
        for name in ("python.exe","python3.exe","python"):
            p = shutil.which(name)
            if p:
                return [p]
        return ["python"]
    def _prep_env_with_api_key(self):
        env = os.environ.copy()
        for k in ("PYTHONHOME","PYTHONPATH","VIRTUAL_ENV","CONDA_PREFIX"):
            env.pop(k, None)
        env.pop("REQUESTS_CA_BUNDLE", None)
        env.pop("SSL_CERT_FILE", None)
        # Старое поведение: чистим GEMINI_API_KEY и используем GOOGLE_API_KEY
        env.pop("GEMINI_API_KEY", None)
        if self.api_key:
            env["GOOGLE_API_KEY"] = self.api_key
        env.setdefault("GRPC_VERBOSITY", "ERROR")
        env.setdefault("GLOG_minloglevel", "2")
        return env

    # layout
    def CreateLayout(self):
        self.SetTitle("Nano C4D")
        self.GroupBegin(20, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, cols=2, rows=1)

        # LEFT column
        self.GroupBegin(21, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, cols=1, rows=3)
        # В этой строке теперь 5 колонок: Render | Prompt | API key | Generate | AR-Combo
        self.GroupBegin(211, c4d.BFH_SCALEFIT, cols=5, rows=1)
        self.AddButton(IDC_BTN_RENDER,   c4d.BFH_LEFT, name="↻Render")   # было Capture

        self.AddEditText(IDC_PROMPT,     c4d.BFH_SCALEFIT, initw=260)
        self.AddButton(IDC_BTN_APIKEY,   c4d.BFH_RIGHT, name="API key…")
        self.AddButton(IDC_BTN_GENERATE, c4d.BFH_RIGHT, name="Generate")

        # ✅ ComboBox с aspect ratio (справа от Generate)
        self.AddComboBox(IDC_AR_COMBO, c4d.BFH_RIGHT, initw=100, inith=0, allowfiltering=False)
        for idx, name in enumerate(ASPECT_RATIO_ITEMS):
            self.AddChild(IDC_AR_COMBO, idx, name)

        self.GroupEnd()

        self.ScrollGroupBegin(30, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_HORIZ | c4d.SCROLLGROUP_BORDERIN, 0, 0)
        self.AddUserArea(IDC_UA_MAIN, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT)
        self.ua_main = PreviewArea(self)
        self.AttachUserArea(self.ua_main, IDC_UA_MAIN)
        self.GroupEnd()

        self.AddMultiLineEditText(IDC_CHATLOG, c4d.BFH_SCALEFIT | c4d.BFV_BOTTOM | c4d.BFV_FIT,
                                  initw=300, inith=LOG_INIT_H)
        self.GroupEnd()

        # RIGHT column
        self.ScrollGroupBegin(IDC_RIGHT_SCROLL, c4d.BFH_LEFT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_BORDERIN, 0, 0)
        self.GroupBegin(IDC_RIGHT_BODY, c4d.BFH_LEFT | c4d.BFV_TOP, cols=1, rows=1)
        self.AddUserArea(9001, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W, inith=1)
        self.AttachUserArea(WidthSpacer(), 9001)

        # ✅ A: main image — с чекбоксом
        self.GroupBegin(41, c4d.BFH_LEFT | c4d.BFV_TOP, cols=2, rows=1)
        self.AddCheckbox(IDC_CB_A, c4d.BFH_LEFT | c4d.BFV_TOP, CB_W, 0, "")
        self.GroupBegin(411, c4d.BFH_LEFT | c4d.BFV_TOP, cols=1, rows=2)
        self.AddUserArea(IDC_LBL_A, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=16)
        self.lblA = LabelArea(self, "main image", IDC_CB_A); self.AttachUserArea(self.lblA, IDC_LBL_A)
        self.AddUserArea(IDC_UA_A, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=ROW_H_MIN)
        self.rowA = RowArea(self, SLOT_A, left_margin=CB_W); self.AttachUserArea(self.rowA, IDC_UA_A)
        self.GroupEnd(); self.GroupEnd()

        # B
        self.GroupBegin(42, c4d.BFH_LEFT | c4d.BFV_TOP, cols=2, rows=1)
        self.AddCheckbox(IDC_CB_B, c4d.BFH_LEFT | c4d.BFV_TOP, CB_W, 0, "")
        self.GroupBegin(421, c4d.BFH_LEFT | c4d.BFV_TOP, cols=1, rows=2)
        self.AddUserArea(IDC_LBL_B, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=16)
        self.lblB = LabelArea(self, "previous frame", IDC_CB_B); self.AttachUserArea(self.lblB, IDC_LBL_B)
        self.AddUserArea(IDC_UA_B, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=ROW_H_MIN)
        self.rowB = RowArea(self, SLOT_B, left_margin=CB_W); self.AttachUserArea(self.rowB, IDC_UA_B)
        self.GroupEnd(); self.GroupEnd()

        # C
        self.GroupBegin(43, c4d.BFH_LEFT | c4d.BFV_TOP, cols=2, rows=1)
        self.AddCheckbox(IDC_CB_C, c4d.BFH_LEFT | c4d.BFV_TOP, CB_W, 0, "")
        self.GroupBegin(431, c4d.BFH_LEFT | c4d.BFV_TOP, cols=1, rows=2)
        self.AddUserArea(IDC_LBL_C, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=16)
        self.lblC = LabelArea(self, "reference 1", IDC_CB_C); self.AttachUserArea(self.lblC, IDC_LBL_C)
        self.AddUserArea(IDC_UA_C, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=ROW_H_MIN)
        self.rowC = RowArea(self, SLOT_C, left_margin=CB_W); self.AttachUserArea(self.rowC, IDC_UA_C)
        self.GroupEnd(); self.GroupEnd()

        # D
        self.GroupBegin(44, c4d.BFH_LEFT | c4d.BFV_TOP, cols=2, rows=1)
        self.AddCheckbox(IDC_CB_D, c4d.BFH_LEFT | c4d.BFV_TOP, CB_W, 0, "")
        self.GroupBegin(441, c4d.BFH_LEFT | c4d.BFV_TOP, cols=1, rows=2)
        self.AddUserArea(IDC_LBL_D, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=16)
        self.lblD = LabelArea(self, "reference 2", IDC_CB_D); self.AttachUserArea(self.lblD, IDC_LBL_D)
        self.AddUserArea(IDC_UA_D, c4d.BFH_LEFT | c4d.BFV_TOP, initw=RIGHT_W-CB_W, inith=ROW_H_MIN)
        self.rowD = RowArea(self, SLOT_D, left_margin=CB_W); self.AttachUserArea(self.rowD, IDC_UA_D)
        self.GroupEnd(); self.GroupEnd()

        self.GroupEnd(); self.GroupEnd(); self.GroupEnd()
        return True

    def InitValues(self):
        # чекбоксы
        self.SetBool(IDC_CB_A, True)   # ✅ включена по умолчанию
        self.SetBool(IDC_CB_B, False)
        self.SetBool(IDC_CB_C, False)
        self.SetBool(IDC_CB_D, False)
        # чат: приветствие и автоскролл (как в новом)
        self.SetString(IDC_PROMPT, "Enter prompt here...")
        self.SetString(IDC_CHATLOG, "Welcome to Nano C4D. Click ↻Render to grab a frame, or double-click a thumbnail on the right to add references.\n")
        self._chat_scroll_bottom()

        # ✅ дефолт для AR — "auto"
        self.SetInt32(IDC_AR_COMBO, 0)

        # state -> UI
        self._update_preview_from_A()
        self._update_rows()
        return True

    # -------- UI helpers --------
    def _chat_scroll_bottom(self):
        try:
            s = self.GetString(IDC_CHATLOG) or ""
            # надёжное смещение каретки в самый низ
            self.SetMultiLinePos(IDC_CHATLOG, 10**9, len(s))
        except Exception:
            pass

    def _update_rows(self):
        for ua in (self.rowA, self.rowB, self.rowC, self.rowD, self.lblA, self.lblB, self.lblC, self.lblD):
            if ua: ua.Redraw()

    def _set_slot(self, idx, path):
        self.slots[idx] = SlotEntry(path=path, bmp=load_bitmap(path))
        try: self._save_state()
        except Exception: pass

    def _update_preview_from_A(self):
        a = self.slots[SLOT_A]
        self.view_bmp = a.bmp if (a and a.bmp) else None
        self.zoom_manual = False
        self.zoom = 1.0
        # сброс в AUTO (100% или Fit) при любой установке main
        self.zoom = 1.0
        self.zoom_manual = False
        if self.ua_main: self.ua_main.Redraw()
        # принудительно заставим окно полностью обновиться (убирает призраки вне dirty rect)
        self._force_full_redraw()
        try: self._save_state()
        except Exception: pass

    # ✅ аккуратная очистка большого превью
    def _clear_main_fast(self):
        self.view_bmp = None
        # сбрасываем масштаб в AUTO
        self.zoom = 1.0
        self.zoom_manual = False
        if self.ua_main:
            self.ua_main.Redraw()
        # одинарный строгий редроу — добиваем «призраков»
        try:
            c4d.DrawViews(c4d.DRAWFLAGS_FORCEFULLREDRAW)
        except Exception:
            pass
        c4d.EventAdd(c4d.EVENT_FORCEREDRAW)

    def _force_full_redraw(self):
        try:
            c4d.DrawViews(c4d.DRAWFLAGS_FORCEFULLREDRAW)
        except Exception:
            pass
        c4d.EventAdd(c4d.EVENT_FORCEREDRAW)

    def _replace_slot_from_dialog(self, idx):
        p = storage.LoadDialog(type=c4d.FILESELECTTYPE_IMAGES, title="Pick image", flags=c4d.FILESELECT_LOAD)
        if not p: return
        self._set_slot(idx, p)
        if idx == SLOT_A:
            self.sel_idx = SLOT_A
            self._update_preview_from_A()
        self._update_rows()
        try: self._save_state()
        except Exception: pass

    # -------- Render (from Render Settings) --------
    def _render_to_bitmap_baked(self):
        doc = documents.GetActiveDocument()
        if not doc: return None
        rd = doc.GetActiveRenderData()
        if not rd: return None
        rdata = rd.GetDataInstance()
        w = int(rdata.GetInt32(c4d.RDATA_XRES)) or 640
        h = int(rdata.GetInt32(c4d.RDATA_YRES)) or 360
        mp = bitmaps.MultipassBitmap(w, h, c4d.COLORMODE_RGBf)
        if mp is None: return None
        flags = c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_SHOWERRORS
        # ✅ индикатор статуса на время рендера
        gui.StatusSetText("Rendering…"); gui.StatusSetSpin()
        try:
            res = documents.RenderDocument(doc, rdata, mp, flags)
        finally:
            gui.StatusClear()
        if res != c4d.RENDERRESULT_OK: return None
        baked = documents.BakeOcioViewToBitmap(mp, rdata, c4d.SAVEBIT_NONE)
        return baked if baked else mp

    # -------- Log helpers --------
    def _log_append(self, text):
        try: prev = self.GetString(IDC_CHATLOG) or ""
        except Exception: prev = ""
        new = (prev + ("\n" if prev and not prev.endswith("\n") else "") + text).rstrip() + "\n"
        self.SetString(IDC_CHATLOG, new)
        self._chat_scroll_bottom()

    # -------- State I/O --------
    def _save_state(self):
        try:
            data = {
                "slot_paths": [s.path if s and s.path else "" for s in self.slots],
                "view_path": (self.slots[SLOT_A].path if self.slots[SLOT_A] and self.slots[SLOT_A].path else ""),
            }
            with open(os.path.join(self.images_dir, STATE_FILENAME), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_state(self):
        try:
            sp = os.path.join(self.images_dir, STATE_FILENAME)
            if not os.path.isfile(sp): return
            with open(sp, "r", encoding="utf-8") as f:
                data = json.load(f)
            paths = data.get("slot_paths", [])
            for i in range(min(4, len(paths))):
                p = paths[i]
                if p and os.path.isfile(p):
                    self.slots[i] = SlotEntry(path=p, bmp=load_bitmap(p))
                else:
                    self.slots[i] = SlotEntry()
        except Exception:
            pass

    # -------- Actions --------
    def _render_update_A_B(self):
        bmp = self._render_to_bitmap_baked()
        if not bmp:
            gui.MessageDialog("Render or OCIO bake failed."); return
        out_path = numbered_path(self.images_dir, "input")
        if not save_bitmap_jpg(bmp, out_path):
            gui.MessageDialog("Failed to save JPG."); return
        oldA = self.slots[SLOT_A]
        if oldA and oldA.path:
            self.slots[SLOT_B] = oldA
        self._set_slot(SLOT_A, out_path)
        self.sel_idx = SLOT_A
        self._update_preview_from_A()
        self._update_rows()
        try: self._save_state()
        except Exception: pass

    def _run_external_generate(self):
        prompt = (self.GetString(IDC_PROMPT) or "").strip()
        if prompt:
            self._log_append(f">> {prompt}")

        # внешний генератор рядом?
        if not os.path.isfile(self.external_py):
            gui.MessageDialog("external_gen.txt not found next to this script."); return

        # refs по чекбоксам, как раньше
        refs = []
        if self.GetBool(IDC_CB_B) and self.slots[SLOT_B] and self.slots[SLOT_B].path:
            refs.append(self.slots[SLOT_B].path)
        if self.GetBool(IDC_CB_C) and self.slots[SLOT_C] and self.slots[SLOT_C].path:
            refs.append(self.slots[SLOT_C].path)
        if self.GetBool(IDC_CB_D) and self.slots[SLOT_D] and self.slots[SLOT_D].path:
            refs.append(self.slots[SLOT_D].path)

        # main передаём только если чекбокс A включён и файл реально есть
        a = self.slots[SLOT_A]
        use_main = self.GetBool(IDC_CB_A) and a and a.path and os.path.isfile(a.path)

        # формируем команду
        py = self._pick_system_python()
        args = py + [self.external_py, "--images-dir", self.images_dir, "--prompt", prompt]
        if use_main:
            args += ["--main", a.path]
        if refs:
            args += ["--refs"] + refs
               # --- PROXY ASPECT RATIO -> external script ---
        try:
            sel = self.GetInt32(IDC_AR_COMBO)
            ar = ASPECT_RATIO_ITEMS[sel] if 0 <= sel < len(ASPECT_RATIO_ITEMS) else "auto"
        except Exception:
            ar = "auto"
        if ar and ar != "auto":
            args += ["--aspect", ar]
      

        env = self._prep_env_with_api_key()
        try:
            # статус-бар (пульс)
            gui.StatusSetText("Generating…")
            p = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                env=env
            )
            out_lines = []
            phase = 0
            last_tick = time.time()
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                out_lines.append(line)

                # пульс раз в ~60 мс
                if time.time() - last_tick > 0.06:
                    phase = (phase + 4) % 100
                    try:
                        gui.StatusSetBar(phase)
                    except Exception:
                        pass
                    last_tick = time.time()
                c4d.GeSyncMessage(c4d.EVMSG_TIMECHANGED)
            gui.StatusClear()

            out = "".join(out_lines)
            if out and out.strip():
                self._log_append("<< " + out.strip()[-1200:].replace('TEXT:', 'BANANA:'))

            # вытаскиваем путь готового файла из stdout
            m = re.search(r"Saved output:\s*(.+)$", out.strip(), re.I | re.M)
            saved = m.group(1).strip() if m else None
            if not saved:
                outs = sorted(glob.glob(os.path.join(self.images_dir, "output_*.jpg")), key=os.path.getmtime)
                if outs:
                    saved = outs[-1]

            if saved and os.path.isfile(saved):
                oldA = self.slots[SLOT_A]
                if oldA and oldA.path:
                    self.slots[SLOT_B] = oldA
                self._set_slot(SLOT_A, saved); self.sel_idx = SLOT_A
                self._update_preview_from_A(); self._update_rows(); self._save_state()
            else:
                gui.MessageDialog("Generation finished, but output path was not found.")

        except subprocess.CalledProcessError as e:
            gui.StatusClear()
            msg = e.output or str(e)
            self._log_append(f"<< ERROR: {msg.strip()[:1200]}")
            gui.MessageDialog("External script failed:\n" + (e.output[:2000] if e.output else str(e)))
        except Exception as ex:
            gui.StatusClear()
            self._log_append(f"<< ERROR: {str(ex)[:1200]}")
            gui.MessageDialog("Error:\n" + str(ex))



    def Command(self, mid, bc):
        if mid in (IDC_CB_A, IDC_CB_B, IDC_CB_C, IDC_CB_D):
            for lbl in (self.lblA, self.lblB, self.lblC, self.lblD):
                if lbl: lbl.Redraw()
            return True
        if mid == IDC_BTN_RENDER:
            self._render_update_A_B(); return True

        if mid == IDC_BTN_GENERATE:
            self._run_external_generate(); return True
        if mid == IDC_BTN_APIKEY:
            dlg = ApiKeyDialog(self.api_key or "")
            dlg.Open(c4d.DLG_TYPE_MODAL_RESIZEABLE, defaultw=360, defaulth=110)
            if dlg.value is not None:
                self.api_key = dlg.value.strip()
                if self.api_key: self._save_settings()
            return True
        # Комбо аспектов — ничего не делаем (только UI)
        return False

# ------- launcher -------
_DLG = None

def main():
    global _DLG
    if _DLG is None:
        _DLG = NanoDialog()
    _DLG.Open(c4d.DLG_TYPE_ASYNC, xpos=-2, ypos=-2, defaultw=1200, defaulth=760)

if __name__ == "__main__":
    main()
