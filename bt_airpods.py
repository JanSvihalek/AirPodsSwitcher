"""
Bluetooth jádro pro přepínání AirPods (nebo jiných BT sluchátek) na Windows.

Dvě metody připojení/odpojení:

  "ks"      (výchozí) – pošle audio ovladači BT sluchátek KS property
            KSPROPSETID_BtAudio / ONESHOT_RECONNECT nebo ONESHOT_DISCONNECT.
            Totéž dělá tlačítko "Připojit"/"Odpojit" v Nastavení Windows.
            Nepotřebuje admin práva, ovladač zůstává nainstalovaný.

  "service" (záloha) – BluetoothSetServiceState vypne/zapne služby A2DP a HFP.
            Windows při tom odinstaluje/přeinstaluje ovladač profilu. Hrubší
            a pomalejší, ale funguje i tam, kde KS filtr nejde otevřít.

  "bth"     – odpojení přes IOCTL_BTH_DISCONNECT_DEVICE na BT rádiu (shodí
            celé BT spojení). Připojení jde přes KS jako u "ks".

  "full"    – (výchozí v CLI) odpojení = vypnout audio služby + shodit spojení,
            připojení = zapnout audio služby (záloha KS / restart služeb).

  "fast"    – jako "full", ale při odpojení nejdřív shodí spojení a služby
            vypne až potom (rychlejší předání iPhonu); viz disconnect().

Stav připojení se čte z BluetoothFindFirstDevice (pole fConnected).

Použití z příkazové řádky:
    python bt_airpods.py list
    python bt_airpods.py status [jméno]
    python bt_airpods.py off|on|toggle [jméno] [--method ks|service]
    python bt_airpods.py diag [jméno]
    python bt_airpods.py watch [jméno] [sekundy]
"""

import ctypes
import sys
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# Základní typy a DLL
# --------------------------------------------------------------------------

_bth = ctypes.WinDLL("bthprops.cpl", use_last_error=True)
_cfg = ctypes.WinDLL("cfgmgr32.dll", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s):
        import uuid
        u = uuid.UUID(s)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = list(u.bytes[8:])
        return g


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, wintypes.WORD) for n in (
        "wYear", "wMonth", "wDayOfWeek", "wDay",
        "wHour", "wMinute", "wSecond", "wMilliseconds")]


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("fReturnAuthenticated", wintypes.BOOL),
        ("fReturnRemembered", wintypes.BOOL),
        ("fReturnUnknown", wintypes.BOOL),
        ("fReturnConnected", wintypes.BOOL),
        ("fIssueInquiry", wintypes.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", wintypes.HANDLE),
    ]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Address", ctypes.c_ulonglong),      # BLUETOOTH_ADDRESS (union, 8 B)
        ("ulClassofDevice", wintypes.ULONG),
        ("fConnected", wintypes.BOOL),
        ("fRemembered", wintypes.BOOL),
        ("fAuthenticated", wintypes.BOOL),
        ("stLastSeen", SYSTEMTIME),
        ("stLastUsed", SYSTEMTIME),
        ("szName", wintypes.WCHAR * 248),
    ]


_bth.BluetoothFindFirstDevice.argtypes = [
    ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS),
    ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
_bth.BluetoothFindFirstDevice.restype = wintypes.HANDLE
_bth.BluetoothFindNextDevice.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
_bth.BluetoothFindNextDevice.restype = wintypes.BOOL
_bth.BluetoothFindDeviceClose.argtypes = [wintypes.HANDLE]
_bth.BluetoothFindDeviceClose.restype = wintypes.BOOL
_bth.BluetoothSetServiceState.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO),
    ctypes.POINTER(GUID), wintypes.DWORD]
_bth.BluetoothSetServiceState.restype = wintypes.DWORD

ERROR_NO_MORE_ITEMS = 259


class BtError(Exception):
    """Chyba se srozumitelnou zprávou pro uživatele."""


def log(msg):
    """Ladicí výpis; CLI ho přesměruje na print, tray aplikace do logu."""
    pass


# --------------------------------------------------------------------------
# Hledání zařízení
# --------------------------------------------------------------------------

def _norm(s):
    """Normalizace jména: malá písmena, typografické apostrofy -> rovné."""
    return (s.replace("’", "'").replace("‘", "'")
             .replace("–", "-").casefold().strip())


def mac_str(addr):
    """BLUETOOTH_ADDRESS (ulonglong) -> 'AABBCCDDEEFF'."""
    return f"{addr:012X}"


def list_devices():
    """Vrátí seznam spárovaných (zapamatovaných) BT zařízení."""
    params = BLUETOOTH_DEVICE_SEARCH_PARAMS()
    params.dwSize = ctypes.sizeof(params)
    params.fReturnAuthenticated = True
    params.fReturnRemembered = True
    params.fReturnConnected = True
    params.fReturnUnknown = False
    params.fIssueInquiry = False      # žádné aktivní skenování, jen cache
    params.cTimeoutMultiplier = 0
    params.hRadio = None              # všechna rádia

    info = BLUETOOTH_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(info)

    devices = []
    h = _bth.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
    if not h:
        err = ctypes.get_last_error()
        if err == ERROR_NO_MORE_ITEMS:
            return devices
        raise BtError(f"Bluetooth není dostupný (chyba {err}). Je zapnutý?")
    try:
        while True:
            copy = BLUETOOTH_DEVICE_INFO()
            ctypes.pointer(copy)[0] = info
            devices.append(copy)
            info = BLUETOOTH_DEVICE_INFO()
            info.dwSize = ctypes.sizeof(info)
            if not _bth.BluetoothFindNextDevice(h, ctypes.byref(info)):
                break
    finally:
        _bth.BluetoothFindDeviceClose(h)
    return devices


def find_device(name):
    """Najde právě jedno zařízení podle jména.

    Nejdřív zkusí přesnou shodu, pak podřetězec. Při nejednoznačnosti
    nebo nenalezení vyhodí BtError se seznamem kandidátů.
    """
    devices = list_devices()
    want = _norm(name)
    exact = [d for d in devices if _norm(d.szName) == want]
    if len(exact) == 1:
        return exact[0]
    partial = [d for d in devices if want in _norm(d.szName)]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(f'"{d.szName}"' for d in devices) or "(žádná)"
    if not partial:
        raise BtError(f'Zařízení "{name}" není spárované. '
                      f"Spárovaná zařízení: {names}")
    cands = ", ".join(f'"{d.szName}"' for d in partial)
    raise BtError(f'Jméno "{name}" odpovídá více zařízením: {cands}. '
                  "Uveď v config.json přesné jméno.")


_bth.BluetoothEnumerateInstalledServices.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO),
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_bth.BluetoothEnumerateInstalledServices.restype = wintypes.DWORD

A2DP_SINK = "0000110B-0000-1000-8000-00805F9B34FB"


def installed_services(device):
    """GUIDy BT služeb, které má Windows pro zařízení zapnuté (velkými písmeny)."""
    count = wintypes.DWORD(32)
    buf = (GUID * 32)()
    rc = _bth.BluetoothEnumerateInstalledServices(None, ctypes.byref(device),
                                                  ctypes.byref(count), buf)
    if rc not in (0, 234):  # 234 = ERROR_MORE_DATA
        return set()
    out = set()
    for g in buf[:count.value]:
        d4 = bytes(g.Data4)
        out.add(f"{g.Data1:08X}-{g.Data2:04X}-{g.Data3:04X}-"
                f"{d4[:2].hex().upper()}-{d4[2:].hex().upper()}")
    return out


def audio_enabled(device):
    """True, pokud má Windows zapnutou audio službu A2DP pro zařízení."""
    return A2DP_SINK in installed_services(device)


COD_MAJOR_AUDIO = 0x04   # Class of Device: hlavní třída Audio/Video


def is_audio_device(device):
    """Sluchátka/reproduktor? Podle Class of Device – ta zůstává, i když jsou
    audio služby vypnuté (na rozdíl od seznamu zapnutých služeb)."""
    return ((device.ulClassofDevice >> 8) & 0x1F) == COD_MAJOR_AUDIO


def audio_devices():
    """Spárovaná audio zařízení: [(jméno, připojeno_k_PC)] seřazená podle jména."""
    out = []
    for d in list_devices():
        if is_audio_device(d):
            out.append((d.szName, bool(d.fConnected) and audio_enabled(d)))
    return sorted(out, key=lambda x: _norm(x[0]))


def device_model_id(device):
    """ID produktu (PID) z Bluetooth devnodu, např. 0x201B. None, když nejde zjistit.

    AirPods vysílají stejné číslo v BLE reklamách se stavem baterie, takže
    podle něj poznáme, kterému typu sluchátek reklama patří.
    """
    import re
    import winreg
    mac = mac_str(device.Address)
    # Rozhraní SDP služby má každé spárované BT zařízení; v názvu je VID/PID i MAC
    key = r"SYSTEM\CurrentControlSet\Control\DeviceClasses\{00001000-0000-1000-8000-00805f9b34fb}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    return None
                i += 1
                if mac in sub.upper():
                    m = re.search(r"PID&([0-9A-F]{4})", sub, re.I)
                    return int(m.group(1), 16) if m else None
    except OSError:
        return None


def restore_audio_services():
    """Zapne audio služby u všech spárovaných sluchátek, která je mají vypnuté.

    Pojistka pro případ, že se aplikace přestane používat: po jejím odpojení
    zůstávají služby vypnuté a připojení přes Nastavení by nedalo zvuk.
    Vrací seznam jmen, kterým se služby zapnuly.
    """
    restored = []
    for d in list_devices():
        if is_audio_device(d) and not audio_enabled(d):
            _service_apply(d, BLUETOOTH_SERVICE_ENABLE)
            restored.append(d.szName)
    return restored


def is_connected(name):
    """True, pokud je zařízení připojené k PC a PC ho může používat pro zvuk.

    Samotné BT spojení nestačí: AirPods drží spojení i přes jiné služby
    (AAP, AVRCP), i když má Windows audio vypnuté.
    """
    dev = find_device(name)
    return bool(dev.fConnected) and audio_enabled(dev)


# --------------------------------------------------------------------------
# Metoda "ks": KS property na audio filtru BT ovladače
# --------------------------------------------------------------------------

KSCATEGORY_AUDIO = GUID.from_str("6994AD04-93EF-11D0-A3CC-00A0C9223196")
KSPROPSETID_BtAudio = GUID.from_str("7FA06C40-B8F6-4C7E-8556-E8C33A12E54D")
KSPROPERTY_ONESHOT_RECONNECT = 0
KSPROPERTY_ONESHOT_DISCONNECT = 1
KSPROPERTY_TYPE_GET = 0x00000001
IOCTL_KS_PROPERTY = 0x002F0003   # CTL_CODE(FILE_DEVICE_KS, 0, METHOD_NEITHER, FILE_ANY_ACCESS)


class KSPROPERTY(ctypes.Structure):
    _fields_ = [("Set", GUID), ("Id", wintypes.ULONG), ("Flags", wintypes.ULONG)]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.ULONG)]


DEVPKEY_Device_InstanceId = DEVPROPKEY(
    GUID.from_str("78C34FC8-104A-4ACA-9EA4-524D52996E57"), 256)

CR_SUCCESS = 0
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0

_cfg.CM_Get_Device_Interface_List_SizeW.argtypes = [
    ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(GUID),
    wintypes.LPCWSTR, wintypes.ULONG]
_cfg.CM_Get_Device_Interface_ListW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.LPWSTR,
    wintypes.ULONG, wintypes.ULONG]
_cfg.CM_Get_Device_Interface_PropertyW.argtypes = [
    wintypes.LPCWSTR, ctypes.POINTER(DEVPROPKEY), ctypes.POINTER(wintypes.ULONG),
    ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
_cfg.CM_Locate_DevNodeW.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
_cfg.CM_Get_Parent.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG]
_cfg.CM_Get_Device_IDW.argtypes = [
    wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]

_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3


def _audio_interfaces():
    """Všechna přítomná rozhraní KSCATEGORY_AUDIO (cesty k filtrům)."""
    size = wintypes.ULONG()
    cr = _cfg.CM_Get_Device_Interface_List_SizeW(
        ctypes.byref(size), ctypes.byref(KSCATEGORY_AUDIO), None,
        CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
    if cr != CR_SUCCESS or size.value <= 1:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    cr = _cfg.CM_Get_Device_Interface_ListW(
        ctypes.byref(KSCATEGORY_AUDIO), None, buf, size.value,
        CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
    if cr != CR_SUCCESS:
        return []
    # MULTI_SZ: řetězce oddělené \0, konec dvojitou \0
    raw = ctypes.wstring_at(ctypes.addressof(buf), size.value)
    return [p for p in raw.split("\0") if p]


def _interface_instance_id(path):
    """ID instance zařízení (devnode), kterému patří rozhraní."""
    ptype = wintypes.ULONG()
    size = wintypes.ULONG(0)
    _cfg.CM_Get_Device_Interface_PropertyW(
        path, ctypes.byref(DEVPKEY_Device_InstanceId), ctypes.byref(ptype),
        None, ctypes.byref(size), 0)
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    cr = _cfg.CM_Get_Device_Interface_PropertyW(
        path, ctypes.byref(DEVPKEY_Device_InstanceId), ctypes.byref(ptype),
        buf, ctypes.byref(size), 0)
    if cr != CR_SUCCESS:
        return None
    return ctypes.wstring_at(ctypes.addressof(buf))


def _ancestor_ids(instance_id, max_depth=6):
    """ID instance zařízení a jeho rodičů (HFP filtr má MAC až u rodiče)."""
    ids = [instance_id]
    devinst = wintypes.DWORD()
    if _cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id, 0) != CR_SUCCESS:
        return ids
    buf = ctypes.create_unicode_buffer(512)
    for _ in range(max_depth):
        parent = wintypes.DWORD()
        if _cfg.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != CR_SUCCESS:
            break
        if _cfg.CM_Get_Device_IDW(parent, buf, 512, 0) != CR_SUCCESS:
            break
        ids.append(buf.value)
        devinst = parent
    return ids


# Vlastnosti audio endpointů v registru (HKLM, čitelné bez admina)
_MMDEV_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
_P_FILTER_PATH = "{233164c8-1b2c-4c7d-bc68-b671687a2567},1"  # cesta ke KS filtru
_P_DEVNODE = "{b3f8fa53-0004-438e-9003-51a46e139bfc},2"      # devnode ovladače
_P_BT_DEVNODE = "{b3f8fa53-0004-438e-9003-51a46e139bfc},39"  # BT devnode (u offloadu)
_P_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"         # "Headphones"/"Headset"


# --- Minimální COM přes ctypes: topologie audio endpointu -------------------

_ole = ctypes.WinDLL("ole32.dll")
_ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole.CoInitializeEx.restype = ctypes.HRESULT
_ole.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_ole.CoCreateInstance.restype = ctypes.HRESULT
_ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]

CLSID_MMDeviceEnumerator = GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IDeviceTopology = GUID.from_str("2A07407E-6497-4A18-9787-32F79BD0D98F")
CLSCTX_ALL = 0x17


def _vcall(obj, index, *args, argtypes=()):
    """Zavolá metodu COM rozhraní podle indexu ve vtable. Vrací HRESULT."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(obj, *args)


def _release(obj):
    if obj:
        _vcall(obj, 2)


def _strip_prefix(path):
    """MMDevice ID mají tvar "{2}.\\\\?\\..." – předponu "{n}." odřízneme."""
    if path.startswith("{") and "}." in path[:6]:
        return path.split("}.", 1)[1]
    return path


def _topology_filter(endpoint_id):
    """Cesta ke KS filtru, na který je napojený audio endpoint.

    Endpoint -> IDeviceTopology -> konektor 0 -> GetDeviceIdConnectedTo.
    U ovladačů s rozdělením wave/topology je to topology filtr, který
    obsluhuje KSPROPSETID_BtAudio (stejnou cestou jde i Nastavení Windows).
    Vrací None, pokud se to nepodaří.
    """
    # Ve vlákně může být COM už inicializovaný jinak – návratovou
    # hodnotu ignorujeme, důležité je jen, aby byl inicializovaný.
    _ole.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    enum = ctypes.c_void_p()
    dev = ctypes.c_void_p()
    topo = ctypes.c_void_p()
    conn = ctypes.c_void_p()
    try:
        if _ole.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None,
                                 CLSCTX_ALL, ctypes.byref(IID_IMMDeviceEnumerator),
                                 ctypes.byref(enum)) < 0:
            return None
        # IMMDeviceEnumerator::GetDevice (index 5)
        if _vcall(enum, 5, endpoint_id, ctypes.byref(dev),
                  argtypes=(wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))) < 0:
            return None
        # IMMDevice::Activate (index 3)
        if _vcall(dev, 3, ctypes.byref(IID_IDeviceTopology), CLSCTX_ALL, None,
                  ctypes.byref(topo),
                  argtypes=(ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p))) < 0:
            return None
        # IDeviceTopology::GetConnector (index 4)
        if _vcall(topo, 4, 0, ctypes.byref(conn),
                  argtypes=(wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))) < 0:
            return None
        # IConnector::GetDeviceIdConnectedTo (index 10)
        pstr = ctypes.c_void_p()
        if _vcall(conn, 10, ctypes.byref(pstr),
                  argtypes=(ctypes.POINTER(ctypes.c_void_p),)) < 0 or not pstr:
            return None
        try:
            return _strip_prefix(ctypes.wstring_at(pstr))
        finally:
            _ole.CoTaskMemFree(pstr)
    finally:
        for o in (conn, topo, dev, enum):
            _release(o)


def _endpoint_filters(mac):
    """KS filtry z audio endpointů, které patří zařízení s danou MAC.

    Důvod: na PC s Bluetooth audio offloadem (např. Intel Smart Sound)
    teče zvuk sluchátek přes filtry zvukového ovladače, ne přes filtr BT
    ovladače. Najdeme je jen přes endpoint, který si v registru pamatuje,
    ke kterému BT zařízení patří. Pro každý endpoint vracíme jeho wave
    filtr (z registru) i topology filtr (z COM topologie).
    """
    import winreg

    def val(key, name):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    result = []
    for flow in ("Render", "Capture"):
        try:
            fk = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MMDEV_KEY + "\\" + flow)
        except OSError:
            continue
        with fk:
            i = 0
            while True:
                try:
                    eid = winreg.EnumKey(fk, i)
                except OSError:
                    break
                i += 1
                try:
                    pk = winreg.OpenKey(fk, eid + r"\Properties")
                except OSError:
                    continue
                with pk:
                    owner = f"{val(pk, _P_BT_DEVNODE) or ''} {val(pk, _P_DEVNODE) or ''}"
                    path = val(pk, _P_FILTER_PATH)
                    if mac not in owner.upper() or not isinstance(path, str):
                        continue
                    path = _strip_prefix(path)
                    label = f"endpoint {flow} {val(pk, _P_NAME) or eid}"
                    # ID endpointu pro MMDevice API: render 0.0.0, capture 0.0.1
                    prefix = "{0.0.0.00000000}." if flow == "Render" else "{0.0.1.00000000}."
                    topo = _topology_filter(prefix + eid)
                    if topo:
                        result.append((topo, label + " (topology)"))
                    result.append((path, label + " (wave)"))
    return result


KSPROPERTY_TYPE_SETSUPPORT = 0x00000100


def _supports_btaudio(path):
    """Ptá se filtru (jen čtení), jestli zná KSPROPSETID_BtAudio."""
    h = _k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                         OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE or not h:
        return False
    try:
        prop = KSPROPERTY(KSPROPSETID_BtAudio, 0, KSPROPERTY_TYPE_SETSUPPORT)
        returned = wintypes.DWORD()
        return bool(_k32.DeviceIoControl(h, IOCTL_KS_PROPERTY, ctypes.byref(prop),
                                         ctypes.sizeof(prop), None, 0,
                                         ctypes.byref(returned), None))
    finally:
        _k32.CloseHandle(h)


def _can_open(path):
    h = _k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                         OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE or not h:
        return False
    _k32.CloseHandle(h)
    return True


def find_audio_filters(device, only_usable=True):
    """Cesty k audio filtrům, které patří danému BT zařízení.

    Kombinuje dva zdroje:
      1) rozhraní KSCATEGORY_AUDIO pod BT devnodem (klasický BT audio
         ovladač, u offloadu aktivní jen v odpojeném stavu),
      2) filtry z audio endpointů (u offloadu filtry zvukového ovladače).
    Vrací seznam (cesta, popis) bez duplicit; standardně jen filtry,
    které existují a podporují KSPROPSETID_BtAudio.
    """
    mac = mac_str(device.Address)
    candidates = []
    seen_inst = set()
    for path in _audio_interfaces():
        inst = _interface_instance_id(path)
        if not inst or inst in seen_inst:
            continue
        if any(mac in i.upper() for i in _ancestor_ids(inst)):
            seen_inst.add(inst)
            candidates.append((path, inst))
    candidates += _endpoint_filters(mac)

    result, seen = [], set()
    for path, label in candidates:
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        if only_usable and not _supports_btaudio(path):
            continue
        result.append((path, label))
    return result


def _ks_oneshot(path, prop_id):
    """Otevře filtr a pošle KSPROPERTY_ONESHOT_*. Vrací (ok, win32_chyba)."""
    h = _k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                         OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE or not h:
        return False, ctypes.get_last_error()
    try:
        prop = KSPROPERTY(KSPROPSETID_BtAudio, prop_id, KSPROPERTY_TYPE_GET)
        returned = wintypes.DWORD()
        ok = _k32.DeviceIoControl(h, IOCTL_KS_PROPERTY, ctypes.byref(prop),
                                  ctypes.sizeof(prop), None, 0,
                                  ctypes.byref(returned), None)
        return bool(ok), (0 if ok else ctypes.get_last_error())
    finally:
        _k32.CloseHandle(h)


def _ks_apply(device, prop_id):
    filters = find_audio_filters(device)
    if not filters:
        raise BtError(f'Pro "{device.szName}" se nenašel audio ovladač '
                      "(KS filtr). Zkus metodu \"service\".")
    errors = []
    any_ok = False
    for path, label in filters:
        ok, err = _ks_oneshot(path, prop_id)
        log(f"  KS {'OK' if ok else f'chyba {err}'}: {label}")
        any_ok |= ok
        if not ok:
            errors.append(f"{label}: chyba {err}")
    if not any_ok:
        raise BtError("Ovladač odmítl požadavek: " + "; ".join(errors))


# --------------------------------------------------------------------------
# Metoda "bth": IOCTL_BTH_DISCONNECT_DEVICE na BT rádiu (jen odpojení)
# --------------------------------------------------------------------------

class BLUETOOTH_FIND_RADIO_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD)]


_bth.BluetoothFindFirstRadio.argtypes = [
    ctypes.POINTER(BLUETOOTH_FIND_RADIO_PARAMS), ctypes.POINTER(wintypes.HANDLE)]
_bth.BluetoothFindFirstRadio.restype = wintypes.HANDLE
_bth.BluetoothFindNextRadio.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE)]
_bth.BluetoothFindNextRadio.restype = wintypes.BOOL
_bth.BluetoothFindRadioClose.argtypes = [wintypes.HANDLE]

# CTL_CODE(FILE_DEVICE_BLUETOOTH=0x41, BTH_IOCTL_BASE+0x03, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_BTH_DISCONNECT_DEVICE = 0x0041000C


def _radios():
    """Handly všech BT rádií (volající je musí zavřít)."""
    params = BLUETOOTH_FIND_RADIO_PARAMS(ctypes.sizeof(BLUETOOTH_FIND_RADIO_PARAMS))
    radio = wintypes.HANDLE()
    find = _bth.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(radio))
    if not find:
        return []
    radios = [radio.value]
    try:
        while True:
            radio = wintypes.HANDLE()
            if not _bth.BluetoothFindNextRadio(find, ctypes.byref(radio)):
                break
            radios.append(radio.value)
    finally:
        _bth.BluetoothFindRadioClose(find)
    return radios


def _bth_disconnect(device):
    """Shodí Bluetooth spojení (ACL) se zařízením přes ovladač rádia."""
    radios = _radios()
    if not radios:
        raise BtError("Nenašel jsem Bluetooth adaptér. Je Bluetooth zapnutý?")
    addr = ctypes.c_ulonglong(device.Address)
    errors, any_ok = [], False
    for h in radios:
        returned = wintypes.DWORD()
        ok = _k32.DeviceIoControl(h, IOCTL_BTH_DISCONNECT_DEVICE, ctypes.byref(addr),
                                  ctypes.sizeof(addr), None, 0,
                                  ctypes.byref(returned), None)
        err = 0 if ok else ctypes.get_last_error()
        log(f"  IOCTL_BTH_DISCONNECT_DEVICE: {'OK' if ok else f'chyba {err}'}")
        _k32.CloseHandle(h)
        any_ok |= bool(ok)
        if not ok:
            errors.append(str(err))
    if not any_ok:
        hint = " (5 = přístup odepřen)" if "5" in errors else ""
        raise BtError("Odpojení přes BT rádio selhalo: chyba "
                      + ", ".join(errors) + hint)


# --------------------------------------------------------------------------
# Metoda "service": BluetoothSetServiceState
# --------------------------------------------------------------------------

# Audio služby, které Windows u AirPods používá
AUDIO_SERVICES = {
    "A2DP (AudioSink)": GUID.from_str("0000110B-0000-1000-8000-00805F9B34FB"),
    "Handsfree":        GUID.from_str("0000111E-0000-1000-8000-00805F9B34FB"),
    "Headset":          GUID.from_str("00001108-0000-1000-8000-00805F9B34FB"),
}
BLUETOOTH_SERVICE_DISABLE = 0
BLUETOOTH_SERVICE_ENABLE = 1
ERROR_SERVICE_DOES_NOT_EXIST = 1060


def _service_apply(device, flag, services=None):
    """Zapne/vypne audio služby. Volání běží souběžně (každé ve svém vlákně),
    protože Windows při nich (od)instalovává ovladače a trvá to sekundy."""
    import threading
    services = services or AUDIO_SERVICES
    results = {}

    def worker(sname, guid):
        g = GUID.from_buffer_copy(guid)
        dev = BLUETOOTH_DEVICE_INFO.from_buffer_copy(device)
        t0 = time.monotonic()
        rc = _bth.BluetoothSetServiceState(None, ctypes.byref(dev),
                                           ctypes.byref(g), flag)
        results[sname] = (rc, time.monotonic() - t0)

    threads = [threading.Thread(target=worker, args=item, daemon=True)
               for item in services.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors, any_ok = [], False
    action = "zapnuto" if flag == BLUETOOTH_SERVICE_ENABLE else "vypnuto"
    for sname, (rc, dt) in results.items():
        if rc != ERROR_SERVICE_DOES_NOT_EXIST:
            log(f"  {sname}: {action if rc == 0 else f'chyba {rc}'} za {dt:.1f} s")
        if rc == 0:
            any_ok = True
        elif rc != ERROR_SERVICE_DOES_NOT_EXIST:
            errors.append(f"{sname}: chyba {rc}")
    if not any_ok:
        hint = " (5 = přístup odepřen, chce to admina)" if "chyba 5" in "".join(errors) else ""
        raise BtError("BluetoothSetServiceState selhal: "
                      + "; ".join(errors) + hint)


# --------------------------------------------------------------------------
# Veřejné API
# --------------------------------------------------------------------------

def _wait(name, predicate, timeout, step=0.5):
    """Čeká, až predicate(device) vrátí True. Vrací True/False."""
    end = time.monotonic() + timeout
    while True:
        if predicate(find_device(name)):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(step)


def disconnect(name, method="full", timeout=10.0, on_released=None):
    """Odpojí zařízení od PC. Vrací True, pokud se odpojení potvrdilo.

    "full" (výchozí): vypne audio služby (Windows pak sluchátka samo znovu
    nepřipojí) a shodí BT spojení (AirPods se uvolní pro iPhone).

    "fast": obráceně – nejdřív shodí spojení, služby vypne až potom.
    on_released() se zavolá hned, jak spojení spadne (sluchátka jsou pro
    iPhone volná); vypínání služeb pak běží dál (u odpojených sluchátek
    trvá Windows ~10 s).
    """
    dev = find_device(name)
    if method == "fast":
        # Rychlejší předání iPhonu: nejdřív shodit spojení (zvuk z PC hned
        # zmizí a iPhone si sluchátka může vzít), služby vypnout až potom.
        # Riziko: Windows se během vypínání služeb může ke sluchátkům znovu
        # připojit – proto na konci kontrola.
        t0 = time.monotonic()
        if dev.fConnected:
            log("  shazuji BT spojení (nejdřív)…")
            _bth_disconnect(dev)
            if on_released and _wait(name, lambda d: not d.fConnected, 2.0, step=0.1):
                log(f"  spojení spadlo za {time.monotonic() - t0:.1f} s, "
                    "služby dovypínám na pozadí")
                on_released()
        if audio_enabled(dev):
            log("  vypínám audio služby…")
            _service_apply(dev, BLUETOOTH_SERVICE_DISABLE)
        dev = find_device(name)
        if dev.fConnected:
            log(f"  sluchátka se mezitím vrátila k PC ({time.monotonic() - t0:.1f} s), "
                "shazuji znovu…")
            _bth_disconnect(dev)
        return _wait(name, lambda d: not d.fConnected, timeout)
    if method == "full":
        if audio_enabled(dev):
            log("  vypínám audio služby…")
            _service_apply(dev, BLUETOOTH_SERVICE_DISABLE)
        dev = find_device(name)
        if dev.fConnected:
            log("  shazuji BT spojení…")
            _bth_disconnect(dev)
        return _wait(name, lambda d: not d.fConnected, timeout)

    if not dev.fConnected:
        return True
    if method == "service":
        _service_apply(dev, BLUETOOTH_SERVICE_DISABLE)
        return _wait(name, lambda d: not audio_enabled(d), timeout)
    if method == "bth":
        _bth_disconnect(dev)
    else:
        _ks_apply(dev, KSPROPERTY_ONESHOT_DISCONNECT)
    return _wait(name, lambda d: not d.fConnected, timeout)


def connect(name, method="full", timeout=15.0):
    """Připojí zařízení k PC. Vrací True, pokud se připojení potvrdilo.

    "full" (výchozí): zapne audio služby, pokud jsou vypnuté (to samo
    obvykle sluchátka připojí); když se nepřipojí, pošle KS reconnect.
    """
    dev = find_device(name)
    if method in ("full", "fast", "service"):
        if not audio_enabled(dev):
            log("  zapínám audio služby…")
            _service_apply(dev, BLUETOOTH_SERVICE_ENABLE)
            if _wait(name, lambda d: d.fConnected and audio_enabled(d), 8.0):
                return True
        if method == "service":
            return _wait(name, lambda d: d.fConnected and audio_enabled(d), timeout)

    if is_connected(name):
        return True
    connected = lambda d: d.fConnected and audio_enabled(d)
    log("  posílám KS reconnect…")
    try:
        _ks_apply(find_device(name), KSPROPERTY_ONESHOT_RECONNECT)
        if _wait(name, connected, 8.0 if method in ("full", "fast") else timeout):
            return True
    except BtError as e:
        log(f"  KS: {e}")
    if method not in ("full", "fast"):
        return False
    # Záloha: vypnout a znovu zapnout audio služby – zapnutí služeb
    # sluchátka spolehlivě připojí (ověřeno na Intel BT offloadu).
    log("  KS nepomohl, restartuji audio služby…")
    try:
        _service_apply(find_device(name), BLUETOOTH_SERVICE_DISABLE)
    except BtError as e:
        log(f"  vypnutí služeb: {e}")
    _service_apply(find_device(name), BLUETOOTH_SERVICE_ENABLE)
    return _wait(name, connected, timeout)


def toggle(name, method="full"):
    """Přepne stav. Vrací ('disconnected'|'connected', potvrzeno: bool)."""
    if is_connected(name):
        return "disconnected", disconnect(name, method)
    return "connected", connect(name, method)


# --------------------------------------------------------------------------
# CLI pro testování
# --------------------------------------------------------------------------

def _cli(argv):
    global log
    log = print
    args = [a for a in argv if not a.startswith("--")]
    method = "full"
    for a in argv:
        if a.startswith("--method="):
            method = a.split("=", 1)[1]
    if "--method" in argv:
        i = argv.index("--method")
        method = argv[i + 1]
        args = [a for a in args if a != method]

    cmd = args[0] if args else "list"
    name = " ".join(args[1:]) or "AirPods"

    try:
        if cmd == "list":
            for d in list_devices():
                st = "PŘIPOJENO" if d.fConnected else "odpojeno"
                print(f"{mac_str(d.Address)}  {st:9}  {d.szName}")
        elif cmd == "status":
            d = find_device(name)
            audio = audio_enabled(d)
            st = "připojeno k PC" if (d.fConnected and audio) else "odpojeno od PC"
            print(f"{d.szName}: {st}  (BT spojení: {'ano' if d.fConnected else 'ne'}, "
                  f"audio služby: {'zapnuté' if audio else 'VYPNUTÉ'})")
        elif cmd == "watch":
            # Každou sekundu vypíše stav; poslední číslo = počet sekund
            secs = 60
            if args[-1].isdigit() and len(args) > 2:
                secs = int(args[-1])
                name = " ".join(args[1:-1])
            last = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < secs:
                st = is_connected(name)
                if st != last:
                    print(f"{time.monotonic() - t0:5.1f} s  "
                          f"{'PŘIPOJENO k PC' if st else 'odpojeno od PC'}")
                    last = st
                time.sleep(1)
        elif cmd == "diag":
            d = find_device(name)
            print(f"Zařízení: {d.szName}  MAC {mac_str(d.Address)}  "
                  f"connected={bool(d.fConnected)}")
            filters = find_audio_filters(d, only_usable=False)
            usable = 0
            for path, label in filters:
                if not _can_open(path):
                    st = "neexistuje"
                elif _supports_btaudio(path):
                    st = "POUŽITELNÝ"
                    usable += 1
                else:
                    st = "bez BtAudio"
                print(f"  [{st:11}] {label}\n      {path}")
            print(f"Použitelných audio filtrů: {usable}")
        elif cmd in ("off", "on", "toggle"):
            t0 = time.monotonic()
            if cmd == "off":
                res, ok = "disconnected", disconnect(name, method)
            elif cmd == "on":
                res, ok = "connected", connect(name, method)
            else:
                res, ok = toggle(name, method)
            dt = time.monotonic() - t0
            word = "Odpojeno" if res == "disconnected" else "Připojeno"
            if ok:
                print(f"{word} ({method}, {dt:.1f} s)")
            else:
                print(f"Příkaz odeslán ({method}), ale stav se do {dt:.0f} s "
                      "nezměnil.")
                return 2
        else:
            print(__doc__)
            return 1
    except BtError as e:
        print(f"Chyba: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_cli(sys.argv[1:]))
