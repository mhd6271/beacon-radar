# BLE-Finder (beacon_radar)

Ein kleiner **Live-BLE-Radar** im Browser. Scannt Bluetooth-Low-Energy-Geräte
(inkl. iBeacons) und zeigt sie nach **Signalstärke (Nähe)** an – ohne
unzuverlässige Meter-Schätzung, ohne Kalibrieren. Läuft lokal als ein einziges
Python-Skript und bedient sich über den Browser unter `http://localhost:8787`.

Funktioniert auf **Windows, macOS und Linux** – überall, wo `bleak` den
Bluetooth-Adapter ansprechen kann.

## Funktionen

- **Radar-Ansicht**: Mitte = du (Empfänger). Je näher ein Punkt an der Mitte,
  desto stärker das Signal. Der Winkel ist bedeutungslos.
- **Zonen** (sehr nah / nah / mittel / weit) – die Ringe lassen sich mit Maus
  oder Finger ziehen und werden lokal gespeichert.
- **Verfolgen**: ein Gerät anklicken → großer Balken + 🔥 wärmer / ❄️ kälter,
  um es physisch zu finden.
- **Annäherungs-Alarm**: Hinweiston + kurzes Aufblitzen, wenn ein Gerät die
  Zone „sehr nah" betritt (mit Hysterese/Entprellung gegen Fehlalarme).
- **Verlauf**: Protokoll der Eintritte in „sehr nah".
- **Bewegungs-Historie** (optional, SQLite): zeichnet pro Gerät auf, wie stark
  sein Signal schwankt, und zeigt eine **Top-10 „meiste Bewegung"**.
- **Typ-Erkennung** über Hersteller-Kennungen und Service-UUIDs.
- **Blacklist**: störende Geräte ausblenden.

## Installation & Start

Voraussetzung: **Python 3** und das Paket `bleak`.

```bash
pip install -r requirements.txt       # oder:  pip install bleak
python beacon_radar.py                # dann http://localhost:8787 öffnen
```

Oder bequem per Starter:
- **Windows**: `run-windows.bat` doppelklicken
- **macOS**: `run-mac.command` doppelklicken

### macOS: „externally-managed-environment"?

Neuere macOS-/Homebrew-Pythons lassen kein globales `pip install` zu. Dann ein
virtuelles Environment nutzen:

```bash
python3 -m venv venv
source venv/bin/activate
pip install bleak
python beacon_radar.py
```

Die Starter erkennen ein `venv/` im selben Ordner automatisch.

> Tipp: Auf sehr neuen Python-Versionen kann die CoreBluetooth-Anbindung
> (pyobjc) instabil sein. Bei Abstürzen hilft meist ein ausgereiftes Python
> (z. B. 3.13) in einem venv.

## Ehrliche Grenzen

Ein einzelner Empfänger misst nur **Nähe** (Signalstärke), **keine Richtung**
und **keine echten Meter**. Die angezeigte „Entfernung" ist eine grobe
Kosmetik-Schätzung. „Bewegung" in der Top-10 ist die **Schwankung der
Signalstärke** – beeinflusst von Hindernissen, Körpern und Antennenlage – also
ein grober Indikator, keine Ortung.

## Daten

Alles läuft lokal. Laufzeitdateien (Verlauf, Bewegungs-DB, Pin/Blacklist) werden
neben dem Skript angelegt und sind per `.gitignore` von der Versionierung
ausgenommen.

## Lizenz

[MIT](LICENSE) © 2026 Max Mühlbronner
