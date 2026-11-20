"""
AirPods Switch – přepíná Bluetooth sluchátka mezi Windows PC a iPhonem.

Odpojení od PC uvolní sluchátka a iPhone si je sám převezme; připojení
je vrátí k PC. Samotná Bluetooth logika je v modulu bt_airpods.

Běží jako ikona v oznamovací oblasti (tray):
  - levý klik na ikonu = malé okno se stavem a tlačítkem (modul popup),
  - globální zkratka (výchozí Ctrl+Alt+B) nebo menu pravého tlačítka = přepnout,
  - barva ikony: modrá = připojeno k PC, šedá = odpojeno,
    oranžová = přepínám, červená = chyba.

Spuštění ze zdrojáků:
    python airpods_switch.py
"""

import ctypes
import json
import logging
import os
import sys
import threading
import time
from ctypes import wintypes

import bt_airpods as bt

APP_NAME = "AirPodsSwitch"

DEFAULT_CONFIG = {
    "device_name": "AirPods",   # jméno (nebo jeho jednoznačná část) BT zařízení
    "hotkey": "ctrl+alt+b",     # globální klávesová zkratka
    "auto_connect": False,      # připojit sluchátka po odemčení / probuzení PC
    "fast_disconnect": True,    # pokus: při odpojení nejdřív shodit spojení, služby až pak
}


# --------------------------------------------------------------------------
# Cesty, logování, konfigurace
# --------------------------------------------------------------------------

def app_dir():
    """Složka vedle .exe (PyInstaller) nebo vedle skriptu."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")
LOG_PATH = os.path.join(app_dir(), "airpods_switch.log")

log = logging.getLogger(APP_NAME)


def setup_logging():
    """Log do souboru vedle aplikace (u --noconsole jediný výstup) + do konzole."""
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    if sys.stdout is not None:  # u --noconsole je stdout None
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    # Ladicí výpisy z BT modulu taky do logu
    bt.log = lambda msg: log.info("bt: %s", msg.strip())


def load_config():
    """Načte config.json; když chybí, vytvoří ho s výchozími hodnotami.

    Vrací (config, chyba_nebo_None). Při chybě v souboru se použijí
    výchozí hodnoty a chyba se vrátí, aby ji šlo ukázat uživateli.
    """
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return cfg, f"Nelze vytvořit config.json: {e}"
        return cfg, None
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("očekáván JSON objekt { ... }")
    except (OSError, ValueError) as e:
        return cfg, f"Chyba v config.json ({e}), používám výchozí nastavení."
    for key, default in DEFAULT_CONFIG.items():
        value = data.get(key)
        if isinstance(default, bool):
            if isinstance(value, bool):
                cfg[key] = value
        elif isinstance(value, str) and value.strip():
            cfg[key] = value.strip()
    return cfg, None


def save_config_value(key, value):
    """Uloží jednu hodnotu do config.json; ostatní obsah souboru zachová."""
    data = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, ValueError):
        pass
    data[key] = value
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Spouštění s Windows (HKCU\...\Run, bez admin práv)
# --------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Sem Windows (Správce úloh -> Aplikace po spuštění) ukládá zakázání
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def autostart_command():
    """Příkaz pro spuštění aplikace (exe, nebo pythonw + skript)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
    except OSError:
        return False
    # Zakázáno ve Správci úloh? (první bajt 3 = zakázáno, 2 = povoleno)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            data = winreg.QueryValueEx(k, APP_NAME)[0]
            if isinstance(data, bytes) and data[:1] == b"\x03":
                return False
    except OSError:
        pass
    return True


def set_autostart(enable):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
    # Zrušit případné zakázání ze Správce úloh, ať zapnutí opravdu platí
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except OSError:
        pass


def refresh_autostart_path():
    """Když je autostart zapnutý a aplikace se přesunula, opraví cestu.

    Jen u zkompilovaného exe – spuštění ze zdrojáků (vývoj, testy) nesmí
    přepsat autostart nainstalované aplikace.
    """
    if not getattr(sys, "frozen", False):
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            current = winreg.QueryValueEx(k, APP_NAME)[0]
            if current != autostart_command():
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
                log.info("Cesta pro autostart aktualizována")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Globální hotkey (RegisterHotKey, bez admin práv)
# --------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
              "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "pause": 0x13, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "scrolllock": 0x91,
}


def parse_hotkey(text):
    """'ctrl+alt+b' -> (modifikátory, virtual-key). Vyhodí ValueError."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    mods, vk = 0, None
    for p in parts:
        if p in _MODIFIERS:
            mods |= _MODIFIERS[p]
        elif vk is not None:
            raise ValueError(f'zkratka "{text}" má víc než jednu hlavní klávesu')
        elif len(p) == 1 and p.isascii() and p.isalnum():
            vk = ord(p.upper())
        elif p[0] == "f" and p[1:].isdigit() and 1 <= int(p[1:]) <= 24:
            vk = 0x70 + int(p[1:]) - 1
        elif p in _NAMED_KEYS:
            vk = _NAMED_KEYS[p]
        else:
            raise ValueError(f'neznámá klávesa "{p}" ve zkratce "{text}"')
    if vk is None:
        raise ValueError(f'zkratka "{text}" nemá hlavní klávesu')
    if not mods:
        raise ValueError(f'zkratka "{text}" musí obsahovat Ctrl, Alt, Shift nebo Win')
    return mods | MOD_NOREPEAT, vk


class HotkeyListener(threading.Thread):
    """Vlákno s vlastní smyčkou zpráv, které čeká na WM_HOTKEY.

    RegisterHotKey(NULL, ...) posílá WM_HOTKEY do fronty vlákna, které
    zkratku zaregistrovalo, proto registrace i smyčka běží tady.
    """

    HOTKEY_ID = 1

    def __init__(self, hotkey_text, callback):
        super().__init__(name="hotkey", daemon=True)
        self.hotkey_text = hotkey_text
        self.callback = callback
        self.error = None
        self._ready = threading.Event()
        self._thread_id = None

    def start_and_wait(self):
        """Spustí vlákno a počká na výsledek registrace. Vrací chybu nebo None."""
        self.start()
        self._ready.wait(5)
        return self.error

    def run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Vytvoří frontu zpráv vlákna dřív, než na ni může přijít WM_QUIT
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        try:
            mods, vk = parse_hotkey(self.hotkey_text)
        except ValueError as e:
            self.error = f"Neplatná zkratka v config.json: {e}"
            self._ready.set()
            return
        if not _user32.RegisterHotKey(None, self.HOTKEY_ID, mods, vk):
            err = ctypes.get_last_error()
            if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                self.error = (f'Zkratku "{self.hotkey_text}" už používá jiná aplikace. '
                              "Změň ji v okně aplikace (klik na zkratku) nebo v config.json.")
            else:
                self.error = f'Zkratku "{self.hotkey_text}" nelze zaregistrovat (chyba {err}).'
            self._ready.set()
            return
        self._ready.set()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Chyba v obsluze zkratky")
        finally:
            _user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def stop(self):
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# --------------------------------------------------------------------------
# Odemčení / probuzení PC (pro automatické připojení)
# --------------------------------------------------------------------------

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.RegisterSuspendResumeNotification.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_user32.RegisterSuspendResumeNotification.restype = ctypes.c_void_p
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetTickCount64.restype = ctypes.c_ulonglong

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOGON = 0x5
WTS_SESSION_UNLOCK = 0x8
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x12


def system_uptime():
    """Sekundy od startu Windows."""
    return _kernel32.GetTickCount64() / 1000


class SessionWatcher(threading.Thread):
    """Skryté okno, které dostává zprávy o odemčení relace a probuzení PC.

    WTSRegisterSessionNotification a RegisterSuspendResumeNotification
    potřebují okno (ne jen vlákno), proto si vytvoříme neviditelné.
    callback("unlock" | "resume") se volá z tohoto vlákna.
    """

    CLASS_NAME = "AirPodsSwitchSessionWatcher"

    def __init__(self, callback):
        super().__init__(name="session", daemon=True)
        self.callback = callback
        self._thread_id = None
        self._wndproc = WNDPROC(self._proc)   # držet referenci, jinak GC

    def run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
        hinst = _kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        _user32.RegisterClassW(ctypes.byref(wc))
        hwnd = _user32.CreateWindowExW(0, self.CLASS_NAME, self.CLASS_NAME, 0,
                                       0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            log.warning("Sledování odemčení nejde spustit (chyba %s)", ctypes.get_last_error())
            return
        wts = ctypes.WinDLL("wtsapi32")
        wts.WTSRegisterSessionNotification(hwnd, 0)         # NOTIFY_FOR_THIS_SESSION
        _user32.RegisterSuspendResumeNotification(hwnd, 0)  # DEVICE_NOTIFY_WINDOW_HANDLE
        msg = wintypes.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            wts.WTSUnRegisterSessionNotification(hwnd)
            _user32.DestroyWindow(hwnd)

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_WTSSESSION_CHANGE and wparam in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON):
                self.callback("unlock")
            elif msg == WM_POWERBROADCAST and wparam == PBT_APMRESUMEAUTOMATIC:
                self.callback("resume")
        except Exception:
            log.exception("Chyba v obsluze odemčení/probuzení")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def stop(self):
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# --------------------------------------------------------------------------
# Přepínání v worker vlákně
# --------------------------------------------------------------------------

class Switcher:
    """Spouští BT operace na pozadí; vždy jen jedna najednou.

    on_result(ok, text, what=..., already=..., quiet=...) se volá z worker
    vlákna; what je název akce ("toggle", "connect", "auto-connect", ...),
    already=True znamená, že nebylo co dělat, quiet=True = jen obnovit stav
    (výsledek už byl oznámen dřív).

    U rychlého odpojení ("fast") se výsledek oznámí hned po shození spojení
    a audio služby se dovypínají na pozadí (~10 s). V té fázi se Switcher
    netváří jako zaneprázdněný a další požadavek se zařadí do fronty
    (provede se, jakmile vypínání doběhne).
    """

    def __init__(self, device_name, on_result, method="full"):
        self.device_name = device_name
        self.on_result = on_result
        self.method = method        # "full" nebo "fast" (viz bt_airpods.disconnect)
        self._lock = threading.Lock()
        self._background = False    # dovypínání služeb po už oznámeném odpojení
        self._pending = None        # (what, job) čekající na dokončení pozadí

    @property
    def busy(self):
        """Probíhá akce, na jejíž výsledek se čeká (samotné dovypínání na pozadí
        se nepočítá, zařazená akce ve frontě ano)."""
        return self._lock.locked() and (not self._background or self._pending is not None)

    def toggle_async(self):
        """Spustí přepnutí. Vrací False, pokud předchozí akce ještě běží."""
        return self.run_async("toggle")

    def run_async(self, what, job=None):
        """Spustí akci "toggle" | "connect" | "disconnect" | "auto-connect" na pozadí.

        job: vlastní funkce vracející (ok, text) – pro jiné BT operace
        (např. obnovení služeb), aby taky běžely jen jedna najednou.
        Vrací False (a nic nedělá), pokud předchozí akce ještě běží;
        během dovypínání na pozadí akci zařadí do fronty a vrátí True.
        """
        if not self._lock.acquire(blocking=False):
            if self._background:
                log.info("Akce %s zařazena – počká na dovypnutí služeb", what)
                self._pending = (what, job)
                return True
            log.info("Akce %s ignorována – předchozí akce ještě běží", what)
            return False
        threading.Thread(target=self._run, args=(what, job), name="bt-worker",
                         daemon=True).start()
        return True

    def _released(self, what):
        """Spojení spadlo (rychlé odpojení) – oznámit výsledek hned."""
        self._background = True
        try:
            self.on_result(True, "Odpojeno, přepnuto na iPhone", what=what, already=False)
        except Exception:
            log.exception("Chyba v on_result")

    def _do(self, what):
        """Provede akci; vrací (výsledek, potvrzeno, už_bylo_hotovo)."""
        name = self.device_name
        released = lambda: self._released(what)
        if what in ("connect", "auto-connect"):
            already = bt.is_connected(name)
            return "connected", already or bt.connect(name, self.method), already
        if what == "disconnect":
            # Hotovo je až ve chvíli, kdy neběží ani BT spojení – AirPods
            # jinak zůstávají přes AAP u PC a iPhone si je nevezme.
            already = not bt.find_device(name).fConnected
            return ("disconnected",
                    already or bt.disconnect(name, self.method, on_released=released), already)
        # toggle
        if bt.is_connected(name):
            return "disconnected", bt.disconnect(name, self.method, on_released=released), False
        return "connected", bt.connect(name, self.method), False

    def _run(self, what, job=None):
        ok, text, already = False, "Neznámá chyba", False
        try:
            if job is not None:
                ok, text = job()
                log.info("%s -> ok=%s: %s", what, ok, text)
                return
            t0 = time.monotonic()
            action, confirmed, already = self._do(what)
            dt = time.monotonic() - t0
            log.info("%s -> %s, potvrzeno=%s, už_hotovo=%s, %.1f s",
                     what, action, confirmed, already, dt)
            if already:
                ok = True
                text = ("Sluchátka už jsou připojená k PC" if action == "connected"
                        else "Sluchátka už jsou odpojená od PC")
            elif action == "disconnected":
                ok = confirmed
                text = ("Odpojeno, přepnuto na iPhone" if confirmed else
                        "Odpojení se nepotvrdilo – sluchátka jsou pořád připojená k PC")
            else:
                ok = confirmed
                text = ("Připojeno k PC" if confirmed else
                        "Připojení se nepovedlo – jsou sluchátka mimo pouzdro a v dosahu?")
        except bt.BtError as e:
            text = str(e)
            log.warning("%s selhal: %s", what, e)
        except Exception as e:  # nečekaná chyba nesmí shodit aplikaci
            text = f"Neočekávaná chyba: {e}"
            log.exception("%s selhal", what)
        finally:
            # Úspěch už byl oznámen dřív (rychlé odpojení) -> jen obnovit stav
            quiet = self._background and ok
            self._background = False
            pending, self._pending = self._pending, None
            self._lock.release()
            try:
                self.on_result(ok, text, what=what, already=already, quiet=quiet)
            except Exception:
                log.exception("Chyba v on_result")
            if pending:
                log.info("Spouštím zařazenou akci %s", pending[0])
                self.run_async(*pending)


# --------------------------------------------------------------------------
# Tray ikona
# --------------------------------------------------------------------------

POLL_INTERVAL = 3.0  # s, jak často se zjišťuje stav na pozadí

# Barvy ikony podle stavu
STATE_COLORS = {
    "connected":    "#1D4ED8",   # modrá jako ikona aplikace – sluchátka jsou u PC
    "disconnected": "#64748B",   # šedá – odpojeno (u iPhonu / v pouzdře)
    "busy":         "#F97316",   # oranžová – probíhá přepínání
    "error":        "#DC2626",   # červená – zařízení nenalezeno apod.
}
STATE_TEXT = {
    "connected": "připojeno k PC",
    "disconnected": "odpojeno od PC",
    "busy": "přepínám…",
    "error": "chyba",
}


def make_icon_image(state, size=64):
    """Tray ikona: stejný tvar jako ikona aplikace, barva podle stavu.

    Windows ji v liště zmenšuje na 16–24 px, proto silnější tah sluchátek.
    """
    from popup import render_app_icon
    return render_app_icon(size, bg=STATE_COLORS[state], stroke=2.6)


def save_app_icon(path):
    """Uloží .ico pro exe (používá se jen při buildu, viz README)."""
    from popup import render_app_icon
    sizes = [16, 20, 24, 32, 40, 48, 64, 256]
    frames = [render_app_icon(s) for s in sizes]
    # Každá velikost vykreslená zvlášť (ostřejší než zmenšování jedné)
    frames[-1].save(path, sizes=[(s, s) for s in sizes], append_images=frames[:-1])


def hotkey_label(text, sep=" + "):
    """'ctrl+alt+b' -> 'Ctrl + Alt + B' (pro okno a menu)."""
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    parts = []
    for p in (x.strip() for x in text.split("+") if x.strip()):
        low = p.lower()
        parts.append(names.get(low) or (p.upper() if len(p) <= 3 else p.capitalize()))
    return sep.join(parts)


class TrayApp:
    """Tray ikona + okno + hotkey.

    Vlákna:
      - hlavní vlákno: tkinter mainloop (okno); jiná vlákna do něj nesahají
        přímo, ale přes frontu ui_call() -> _pump_ui(),
      - pystray: vlastní vlákno (run_detached) se smyčkou zpráv tray ikony,
      - hotkey: vlastní vlákno se smyčkou zpráv (HotkeyListener),
      - poll: zjišťování stavu každé ~3 s,
      - bt-worker: přepínání (Switcher), jen jedno najednou,
      - session: skryté okno pro zprávy o odemčení/probuzení PC,
      - WinRT: BLE reklamy se stavem baterie (BatteryMonitor).
    """

    def __init__(self, root):
        import pystray
        import queue
        from popup import Popup
        from battery import BatteryMonitor

        self.root = root
        self.cfg, self.cfg_error = load_config()
        self.device = self.cfg["device_name"]
        self.selected_name = None   # skutečné BT jméno vybraných sluchátek
        self.devices = []           # [(jméno, připojeno_k_PC)] všech spárovaných sluchátek
        self.images = {st: make_icon_image(st) for st in STATE_COLORS}
        self.state = None           # stav ikony vč. "busy"
        self.stable_state = None    # poslední zjištěný stav: connected/disconnected/error
        self.detail = ""            # text chyby u stavu "error"
        self.last_error = None      # aby se stejná chyba neoznamovala každé 3 s
        self._state_lock = threading.Lock()
        self._menu_lock = threading.Lock()
        self.stop_event = threading.Event()
        self._ui_queue = queue.Queue()
        self.switcher = Switcher(self.device, self.on_result,
                                 "fast" if self.cfg["fast_disconnect"] else "full")
        self.hotkey = HotkeyListener(self.cfg["hotkey"], self.on_hotkey)
        self.session = SessionWatcher(self.on_session_event)
        self.battery = BatteryMonitor()
        self.model_id = None        # ID modelu vybraných sluchátek (pro baterii)
        self.popup = Popup(root, self)

        menu = pystray.Menu(
            # Neviditelná výchozí položka: levý klik na ikonu otevře okno
            pystray.MenuItem("Otevřít", self.on_icon_click, default=True, visible=False),
            # "\t" zarovná zkratku doprava (standardní Win32 menu)
            pystray.MenuItem(
                lambda item: f"Přepnout sluchátka\t{hotkey_label(self.cfg['hotkey'], '+')}",
                self.on_toggle),
            pystray.MenuItem(lambda item: f"Stav: {self._status_text()}",
                             lambda icon, item: None, enabled=False),
            # Podmenu se seznamem sluchátek – generuje se při každé aktualizaci menu
            pystray.MenuItem("Sluchátka", pystray.Menu(self._device_menu_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Připojit po odemčení PC", self.on_auto_connect,
                             checked=lambda item: self.cfg["auto_connect"]),
            pystray.MenuItem("Spouštět s Windows", self.on_autostart,
                             checked=lambda item: autostart_enabled()),
            pystray.MenuItem("Obnovit audio služby", self.on_restore_services),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Konec", self.on_quit),
        )
        self.icon = pystray.Icon(APP_NAME, self.images["disconnected"], self.title, menu)

    # --- most do GUI vlákna -----------------------------------------------

    def ui_call(self, fn, *args):
        """Naplánuje volání v hlavním (tk) vlákně. Bezpečné z libovolného vlákna."""
        self._ui_queue.put((fn, args))

    def _pump_ui(self):
        import queue
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log.exception("Chyba v GUI vlákně")
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(30, self._pump_ui)

    # --- model pro okno (volá se z tk vlákna) ------------------------------

    @property
    def title(self):
        """Název do okna, tooltipu a notifikací = jméno vybraných sluchátek."""
        return self.selected_name or self.device

    def view(self):
        return {
            "title": self.title,
            "devices": list(self.devices),
            "selected": self.selected_name,
            "state": self.stable_state,
            "detail": self.detail if self.stable_state == "error" else "",
            "busy": self.switcher.busy,
            "autostart": autostart_enabled(),
            "auto_connect": self.cfg["auto_connect"],
            "hotkey": hotkey_label(self.cfg["hotkey"]),
            "battery": self.battery_items(),
        }

    def battery_items(self):
        """((popisek, %, nabíjí), ...) pro okno; prázdné, když nic nevíme."""
        from battery import battery_items
        if not self.battery.available or self.model_id is None:
            return ()
        return battery_items(self.battery.get(self.model_id))

    def toggle(self):
        log.info("Přepnout z okna")
        self.start_toggle()

    def toggle_autostart(self):
        self.on_autostart()

    def toggle_auto_connect(self):
        self.on_auto_connect()

    # --- změna klávesové zkratky z okna (volá se z tk vlákna) ---------------

    def begin_hotkey_capture(self):
        """Dočasně uvolní zkratku, aby ji šlo stisknout při nahrávání nové."""
        self.hotkey.stop()
        self.hotkey.join(1)
        log.info("Nahrávání nové zkratky")

    def end_hotkey_capture(self, new_text):
        """Ukončí nahrávání. new_text=None = zrušeno (vrátí původní zkratku).

        Vrací None při úspěchu, jinak krátký text chyby pro okno.
        """
        old_text = self.cfg["hotkey"]
        if new_text and new_text != old_text:
            listener = HotkeyListener(new_text, self.on_hotkey)
            err = listener.start_and_wait()
            if not err:
                self.hotkey = listener
                self.cfg["hotkey"] = new_text
                try:
                    save_config_value("hotkey", new_text)
                except OSError as e:
                    log.warning("Zkratku nejde uložit: %s", e)
                log.info("Nová zkratka: %s", new_text)
                self.update_menu()
                return None
            log.info("Zkratku %s nejde použít: %s", new_text, err)
            short = ("Obsazená jinou aplikací" if "jiná aplikace" in err
                     else "Tuhle zkratku nejde použít")
        else:
            short = None
        # Zrušeno nebo chyba -> vrátit původní zkratku
        self.hotkey = HotkeyListener(old_text, self.on_hotkey)
        err = self.hotkey.start_and_wait()
        if err:
            self.notify(err, f"{self.title} – chyba")
        return short

    def select_device(self, name):
        """Vybere sluchátka, se kterými aplikace pracuje (z okna i z menu)."""
        if name == self.selected_name:
            return
        if self.switcher.busy:
            log.info("Výběr sluchátek ignorován – probíhá přepínání")
            return
        log.info("Vybrána sluchátka: %s", name)
        self.device = name
        self.switcher.device_name = name
        self.cfg["device_name"] = name
        try:
            save_config_value("device_name", name)
        except OSError as e:
            self.notify(f"Výběr nejde uložit do config.json: {e}", f"{name} – chyba")
        self.selected_name = name
        self.stable_state = None        # do zjištění stavu "Zjišťuji stav…"
        self.state = None
        self.last_error = None
        self.update_menu()
        self.ui_call(self.popup.refresh)
        threading.Thread(target=self.refresh_state, name="poll-now", daemon=True).start()

    def _device_menu_items(self):
        import pystray
        if not self.devices:
            yield pystray.MenuItem("Žádná spárovaná sluchátka", lambda icon, item: None,
                                   enabled=False)
            return
        for name, connected in self.devices:
            # "&" je ve Win32 menu zkratka – zdvojit; "\t" zarovná stav doprava
            label = name.replace("&", "&&") + ("\tpřipojeno k PC" if connected else "")
            yield pystray.MenuItem(label, self._select_action(name),
                                   checked=self._is_selected(name), radio=True)

    # pystray přijímá jen akce se 0–2 parametry, proto továrny místo lambda s n=name
    def _select_action(self, name):
        return lambda icon, item: self.select_device(name)

    def _is_selected(self, name):
        return lambda item: name == self.selected_name

    def _refresh_model_id(self, dev):
        """ID modelu vybraných sluchátek (registr) – jen při změně výběru."""
        if getattr(self, "_model_id_for", None) != dev.szName:
            self._model_id_for = dev.szName
            self.model_id = bt.device_model_id(dev)
            log.info("Model sluchátek %s: %s", dev.szName,
                     f"0x{self.model_id:04X}" if self.model_id else "neznámý")

    def update_menu(self):
        """Přestaví menu tray ikony; zámek – volá se z více vláken."""
        with self._menu_lock:
            try:
                self.icon.update_menu()
            except Exception:
                log.exception("Nelze aktualizovat menu")

    # --- pomocné ---------------------------------------------------------

    def _status_text(self):
        if self.switcher.busy:
            return STATE_TEXT["busy"]
        return STATE_TEXT.get(self.stable_state, "zjišťuji…")

    def notify(self, text, title=None):
        title = title or self.title
        log.info("Notifikace: %s – %s", title, text)
        try:
            self.icon.notify(text, title)
        except Exception:
            log.exception("Notifikaci nelze zobrazit")

    def set_state(self, state, detail=""):
        self.ui_call(self.popup.refresh)   # okno se překreslí jen při změně
        with self._state_lock:
            if state != "busy":
                self.stable_state = state
            if state == self.state and detail == self.detail:
                return
            self.state, self.detail = state, detail
            tip = f"{self.title}: {STATE_TEXT[state]}"
            if detail:
                tip += f" – {detail}"
            try:
                self.icon.icon = self.images[state]
                self.icon.title = tip[:127]  # Windows tooltip má limit 128 znaků
                self.update_menu()           # položka "Stav: ..."
            except Exception:
                log.exception("Nelze aktualizovat ikonu")

    def refresh_state(self):
        """Zjistí stav sluchátek a promítne ho do ikony i okna."""
        try:
            devices = bt.audio_devices()
        except Exception:
            devices = []
        if devices != self.devices:
            self.devices = devices
            self.update_menu()                 # podmenu Sluchátka
            self.ui_call(self.popup.refresh)
        try:
            dev = bt.find_device(self.device)
            if dev.szName != self.selected_name:
                self.selected_name = dev.szName
                self.update_menu()
            self._refresh_model_id(dev)
            connected = bool(dev.fConnected) and bt.audio_enabled(dev)
        except bt.BtError as e:
            if self.switcher.busy:
                return
            self.set_state("error", str(e))
            if str(e) != self.last_error:
                self.last_error = str(e)
                self.notify(str(e), f"{self.title} – chyba")
            return
        except Exception:
            log.exception("Chyba při zjišťování stavu")
            return
        self.last_error = None
        if not self.switcher.busy:  # během přepínání nechat oranžovou
            self.set_state("connected" if connected else "disconnected")

    def poll_loop(self):
        while not self.stop_event.is_set():
            if not self.switcher.busy:
                self.refresh_state()
            self.stop_event.wait(POLL_INTERVAL)

    # --- akce ------------------------------------------------------------

    def start_toggle(self, what="toggle"):
        if self.switcher.run_async(what):
            self.set_state("busy", self.detail if self.stable_state == "error" else "")

    def on_icon_click(self, icon=None, item=None):
        # Volá pystray vlákno – pozici kurzoru vezmeme hned (monitor kliknutí)
        from popup import cursor_pos
        self.ui_call(self.popup.toggle_at, cursor_pos())

    def on_toggle(self, icon=None, item=None):
        log.info("Přepnout z menu")
        self.start_toggle()

    def on_hotkey(self):
        log.info("Stisknuta zkratka")
        self.start_toggle()

    def on_result(self, ok, text, what="toggle", already=False, quiet=False):
        # Volá se z worker vlákna po dokončení akce (u rychlého odpojení
        # i dřív – hned po shození spojení; konec je pak quiet)
        if quiet:
            pass
        elif what == "auto-connect":
            # Automatické připojení: hlásit jen úspěch, neúspěch jen do logu
            # (sluchátka v pouzdře by jinak hlásila chybu po každém odemčení)
            if ok and not already:
                self.notify("Připojeno k PC (po odemčení)", self.title)
        elif what == "restore":
            self.notify(text, "AirPods Switch" if ok else "AirPods Switch – chyba")
        else:
            self.notify(text, self.title if ok else f"{self.title} – chyba")
        self.refresh_state()
        self.ui_call(self.popup.refresh)

    def on_session_event(self, kind):
        """Odemčení / přihlášení / probuzení PC (volá vlákno session)."""
        log.info("Událost relace: %s", kind)
        if not self.cfg["auto_connect"]:
            return
        # Chvíli počkat: po probuzení se BT stack teprve rozjíždí
        delay = 4.0 if kind == "unlock" else 8.0
        threading.Timer(delay, self._auto_connect, args=(kind,)).start()

    def _auto_connect(self, kind):
        if self.stop_event.is_set() or not self.cfg["auto_connect"]:
            return
        log.info("Automatické připojení (%s)", kind)
        if self.switcher.run_async("auto-connect"):
            self.set_state("busy")

    def on_auto_connect(self, icon=None, item=None):
        value = not self.cfg["auto_connect"]
        self.cfg["auto_connect"] = value
        try:
            save_config_value("auto_connect", value)
        except OSError as e:
            self.notify(f"Nastavení nejde uložit do config.json: {e}", f"{self.title} – chyba")
        log.info("Připojení po odemčení: %s", "zapnuto" if value else "vypnuto")
        self.update_menu()
        self.ui_call(self.popup.refresh)

    def on_restore_services(self, icon=None, item=None):
        def job():
            restored = bt.restore_audio_services()
            if restored:
                return True, ("Audio služby obnoveny: " + ", ".join(restored)
                              + ". Windows je teď může připojit k PC.")
            return True, "Všechna sluchátka už mají audio služby zapnuté."
        log.info("Obnovit audio služby z menu")
        if self.switcher.run_async("restore", job):
            self.set_state("busy")

    def on_autostart(self, icon=None, item=None):
        enable = not autostart_enabled()
        try:
            set_autostart(enable)
            log.info("Autostart %s", "zapnut" if enable else "vypnut")
        except OSError as e:
            self.notify(f"Nastavení autostartu selhalo: {e}", f"{self.title} – chyba")
        self.update_menu()                 # zaškrtnutí v menu
        self.ui_call(self.popup.refresh)   # přepínač v okně

    def on_quit(self, icon=None, item=None):
        log.info("Ukončuji")
        self.stop_event.set()
        self.hotkey.stop()
        self.session.stop()
        self.battery.stop()
        self.icon.stop()
        self.ui_call(self.root.quit)

    # --- běh ------------------------------------------------------------

    def _setup(self, icon):
        # Běží ve vlastním vlákně pystray po vytvoření ikony
        icon.visible = True
        log.info("Zařízení: %s, zkratka: %s, odpojení: %s", self.device, self.cfg["hotkey"],
                 self.switcher.method)
        refresh_autostart_path()
        problems = []
        if self.cfg_error:
            problems.append(self.cfg_error)
        hk_error = self.hotkey.start_and_wait()
        if hk_error:
            problems.append(hk_error)
        threading.Thread(target=self.poll_loop, name="poll", daemon=True).start()
        self.session.start()
        self.battery.start()
        if problems:
            self.notify("\n".join(problems), f"{self.title} – chyba")
        # Spuštění hned po startu Windows (autostart) = přihlášení -> připojit
        if self.cfg["auto_connect"] and system_uptime() < 300:
            self.on_session_event("logon")

    def run(self):
        self.icon.run_detached(setup=self._setup)   # tray ve vlastním vlákně
        self.root.after(30, self._pump_ui)
        try:
            self.root.mainloop()                     # GUI v hlavním vlákně
        finally:
            self.stop_event.set()
            self.hotkey.stop()
            self.session.stop()
            self.battery.stop()
            try:
                self.icon.stop()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Spuštění
# --------------------------------------------------------------------------

_single_instance_handle = None


def ensure_single_instance():
    """Vrací False, pokud aplikace už běží (pojmenovaný mutex)."""
    global _single_instance_handle
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _single_instance_handle = _kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def run_preview(out_path, state):
    """Testovací režim: ukáže okno s daným stavem, uloží snímek obrazovky a skončí.

    python airpods_switch.py --preview okno.png [connected|disconnected|busy|error|picker]
    Neregistruje hotkey ani tray ikonu a nesahá na Bluetooth.
    """
    import tkinter as tk
    from PIL import ImageGrab
    import popup

    picker = state == "picker"           # rozbalený seznam sluchátek
    if picker:
        state = "connected"

    class FakeModel:
        def view(self):
            return {"title": "AirPods 4",
                    "devices": [("AirPods 4", state == "connected"), ("AirPods Pro", False)],
                    "selected": "AirPods 4",
                    "state": "connected" if state == "busy" else state,
                    "detail": 'Zařízení "AirPods" není spárované.' if state == "error" else "",
                    "busy": state == "busy", "autostart": state != "disconnected",
                    "hotkey": hotkey_label(DEFAULT_CONFIG["hotkey"]),
                    "auto_connect": state == "connected",
                    "battery": () if state == "error" else
                               (("L", 60, False), ("P", 20, False), ("Pouzdro", 10, True))}
        def toggle(self): pass
        def toggle_autostart(self): pass
        def select_device(self, name): pass
        def toggle_auto_connect(self): pass
        def begin_hotkey_capture(self): pass
        def end_hotkey_capture(self, text): return None

    root = tk.Tk()
    root.withdraw()
    p = popup.Popup(root, FakeModel())
    # Klik "u hodin": pravý dolní roh primárního monitoru
    w = _user32.GetSystemMetrics(0)
    h = _user32.GetSystemMetrics(1)
    p.show((w - 50, h - 10), watch=False)   # bez zavírání při ztrátě popředí
    if picker:
        p.picker_open = True
        p.refresh()

    def grab():
        hwnd = p._hwnd()
        rc = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rc))
        pad = 20
        img = ImageGrab.grab(bbox=(rc.left - pad, rc.top - pad, rc.right + pad, rc.bottom + pad),
                             all_screens=True)
        img.save(out_path)
        print(f"okno {rc.right - rc.left}x{rc.bottom - rc.top} na ({rc.left},{rc.top}), "
              f"scale {p.scale:.2f} -> {out_path}")
        root.destroy()

    root.after(800, grab)
    root.mainloop()
    return 0


def main():
    # Pomocný režim pro build: python airpods_switch.py --make-icon soubor.ico
    if len(sys.argv) == 3 and sys.argv[1] == "--make-icon":
        save_app_icon(sys.argv[2])
        return 0
    import popup
    popup.set_dpi_awareness()      # před vytvořením jakéhokoli okna
    if len(sys.argv) >= 3 and sys.argv[1] == "--preview":
        return run_preview(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "connected")
    setup_logging()
    if not ensure_single_instance():
        log.info("Aplikace už běží, končím")
        _user32.MessageBoxW(None, "AirPods Switch už běží – najdeš ho v oznamovací oblasti.",
                            "AirPods Switch", 0x40)
        return 0
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()                # skryté hlavní okno; popup je Toplevel
    TrayApp(root).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
