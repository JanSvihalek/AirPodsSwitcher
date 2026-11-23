"""
Malé okno u hodin, které se otevře kliknutím na tray ikonu.

Vzhled podle návrhu "Okno": tmavá karta 360 px, stav, velké tlačítko,
klávesová zkratka a přepínač autostartu.

Proč Pillow místo tk widgetů: tk na Windows nevyhlazuje zaoblené tvary
(tlačítko, kroužek, přepínač by měly zubaté hrany). Kartu proto kreslíme
přes Pillow (tvary s 4x supersamplingem, text fontem Segoe UI) a v okně je
jen obrázek; kliknutí se vyhodnocují podle souřadnic.

Všechny metody třídy Popup se volají jen z vlákna tkinteru (hlavní vlákno).
"""

import ctypes
import logging
import os
import time
from ctypes import wintypes

import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk

log = logging.getLogger("AirPodsSwitch")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --------------------------------------------------------------------------
# Barvy a rozměry z návrhu (v CSS px při 100 % DPI)
# --------------------------------------------------------------------------

BG = "#1E293B"
TEXT = "#F8FAFC"
TEXT2 = "#CBD5E1"
DIVIDER = "#334155"
APP_BLUE = "#1D4ED8"
DOT_ON = "#60A5FA"        # připojeno – plný kruh
DOT_OFF = "#FB923C"       # odpojeno – kroužek
DOT_ERR = "#F87171"       # chyba – kroužek
DOT_UNKNOWN = "#94A3B8"   # stav ještě nezjištěn
BTN_ORANGE = "#F97316"
BTN_BLUE = "#3B82F6"
BTN_TEXT = "#0F172A"
BTN_BUSY = "#334155"
BTN_BUSY_TEXT = "#CBD5E1"
BADGE_BG = "#334155"
SWITCH_ON = "#3B82F6"
SWITCH_OFF = "#475569"
PICKER_BG = "#0F172A"     # rozbalený seznam sluchátek
PICKER_SEL = "#334155"    # vybraný řádek
DOT_IDLE = "#64748B"      # sluchátka v seznamu, která nejsou u PC
BAT_GREEN = "#22C55E"     # baterie > 30 %
BAT_AMBER = "#F59E0B"     # baterie <= 30 %
BAT_RED = "#EF4444"       # baterie <= 10 %

CARD_W = 360
CARD_H = 332              # výška se sbaleným seznamem
RADIUS = 16
MARGIN = 12               # odstup od hlavního panelu / okraje
SS = 4                    # supersampling pro tvary
ROW_H = 44                # řádek v seznamu sluchátek
PICKER_PAD = 6

# Pevná část rozložení (x0, y0, x1, y1) v CSS px – podle návrhu (padding 24, mezery 20)
R_APPICON = (24, 24, 64, 64)
R_HEADER = (24, 20, 336, 68)          # klikací hlavička (otevře seznam)


def layout(view):
    """Rozložení karty v CSS px; část pod hlavičkou se posune o rozbalený seznam."""
    L = {"header": R_HEADER, "rows": []}
    y = 84
    if view.get("battery"):
        L["battery_y"] = y + 8             # řádek baterie (výška 16) pod hlavičkou
        y += 16 + 20
    if view.get("picker_open"):
        n = max(1, len(view["devices"]))
        ph = PICKER_PAD * 2 + n * ROW_H
        L["panel"] = (24, y, 336, y + ph)
        for i, (name, _) in enumerate(view["devices"]):
            top = y + PICKER_PAD + i * ROW_H
            L["rows"].append(((24 + PICKER_PAD, top, 336 - PICKER_PAD, top + ROW_H), name))
        y += ph + 20
    dy = y - 84
    L.update(dot=(24, 99 + dy, 42, 117 + dy), title_y=97.5 + dy, sub_y=122.5 + dy)
    L.update(
        button=(24, 152 + dy, 336, 200 + dy),
        hotkey_y=232.5 + dy,
        divider_y=265 + dy,
        switch=(296, 286 + dy, 336, 308 + dy),
        switch_row=(24, 282 + dy, 336, 312 + dy),
        auto=(296, 328 + dy, 336, 350 + dy),
        auto_row=(24, 324 + dy, 336, 354 + dy),
        media=(296, 370 + dy, 336, 392 + dy),
        media_row=(24, 366 + dy, 336, 396 + dy),
        height=CARD_H + 84 + dy,
    )
    return L


# --------------------------------------------------------------------------
# DPI, monitory, hlavní panel
# --------------------------------------------------------------------------

def set_dpi_awareness():
    """Per-monitor DPI awareness – musí se volat před vytvořením oken."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class _APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
                ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


_user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_user32.MonitorFromPoint.restype = wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
_shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(_APPBARDATA)]
_shell32.SHAppBarMessage.restype = ctypes.c_size_t

MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
ABM_GETTASKBARPOS = 5


def cursor_pos():
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _monitor(pt):
    hmon = _user32.MonitorFromPoint(wintypes.POINT(*pt), MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    _user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    return hmon, info.rcMonitor, info.rcWork


def _monitor_dpi(hmon):
    try:
        x, y = wintypes.UINT(), wintypes.UINT()
        if ctypes.WinDLL("shcore").GetDpiForMonitor(hmon, 0, ctypes.byref(x),
                                                    ctypes.byref(y)) == 0:
            return x.value
    except Exception:
        pass
    return 96


def _taskbar():
    """(hrana, RECT) hlavního panelu, nebo (None, None)."""
    abd = _APPBARDATA()
    abd.cbSize = ctypes.sizeof(abd)
    if not _shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd)):
        return None, None
    return {0: "left", 1: "top", 2: "right", 3: "bottom"}.get(abd.uEdge), abd.rc


def compute_position(w, h, pt, scale):
    """Levý horní roh okna u hodin podle umístění hlavního panelu.

    Hranu panelu poznáme podle rozdílu mezi celou plochou monitoru a
    pracovní plochou. Když je panel automaticky skrývaný (pracovní plocha =
    celý monitor), vezmeme hranu i tloušťku z SHAppBarMessage.
    """
    _, mon, work = _monitor(pt)
    m = round(MARGIN * scale)
    edge, extra = None, 0
    if work.bottom < mon.bottom:
        edge = "bottom"
    elif work.top > mon.top:
        edge = "top"
    elif work.left > mon.left:
        edge = "left"
    elif work.right < mon.right:
        edge = "right"
    else:
        tb_edge, rc = _taskbar()
        edge = tb_edge or "bottom"
        if rc is not None and rc.left < mon.right and rc.right > mon.left \
                and rc.top < mon.bottom and rc.bottom > mon.top:
            extra = (rc.bottom - rc.top) if edge in ("top", "bottom") else (rc.right - rc.left)

    x = work.right - w - m
    y = work.bottom - h - m
    if edge == "bottom":
        y -= extra
    elif edge == "top":
        y = work.top + m + extra
    elif edge == "left":
        x = work.left + m + extra
    elif edge == "right":
        x -= extra
    # Nikdy mimo monitor
    x = max(mon.left, min(x, mon.right - w))
    y = max(mon.top, min(y, mon.bottom - h))
    return x, y


# --------------------------------------------------------------------------
# Kreslení karty
# --------------------------------------------------------------------------

_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_fonts = {}


def _font(px, bold=False):
    """Segoe UI (Semibold pro tučné) v pixelové velikosti."""
    key = (round(px), bold)
    if key not in _fonts:
        names = ["seguisb.ttf", "segoeuib.ttf"] if bold else ["segoeui.ttf"]
        for n in names:
            try:
                _fonts[key] = ImageFont.truetype(os.path.join(_FONT_DIR, n), key[0])
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default(size=key[0])
    return _fonts[key]


def render_app_icon(px, bg=APP_BLUE, stroke=None):
    """Ikona aplikace z návrhu: zaoblený čtverec (výchozí modrý) + bílá sluchátka.

    Používá se pro ikonu exe (plocha, Start) i tray ikonu (tam barva
    podle stavu). Pro malé velikosti je tah glyfu silnější, aby nezanikl.
    """
    big = px * SS
    k = big / 40                            # návrh: čtverec 40 px, glyf 26 px
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=10 * k, fill=bg)
    if stroke is None:
        stroke = 3.0 if px <= 16 else 2.6 if px <= 32 else 2.0 if px <= 48 else 1.8
    _draw_headphones(d, (7, 7, 26), k, stroke=stroke)
    return img.resize((px, px), Image.LANCZOS)


def _ellipsize(text, font, max_w):
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def _draw_headphones(d, box, k, color="#FFFFFF", stroke=1.8):
    """Glyf sluchátek z návrhu (SVG 24x24, tah 1.8) do čtverce box (CSS px)."""
    x0, y0, size = box
    u = size / 24 * k                      # 1 jednotka SVG v pixelech plátna
    ox, oy = x0 * k, y0 * k
    w = stroke * u
    P = lambda x, y: (ox + x * u, oy + y * u)
    # Oblouk: střed (12,12), poloměr 7 – Pillow kreslí tah dovnitř boxu
    r = 7 * u + w / 2
    cx, cy = P(12, 12)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=180, end=360, fill=color, width=round(w))
    for x in (5, 19):
        a, b = P(x, 12), P(x, 14)
        d.line([a, b], fill=color, width=round(w))
    # Mušle: obdélníky 4x6 se zaoblením 1.6 (jen obrys)
    for x in (3, 17):
        a, b = P(x, 14), P(x + 4, 20)
        d.rounded_rectangle([a[0] - w / 2, a[1] - w / 2, b[0] + w / 2, b[1] + w / 2],
                            radius=1.6 * u + w / 2, outline=color, width=round(w))


def _battery_positions(items, scale):
    """Rozmístění položek baterie v řádku: [(x_popisku, x_ikony, x_textu), ...] v CSS px."""
    f = _font(13 * scale)
    out, x = [], 24
    for label, pct, _ in items:
        lw = f.getlength(label) / scale
        tw = f.getlength(f"{pct} %") / scale
        out.append((x, x + lw + 6, x + lw + 6 + 24 + 6))
        x += lw + 6 + 24 + 6 + tw + 18
    return out


def _draw_battery_icon(d, x, cy, pct, charging, k):
    """Ikona baterie 22x11 + plíšek; výplň podle procent, blesk při nabíjení."""
    R = lambda r: [r[0] * k, r[1] * k, r[2] * k, r[3] * k]
    body = (x, cy - 5.5, x + 22, cy + 5.5)
    d.rounded_rectangle(R(body), radius=3 * k, outline=TEXT2, width=round(1.4 * k))
    d.rounded_rectangle(R((x + 22.6, cy - 2.5, x + 24.4, cy + 2.5)), radius=0.8 * k, fill=TEXT2)
    if pct > 0:
        color = BAT_RED if pct <= 10 else BAT_AMBER if pct <= 30 else BAT_GREEN
        inner_w = 22 - 2 * 2.6
        d.rounded_rectangle(R((x + 2.6, cy - 2.9, x + 2.6 + max(1.6, inner_w * pct / 100), cy + 2.9)),
                            radius=1.3 * k, fill=color)
    if charging:
        cx = x + 11
        bolt = [(cx + 1.2, cy - 6.5), (cx - 3.4, cy + 0.8), (cx - 0.4, cy + 0.8),
                (cx - 1.4, cy + 6.5), (cx + 3.4, cy - 1.0), (cx + 0.4, cy - 1.0)]
        d.polygon([(px * k, py * k) for px, py in bolt], fill=TEXT, outline=BG,
                  width=round(1.2 * k))


def render_card(view, scale):
    """Vykreslí kartu podle stavu. Vrací (PIL Image ve fyzických px, rozložení)."""
    L = layout(view)
    W, H = round(CARD_W * scale), round(L["height"] * scale)
    k = scale * SS
    big = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(big)
    R = lambda r: [r[0] * k, r[1] * k, r[2] * k, r[3] * k]

    state, busy = view["state"], view["busy"]

    # Ikona aplikace
    d.rounded_rectangle(R(R_APPICON), radius=10 * k, fill=APP_BLUE)
    _draw_headphones(d, (31, 31, 26), k)

    # Hlavička: jméno vybraných sluchátek + šipka (otevře seznam)
    f_title = _font(18 * scale, True)
    title = _ellipsize(view["title"], f_title, (336 - 76 - 24) * scale)
    title_w = f_title.getlength(title) / scale
    cx, cy = 76 + title_w + 12, 44
    pts = [(cx - 5, cy - 2.5), (cx, cy + 2.5), (cx + 5, cy - 2.5)]
    if view.get("picker_open"):
        pts = [(cx - 5, cy + 2.5), (cx, cy - 2.5), (cx + 5, cy + 2.5)]
    d.line([(x * k, y * k) for x, y in pts], fill=TEXT2, width=round(2 * k), joint="curve")

    # Rozbalený seznam sluchátek
    if "panel" in L:
        d.rounded_rectangle(R(L["panel"]), radius=10 * k, fill=PICKER_BG)
        for (r, name), (_, connected) in zip(L["rows"], view["devices"]):
            if name == view.get("selected"):
                d.rounded_rectangle(R(r), radius=8 * k, fill=PICKER_SEL)
            my = (r[1] + r[3]) / 2
            dot = (r[0] + 12, my - 5, r[0] + 22, my + 5)
            if connected:
                d.ellipse(R(dot), fill=DOT_ON)
            else:
                d.ellipse(R(dot), outline=DOT_IDLE, width=round(2 * k))

    # Baterie: ikony (text až po zmenšení)
    if "battery_y" in L:
        for (lx, ix, tx), (_, pct, chg) in zip(_battery_positions(view["battery"], scale),
                                                view["battery"]):
            _draw_battery_icon(d, ix, L["battery_y"], pct, chg, k)

    # Indikátor stavu
    if state == "connected":
        d.ellipse(R(L["dot"]), fill=DOT_ON)
    else:
        ring = {"disconnected": DOT_OFF, "error": DOT_ERR}.get(state, DOT_UNKNOWN)
        d.ellipse(R(L["dot"]), outline=ring, width=round(3 * k))

    # Tlačítko
    if busy or state is None:
        btn_bg, btn_fg = BTN_BUSY, BTN_BUSY_TEXT
        btn_label = "Pracuji..." if busy else "Zjišťuji stav..."
    elif state == "connected":
        btn_bg, btn_fg, btn_label = BTN_ORANGE, BTN_TEXT, "Přepnout na iPhone"
    elif state == "disconnected":
        btn_bg, btn_fg, btn_label = BTN_BLUE, BTN_TEXT, "Připojit zpět k PC"
    else:
        btn_bg, btn_fg, btn_label = BTN_BLUE, BTN_TEXT, "Zkusit znovu"
    d.rounded_rectangle(R(L["button"]), radius=10 * k, fill=btn_bg)

    # Odznak klávesové zkratky (šířka podle textu); při nahrávání modrý
    hy = L["hotkey_y"]
    f_badge = _font(13 * scale, bold=True)
    capturing = view.get("capturing")
    badge_text = "Stiskni zkratku…" if capturing else view["hotkey"]
    badge_w = f_badge.getlength(badge_text) / scale + 20
    L["badge"] = (336 - badge_w, hy - 12.5, 336, hy + 12.5)
    d.rounded_rectangle(R(L["badge"]), radius=6 * k, fill=BTN_BLUE if capturing else BADGE_BG)

    # Oddělovač
    dv = L["divider_y"]
    d.rectangle(R((24, dv, 336, dv + 1)), fill=DIVIDER)

    # Přepínače: autostart, připojení po odemčení, pozastavení médií
    def switch(r, on):
        d.rounded_rectangle(R(r), radius=11 * k, fill=SWITCH_ON if on else SWITCH_OFF)
        kx = r[2] - 3 - 16 if on else r[0] + 3
        d.ellipse(R((kx, r[1] + 3, kx + 16, r[1] + 19)), fill="#FFFFFF")
    sw = L["switch"]
    switch(sw, view["autostart"])
    switch(L["auto"], view.get("auto_connect", False))
    switch(L["media"], view.get("pause_media", False))

    img = big.resize((W, H), Image.LANCZOS)

    # Text kreslíme až ve finálním rozlišení (ostřejší)
    t = ImageDraw.Draw(img)
    S = lambda x, y: (x * scale, y * scale)
    t.text(S(76, 44), title, font=f_title, fill=TEXT, anchor="lm")

    if "panel" in L:
        f_name, f_name_sel = _font(15 * scale), _font(15 * scale, True)
        f_st = _font(13 * scale)
        if not view["devices"]:
            p = L["panel"]
            t.text(S(p[0] + 16, (p[1] + p[3]) / 2), "Žádná spárovaná sluchátka",
                   font=f_name, fill=TEXT2, anchor="lm")
        for (r, name), (_, connected) in zip(L["rows"], view["devices"]):
            my = (r[1] + r[3]) / 2
            st = "připojeno k PC" if connected else "odpojeno"
            st_w = f_st.getlength(st) / scale
            fn = f_name_sel if name == view.get("selected") else f_name
            nm = _ellipsize(name, fn, (r[2] - 12 - st_w - 12 - (r[0] + 32)) * scale)
            t.text(S(r[0] + 32, my), nm, font=fn, fill=TEXT, anchor="lm")
            t.text(S(r[2] - 12, my), st, font=f_st, fill=TEXT2, anchor="rm")

    if state == "connected":
        title, sub = "Připojeno k PC", "Zvuk jde z počítače"
    elif state == "disconnected":
        title, sub = "Odpojeno od PC", "Přepnuto na iPhone, připojí se do ~5 s"
    elif state == "error":
        title, sub = "Chyba", view.get("detail") or ""
    else:
        title, sub = "Zjišťuji stav…", ""
    f_sub = _font(14 * scale)
    t.text(S(56, L["title_y"]), title, font=_font(20 * scale, True), fill=TEXT, anchor="lm")
    if sub:
        t.text(S(56, L["sub_y"]), _ellipsize(sub, f_sub, 280 * scale), font=f_sub,
               fill=TEXT2, anchor="lm")
    if "battery_y" in L:
        f_bat, f_bat_v = _font(13 * scale), _font(13 * scale, True)
        by = L["battery_y"]
        for (lx, ix, tx), (label, pct, _) in zip(_battery_positions(view["battery"], scale),
                                                  view["battery"]):
            t.text(S(lx, by), label, font=f_bat, fill=TEXT2, anchor="lm")
            t.text(S(tx, by), f"{pct} %", font=f_bat_v, fill=TEXT, anchor="lm")

    b = L["button"]
    t.text(S(180, (b[1] + b[3]) / 2), btn_label, font=_font(16 * scale, True),
           fill=btn_fg, anchor="mm")

    f_row = _font(14 * scale)
    # Levý popisek řádku zkratky: při nahrávání nápověda, po chybě hláška
    label, color = "Klávesová zkratka", TEXT2
    if capturing:
        label = "Esc = zrušit"
    if view.get("hotkey_msg"):
        label, color = view["hotkey_msg"], DOT_ERR
    label = _ellipsize(label, f_row, (L["badge"][0] - 24 - 10) * scale)
    t.text(S(24, hy), label, font=f_row, fill=color, anchor="lm")
    t.text(S(336 - 10, hy), badge_text, font=f_badge,
           fill=BTN_TEXT if capturing else TEXT, anchor="rm")
    t.text(S(24, (sw[1] + sw[3]) / 2), "Spouštět s Windows", font=f_row, fill=TEXT2, anchor="lm")
    a = L["auto"]
    t.text(S(24, (a[1] + a[3]) / 2), "Připojit po odemčení PC", font=f_row, fill=TEXT2, anchor="lm")
    m = L["media"]
    t.text(S(24, (m[1] + m[3]) / 2), "Pozastavit média při přepnutí", font=f_row,
           fill=TEXT2, anchor="lm")
    return img, L


def _inside(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


# Klávesy pro nahrávání zkratky (virtual-key kódy Windows)
_MODIFIER_VKS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
_VK_NAMES = {0x20: "space", 0x0D: "enter", 0x09: "tab", 0x13: "pause", 0x2D: "insert",
             0x2E: "delete", 0x24: "home", 0x23: "end", 0x21: "pageup", 0x22: "pagedown",
             0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x91: "scrolllock"}


def _vk_name(vk):
    """Virtual-key kód -> název klávesy pro config ("b", "5", "f9", "space"), nebo None."""
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    return _VK_NAMES.get(vk)


# --------------------------------------------------------------------------
# Okno
# --------------------------------------------------------------------------

class Popup:
    """Okno bez rámečku u hodin.

    model musí mít:
      view()             -> dict(title, state, detail, busy, autostart, hotkey,
                                 devices=[(jméno, připojeno)], selected)
      toggle()           -> spustí přepnutí (neblokuje)
      toggle_autostart() -> přepne autostart
      select_device(jm)  -> vybere sluchátka
      toggle_auto_connect()            -> přepne připojení po odemčení
      toggle_pause_media()             -> přepne pozastavení médií
      begin_hotkey_capture()           -> dočasně uvolní globální zkratku
      end_hotkey_capture(text|None)    -> uloží novou / vrátí starou; vrací chybu
    """

    REOPEN_GUARD = 0.6   # s – kliknutí na ikonu, které okno zavřelo, ho neotevře

    def __init__(self, root, model):
        self.root = root
        self.model = model
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG)
        self.label = tk.Label(self.win, bd=0, highlightthickness=0, padx=0, pady=0, bg=BG)
        self.label.pack()
        self.label.bind("<Button-1>", self._on_click)
        self.label.bind("<Motion>", self._on_motion)
        self.win.bind("<Escape>", self._on_escape)
        self.win.bind("<KeyPress>", self._on_key)
        self.visible = False
        self.scale = 1.0
        self.size = (0, 0)
        self.hidden_at = 0.0
        self.shown_at = 0.0
        self.activated = False
        self._photo = None
        self._watch_job = None
        self._last_view = None
        self._layout = None
        self._pt = (0, 0)
        self.picker_open = False      # rozbalený seznam sluchátek
        self.capturing = False        # nahrává se nová klávesová zkratka
        self.hotkey_msg = ""          # chyba po nahrání zkratky (v řádku zkratky)

    # --- veřejné API ----------------------------------------------------

    def toggle_at(self, pt):
        """Klik na tray ikonu: otevřít, nebo zavřít."""
        if self.visible:
            self.hide("icon")
        elif time.monotonic() - self.hidden_at < self.REOPEN_GUARD:
            # Tentýž klik už okno zavřel (klik mimo okno) – neotevírat znovu
            pass
        else:
            self.show(pt)

    def show(self, pt, watch=True):
        self._pt = pt
        self.picker_open = False
        hmon, _, _ = _monitor(pt)
        self.scale = _monitor_dpi(hmon) / 96
        self._render(force=True)
        self._place()
        hwnd = self._hwnd()
        # Bez ikony na hlavním panelu
        ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        _user32.SetForegroundWindow(hwnd)
        self.win.focus_force()
        self.visible = True
        self.activated = False
        self.shown_at = time.monotonic()
        if watch:
            self._schedule_watch()

    def hide(self, reason=""):
        if not self.visible:
            return
        self.win.withdraw()
        self.visible = False
        self.picker_open = False
        self.hotkey_msg = ""
        if self.capturing:            # zavřením se nahrávání ruší
            self.capturing = False
            self.model.end_hotkey_capture(None)
        if reason in ("outside", "focus"):
            self.hidden_at = time.monotonic()
        if self._watch_job:
            self.root.after_cancel(self._watch_job)
            self._watch_job = None

    def refresh(self):
        """Překreslí okno, pokud je otevřené a stav se změnil."""
        if self.visible:
            self._render()

    # --- vnitřní ---------------------------------------------------------

    def _hwnd(self):
        return _user32.GetAncestor(self.win.winfo_id(), GA_ROOT)

    def _place(self):
        """Umístí okno u hodin a nastaví zaoblený region (i po změně výšky)."""
        w, h = self.size
        x, y = compute_position(w, h, self._pt, self.scale)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.update_idletasks()
        # Zaoblené rohy (region vlastní Windows, nemazat)
        r = round(RADIUS * self.scale) * 2
        _user32.SetWindowRgn(self._hwnd(), _gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r), True)

    def _render(self, force=False):
        view = dict(self.model.view(), picker_open=self.picker_open,
                    capturing=self.capturing, hotkey_msg=self.hotkey_msg)
        key = (repr(sorted(view.items())), self.scale)
        if not force and key == self._last_view:
            return
        self._last_view = key
        self._view = view
        img, self._layout = render_card(view, self.scale)
        old_size = self.size
        self.size = img.size
        self._photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self._photo)
        if self.visible and self.size != old_size:
            self._place()   # rozbalení/sbalení seznamu změní výšku

    def _css(self, e):
        return e.x / self.scale, e.y / self.scale

    def _clickable(self, x, y):
        """Co je pod kurzorem: ("header"|"row"|"button"|"switch", jméno) nebo None."""
        v, L = self._view, self._layout
        if _inside(L["header"], x, y):
            return ("header", None)
        for r, name in L["rows"]:
            if _inside(r, x, y):
                return ("row", name)
        if _inside(L["button"], x, y) and not v["busy"] and v["state"] is not None:
            return ("button", None)
        if _inside(L["badge"], x, y):
            return ("badge", None)
        if _inside(L["switch_row"], x, y):
            return ("switch", None)
        if _inside(L["auto_row"], x, y):
            return ("auto", None)
        if _inside(L["media_row"], x, y):
            return ("media", None)
        return None

    def _on_click(self, e):
        hit = self._clickable(*self._css(e))
        if not hit:
            return
        what, name = hit
        if what == "header":
            self.picker_open = not self.picker_open
        elif what == "row":
            self.model.select_device(name)
            self.picker_open = False
        elif what == "button":
            self.model.toggle()
        elif what == "switch":
            self.model.toggle_autostart()
        elif what == "auto":
            self.model.toggle_auto_connect()
        elif what == "media":
            self.model.toggle_pause_media()
        elif what == "badge":
            if self.capturing:
                self._end_capture(None)
            else:
                self.capturing = True
                self.hotkey_msg = ""
                self.model.begin_hotkey_capture()
                # Klávesy potřebují fokus – po kliknutí si ho okno vezme
                _user32.SetForegroundWindow(self._hwnd())
                self.win.focus_force()
        self.refresh()

    # --- nahrávání klávesové zkratky ---------------------------------------

    def _on_escape(self, e):
        if self.capturing:
            self._end_capture(None)
        else:
            self.hide("esc")
        return "break"

    def _on_key(self, e):
        if not self.capturing:
            return
        vk = e.keycode                       # na Windows = virtual-key kód
        if vk in _MODIFIER_VKS:
            return                           # čekáme na hlavní klávesu
        mods = [name for code, name in ((0x11, "ctrl"), (0x12, "alt"), (0x10, "shift"))
                if _user32.GetAsyncKeyState(code) & 0x8000]
        if _user32.GetAsyncKeyState(0x5B) & 0x8000 or _user32.GetAsyncKeyState(0x5C) & 0x8000:
            mods.append("win")
        key = _vk_name(vk)
        if key is None:
            self.hotkey_msg = "Tuhle klávesu nejde použít"
        elif not mods:
            self.hotkey_msg = "Přidej Ctrl, Alt, Shift nebo Win"
        else:
            self._end_capture("+".join(mods + [key]))
            return "break"
        self.refresh()
        return "break"

    def _end_capture(self, text):
        self.capturing = False
        err = self.model.end_hotkey_capture(text)
        self.hotkey_msg = err or ""
        self.refresh()

    def _on_motion(self, e):
        self.label.configure(cursor="hand2" if self._clickable(*self._css(e)) else "")

    def _schedule_watch(self):
        self._watch_job = self.root.after(50, self._watch)

    def _watch(self):
        """Zavře okno po kliknutí mimo něj nebo po ztrátě popředí.

        Hlídáme obojí: popředí (Alt+Tab, klik do jiného okna) i stisk myši
        mimo okno – to funguje, i kdyby Windows oknu popředí nedalo.
        """
        self._watch_job = None
        if not self.visible:
            return
        hwnd = self._hwnd()
        fg = _user32.GetForegroundWindow()
        if fg == hwnd:
            self.activated = True
        pressed = any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))
        if pressed and time.monotonic() - self.shown_at > 0.15:
            rc = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rc))
            x, y = cursor_pos()
            if not (rc.left <= x < rc.right and rc.top <= y < rc.bottom):
                log.info("Klik mimo okno -> zavírám")
                self.hide("outside")
                return
        if self.activated and fg and fg != hwnd:
            cls = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(fg, cls, 64)
            log.info("Okno ztratilo popředí (nové popředí: %s, třída %s)", fg, cls.value)
            self.hide("focus")
            return
        self._schedule_watch()
