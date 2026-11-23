# AirPods Switch

Malá tray aplikace pro Windows 10/11, která přepíná AirPods (nebo jiná BT sluchátka)
mezi PC a iPhonem. AirPods nemají multipoint s Windows: když je aplikace odpojí
od PC, iPhone si je za pár sekund sám převezme. Připojení zpět k PC spouští aplikace.

## Ovládání

| Akce | Jak |
|---|---|
| Okno se stavem | **levý klik** na ikonu (znovu klik / Esc / klik mimo okno ho zavře) |
| Přepnout (PC ↔ iPhone) | **Ctrl+Alt+B**, tlačítko v okně, nebo menu *Přepnout sluchátka* |
| Stav | okno, tooltip ikony, nebo šedá položka *Stav: …* v menu |
| Které sluchátka jsou u PC / výběr | v okně klik na jméno v hlavičce (rozbalí seznam se stavem), nebo menu *Sluchátka* |
| Spouštění s Windows | přepínač v okně nebo menu *Spouštět s Windows* (obojí je synchronní) |
| Připojit po odemčení PC | přepínač v okně nebo menu – po odemčení (Win+L), přihlášení nebo probuzení se vybraná sluchátka připojí k PC |
| Pozastavit média při přepnutí | přepínač v okně nebo menu – před přepnutím na iPhone pozastaví, co na PC hraje, po připojení zpět to zase spustí (viz níže) |
| Změna klávesové zkratky | v okně klik na odznak se zkratkou a stisk nové kombinace (Esc = zrušit) |
| Baterie AirPods | v okně pod jménem sluchátek – ikony baterie L / P / pouzdro (zelená, ≤ 30 % oranžová, ≤ 10 % červená, blesk = nabíjí), když jsou sluchátka vyndaná nebo pouzdro otevřené |
| Obnovit audio služby | menu – zapne audio služby u všech sluchátek (viz níže) |

Tray ikona má stejný tvar jako ikona aplikace, barva ukazuje stav: modrá = připojeno k PC ·
šedá = odpojeno · oranžová = přepínám · červená = chyba (např. zařízení nenalezeno).
Po každé akci přijde krátká notifikace Windows.

Okno (`popup.py`) se kreslí přes Pillow (kvůli vyhlazeným tvarům), zobrazuje se u hodin
nad hlavním panelem podle DPI monitoru, na kterém se kliklo.

Náhled okna bez Bluetooth, tray i hotkey (pro ladění vzhledu):

```powershell
python airpods_switch.py --preview okno.png connected   # nebo disconnected | busy | error | picker
```

### Připojení po odemčení

Aplikace si skrytým oknem nechá od Windows posílat zprávy o odemčení/přihlášení
(`WTSRegisterSessionNotification`) a probuzení (`RegisterSuspendResumeNotification`).
Po odemčení počká ~4 s, po probuzení ~8 s (BT se rozjíždí) a připojí vybraná sluchátka.
Když jsou v pouzdře nebo mimo dosah, nic nehlásí (jen do logu). Když se aplikace
spustí do 5 minut od startu Windows (autostart), bere se to jako přihlášení.

### Média

Před odpojením od PC aplikace pozastaví všechno, co na PC hraje (Spotify, YouTube
v prohlížeči, …), aby zvuk nepřeskočil do reproduktorů. Po připojení zpět
(zkratkou, z okna, z menu i po odemčení) spustí znovu jen to, co sama pozastavila
a co je pořád pozastavené. Co mezitím spustíš nebo zavřeš ručně, nechá být.
Když se odpojení nepovede, média se hned zase spustí.

Funguje pro aplikace, které se ukazují v panelu médií Windows (u hlasitosti);
modul `media.py`, balíček `winrt-Windows.Media.Control`. Média se ovládají jen
při přepnutí přes aplikaci. Když si sluchátka iPhone vezme sám nebo je dáš
do pouzdra, aplikace média nepozastaví.

### Baterie

AirPods vysílají stav baterie v Bluetooth LE reklamách Applu (typ „proximity pairing“);
čte je modul `battery.py` přes WinRT. Formát Apple nedokumentuje (dekódování podle
komunitních projektů). Reklama nenese adresu sluchátek, jen model – když je poblíž
víc AirPods stejného modelu, ukáže se stav těch s nejsilnějším signálem.
Bez balíčků `winrt-*` se baterie prostě nezobrazuje.

## Konfigurace – `config.json` (vedle exe)

```json
{
  "device_name": "AirPods 4",
  "hotkey": "ctrl+alt+b",
  "auto_connect": false,
  "pause_media": true
}
```

- Vybraná sluchátka se dají změnit i bez editace souboru: v okně klikem na jméno
  v hlavičce, nebo v menu *Sluchátka*. Výběr se uloží sem do `device_name`.

- `device_name` – jméno zařízení tak, jak je v Nastavení → Bluetooth. Stačí jednoznačná
  část jména (např. `AirPods`); při víc shodách aplikace ohlásí, které zařízení myslíš.
- `hotkey` – modifikátory `ctrl`, `alt`, `shift`, `win` + jedna klávesa (`a`–`z`, `0`–`9`,
  `f1`–`f24`, `space`, `pause`, `home`, …). Např. `ctrl+shift+f9`. Jde měnit i v okně.
- `auto_connect` – `true` = připojit sluchátka po odemčení/probuzení PC (přepínač v okně).
- `pause_media` – `true` (výchozí) = při přepnutí na iPhone pozastavit média na PC a po návratu
  je spustit (přepínač v okně).
- `fast_disconnect` – `true` (výchozí) = při odpojení nejdřív shodit BT spojení a audio služby
  vypnout až potom, takže iPhone dostane sluchátka asi o 3 s dřív. `false` = původní pořadí
  (nejdřív služby, pak spojení). Když se při `true` sluchátka občas vrátí k PC, přepni na `false`.

Když soubor chybí, vytvoří se s výchozími hodnotami. Změny platí po restartu aplikace
(menu *Konec* a spustit znovu).

Log je v `airpods_switch.log` vedle exe (max. ~1 MB).

## Instalace

Doporučené umístění je `%LOCALAPPDATA%\Programs\AirPodsSwitch\` (bez admin práv),
zástupce na ploše si vytvoř na `AirPodsSwitch.exe`. Autostart se zapíná v menu ikony
nebo přepínačem v okně. Postup sestavení a kopírování je níže.

## Build

Potřeba: Python 3 a balíčky z `requirements.txt`:

```powershell
python -m pip install -r requirements.txt
```

(`winrt-*` jsou jen pro zobrazení baterie AirPods a ovládání médií; bez nich aplikace funguje dál.)

Build (ve složce projektu):

```powershell
python airpods_switch.py --make-icon AirPodsSwitch.ico
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name AirPodsSwitch --icon AirPodsSwitch.ico --hidden-import pystray._win32 airpods_switch.py
```

Výsledek: `dist\AirPodsSwitch.exe`. Instalace = zkopírovat exe (a případně `config.json`)
do libovolné složky, např.:

```powershell
$dst = "$env:LOCALAPPDATA\Programs\AirPodsSwitch"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item dist\AirPodsSwitch.exe $dst -Force
```

Před kopírováním aplikaci ukonči (menu *Konec*), jinak je exe zamčené.

Když se po nové verzi na zástupci drží stará ikona (mezipaměť ikon Windows se řídí
cestou k souboru), nastav zástupci ikonu z `AirPodsSwitch.ico` zkopírovaného vedle exe
(Vlastnosti zástupce → Změnit ikonu).
Pokud je zapnutý autostart a exe přesuneš, cesta se při dalším spuštění opraví sama.

## Jak to funguje (a proč)

Bluetooth logika je v `bt_airpods.py`. Testováno na Windows 11 s Intel Smart Sound
Bluetooth audio offloadem:

- **Odpojení** = vypnout audio služby A2DP a Hands-Free (`BluetoothSetServiceState`)
  a pak shodit BT spojení (`IOCTL_BTH_DISCONNECT_DEVICE`). Trvá ~4 s.
  - Samotné shození spojení nestačí – Windows sluchátka hned připojí zpátky.
  - Samotné vypnutí služeb nestačí – AirPods drží spojení přes Apple protokol (AAP),
    takže zůstanou u PC bez zvuku a iPhone si je nevezme.
  - „Oficiální“ cesta (KS property `KSPROPSETID_BtAudio`) ovladač Intel přijme, ale ignoruje.
- **Připojení** = zapnout audio služby; Windows se pak ke sluchátkům samo připojí (~3 s).
  Pokud služby zapnuté jsou (sluchátka se odpojila jinak), zkusí se KS reconnect
  a když nezabere, restart služeb.
- Vše funguje **bez admin práv**.

Stav „připojeno k PC“ = BT spojení existuje **a** audio služba je zapnutá.

### Vlákna

tkinter (okno) běží v hlavním vlákně, pystray (tray) přes `run_detached()` ve svém
vlákně, hotkey a zjišťování stavu ve vlastních vláknech, Bluetooth operace ve worker
vlákně (vždy jen jedna, další stisky se ignorují). Ostatní vlákna do GUI nesahají
přímo – posílají požadavky přes frontu, kterou tk vybírá každých 30 ms.

### Důležité: připojování ručně v Nastavení

Když sluchátka odpojí tahle aplikace, jsou pro ně ve Windows vypnuté audio služby.
Připojení ručně v Nastavení → Bluetooth pak **nedá zvuk** – připojuj je přes aplikaci
(*Připojit k PC* / Ctrl+Alt+B), ta služby zase zapne.

Než aplikaci odinstaluješ nebo ji přestaneš používat, dej v menu **Obnovit audio služby**:
zapne služby u všech spárovaných sluchátek, aby fungovala i bez aplikace. Pozor, Windows
se po zapnutí služeb ke sluchátkům v dosahu samo připojí (vezme je iPhonu).

## Ladění z příkazové řádky

```powershell
python bt_airpods.py list                  # spárovaná zařízení a stav
python bt_airpods.py status AirPods        # stav včetně audio služeb
python bt_airpods.py on|off|toggle AirPods
python bt_airpods.py watch AirPods 30      # 30 s sleduje změny stavu
python bt_airpods.py diag AirPods          # nalezené audio (KS) filtry
python media.py list                       # mediální relace (co hraje) a jejich stav
python media.py cycle 5                    # pozastavit, 5 s počkat, spustit
```
