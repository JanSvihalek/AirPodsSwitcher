"""
Stav baterie AirPods z Bluetooth LE reklam.

AirPods (vyndané z pouzdra nebo s otevřeným pouzdrem) neustále vysílají
reklamu Applu typu "proximity pairing" (výrobce 0x004C, typ 0x07) se stavem
baterie levého a pravého sluchátka a pouzdra. Formát Apple nedokumentuje,
dekódování vychází z komunitních projektů (OpenPods, AirPodsDesktop).

Omezení: reklama neobsahuje Bluetooth adresu sluchátek (Apple ji náhodně
mění), jen ID modelu. Když je poblíž víc AirPods stejného modelu, bereme
ty s nejsilnějším signálem.

Potřebuje balíčky winrt-* (viz README); bez nich se funkce tiše vypne.
"""

import logging
import threading
import time

log = logging.getLogger("AirPodsSwitch")

APPLE_COMPANY_ID = 0x004C
PROXIMITY_PAIRING = 0x07
MAX_AGE = 30.0          # s – starší údaje nezobrazujeme


def parse(data):
    """Dekóduje reklamu typu 0x07. Vrací dict, nebo None.

    Hodnoty baterie 0–10 = 0–100 %, 15 = neznámo (None).
    """
    if len(data) < 8 or data[0] != PROXIMITY_PAIRING:
        return None
    model = data[3] | (data[4] << 8)
    flip = (data[5] & 0x20) == 0            # které sluchátko vysílá
    hi, lo = data[6] >> 4, data[6] & 0x0F
    left, right = (hi, lo) if flip else (lo, hi)
    case = data[7] & 0x0F
    chg = data[7] >> 4
    pct = lambda v: v * 10 if v <= 10 else None
    return {
        "model": model,
        "left": pct(left),
        "right": pct(right),
        "case": pct(case),
        "left_charging": bool(chg & (0b10 if flip else 0b01)),
        "right_charging": bool(chg & (0b01 if flip else 0b10)),
        "case_charging": bool(chg & 0b100),
    }


class BatteryMonitor:
    """Na pozadí poslouchá BLE reklamy a pamatuje si poslední stav baterie."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen = {}          # BLE adresa -> (čas, RSSI, dekódovaná data)
        self._watcher = None
        self.available = False   # False = chybí winrt nebo BT LE nejde spustit

    def start(self):
        try:
            from winrt.windows.devices.bluetooth.advertisement import (
                BluetoothLEAdvertisementWatcher, BluetoothLEScanningMode)
        except ImportError:
            log.info("Baterie: balíčky winrt nejsou k dispozici, funkce vypnutá")
            return
        try:
            w = BluetoothLEAdvertisementWatcher()
            w.scanning_mode = BluetoothLEScanningMode.ACTIVE
            w.add_received(self._on_received)
            w.start()
            self._watcher = w
            self.available = True
            log.info("Baterie: poslouchám BLE reklamy AirPods")
        except Exception:
            log.exception("Baterie: BLE watcher nejde spustit")

    def stop(self):
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                pass
            self._watcher = None

    def _on_received(self, sender, args):
        # Volá WinRT ze svého vlákna – jen uložit, nic dalšího
        try:
            for md in args.advertisement.manufacturer_data:
                if md.company_id != APPLE_COMPANY_ID:
                    continue
                info = parse(bytes(md.data))
                if info:
                    with self._lock:
                        self._seen[args.bluetooth_address] = (
                            time.monotonic(), args.raw_signal_strength_in_dbm, info)
        except Exception:
            log.exception("Baterie: chyba při čtení reklamy")

    def get(self, model):
        """Nejčerstvější stav baterie pro daný model (nejsilnější signál), nebo None."""
        now = time.monotonic()
        best = None
        with self._lock:
            for addr, (t, rssi, info) in list(self._seen.items()):
                if now - t > MAX_AGE:
                    del self._seen[addr]
                    continue
                if model is not None and info["model"] != model:
                    continue
                if best is None or rssi > best[0]:
                    best = (rssi, info)
        return best[1] if best else None


def battery_items(info):
    """Pro okno: ((popisek, procenta, nabíjí), ...) jen pro známé hodnoty."""
    if not info:
        return ()
    out = []
    for key, label in (("left", "L"), ("right", "P"), ("case", "Pouzdro")):
        v = info.get(key)
        if v is not None:
            out.append((label, v, bool(info.get(f"{key}_charging"))))
    return tuple(out)


def format_battery(info):
    """'L 60 % · P 80 % · pouzdro 50 %' (+ 'nabíjí'), nebo '' když nic není známo."""
    if not info:
        return ""
    parts = []
    for key, label in (("left", "L"), ("right", "P"), ("case", "pouzdro")):
        v = info.get(key)
        if v is None:
            continue
        text = f"{label} {v} %"
        if info.get(f"{key}_charging"):
            text += " nabíjí"
        parts.append(text)
    return " · ".join(parts)
