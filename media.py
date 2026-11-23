"""
Pozastavení a obnovení přehrávání médií na PC.

Při přepnutí sluchátek na iPhone se pozastaví všechno, co na PC hraje
(Spotify, YouTube v prohlížeči, …), aby zvuk nepřeskočil do reproduktorů.
Po připojení zpět se znovu spustí jen to, co pozastavila aplikace
a co mezitím nikdo jiný nespustil ani nezavřel.

Používá Windows "System Media Transport Controls" (to, co ukazuje
panel médií u hlasitosti) – funguje tedy pro aplikace, které se v tom
panelu ukazují. Potřebuje balíček winrt-Windows.Media.Control; bez něj
se funkce tiše vypne.

Ladění:  python media.py list            # mediální relace a jejich stav
         python media.py cycle 5         # pozastavit, 5 s počkat, spustit
"""

import logging
import threading

log = logging.getLogger("AirPodsSwitch")

TIMEOUT = 2.0   # s – max. čekání na jednu WinRT operaci


def _api():
    """(SessionManager, PlaybackStatus) nebo None, když chybí winrt."""
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
    except ImportError:
        return None
    return Manager, Status


def _wait(op):
    """Počká na výsledek WinRT async operace (blokuje volající vlákno)."""
    op.wait(TIMEOUT)
    return op.get_results()


class MediaController:
    """Pamatuje si, co pozastavil, aby po připojení obnovil jen to."""

    def __init__(self):
        self._lock = threading.Lock()
        self._paused = []        # [(app_id, session)] pozastavené touto aplikací
        api = _api()
        self.available = api is not None
        if not self.available:
            log.info("Média: balíček winrt-Windows.Media.Control chybí, funkce vypnutá")

    def _sessions(self):
        Manager, Status = _api()
        mgr = _wait(Manager.request_async())
        return list(mgr.get_sessions()), Status

    def sessions(self):
        """[(app_id, stav)] všech mediálních relací – pro ladění."""
        sessions, Status = self._sessions()
        out = []
        for s in sessions:
            st = s.get_playback_info().playback_status
            out.append((s.source_app_user_model_id, Status(st).name))
        return out

    def pause_all(self):
        """Pozastaví všechno, co hraje. Vrací počet pozastavených relací."""
        if not self.available:
            return 0
        with self._lock:
            try:
                sessions, Status = self._sessions()
            except Exception:
                log.exception("Média: nelze načíst relace")
                return 0
            paused = []
            for s in sessions:
                try:
                    app = s.source_app_user_model_id
                    if s.get_playback_info().playback_status != Status.PLAYING:
                        continue
                    if _wait(s.try_pause_async()):
                        paused.append((app, s))
                        log.info("Média: pozastaveno %s", app)
                    else:
                        log.info("Média: %s pozastavení odmítlo", app)
                except Exception:
                    log.exception("Média: chyba při pozastavování")
            # Přepnutí na iPhone podruhé (bez připojení mezi tím) nesmí
            # zapomenout dřív pozastavené relace
            known = {id(s) for _, s in self._paused}
            self._paused += [p for p in paused if id(p[1]) not in known]
            return len(paused)

    def resume(self):
        """Spustí znovu to, co pozastavila pause_all() a je pořád pozastavené.
        Vrací počet spuštěných relací."""
        if not self.available:
            return 0
        with self._lock:
            paused, self._paused = self._paused, []
            if not paused:
                return 0
            Status = _api()[1]
            resumed = 0
            for app, s in paused:
                try:
                    # Mezitím spuštěné ručně (hraje) nebo zastavené/zavřené nechat být
                    if s.get_playback_info().playback_status != Status.PAUSED:
                        log.info("Média: %s už není pozastavené, nespouštím", app)
                        continue
                    if _wait(s.try_play_async()):
                        resumed += 1
                        log.info("Média: spuštěno %s", app)
                    else:
                        log.info("Média: %s spuštění odmítlo", app)
                except Exception as e:   # relace mezitím zanikla (zavřená karta…)
                    log.info("Média: %s nejde spustit (%s)", app, e)
            return resumed

    @property
    def has_paused(self):
        """Je co obnovovat?"""
        return bool(self._paused)


if __name__ == "__main__":
    import sys
    import time
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mc = MediaController()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for app, st in mc.sessions():
            print(f"{st:10} {app}")
    elif cmd in ("pause", "cycle"):
        print("pozastaveno:", mc.pause_all())
        if cmd == "cycle":          # pozastavit, počkat a zase spustit
            time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 3)
            print("spuštěno:", mc.resume())
    else:
        print(__doc__)
