#!/usr/bin/env python3
"""
beacon_radar.py — Beacon-Radar: einfacher Live-BLE-Radar im Browser

Scannt BLE-Geräte (inkl. iBeacons) und zeigt sie nach SIGNALSTÄRKE an —
ohne unzuverlässige Meter-Schätzung, ohne Kalibrieren.

Bedienung:
  * Gerät anklicken -> "Verfolgen": großer Balken + 🔥 wärmer / ❄️ kälter,
    während du dich bewegst. So findest du ein Gerät physisch.
  * Das angeheftete Gerät wird SERVERSEITIG gespeichert und nach einem
    Neustart automatisch wieder markiert.

ANHEFTEN & MAC-ROTATION:
  Geräte, die ein iBeacon senden, werden über ihre feste iBeacon-Identität
  (UUID/Major/Minor) wiedererkannt — stabil über Neustarts und MAC-Wechsel.
  Reine (Nicht-iBeacon-)Geräte wie Handys rotieren ihre BLE-MAC absichtlich
  und lassen sich NICHT dauerhaft wiedererkennen. Lösung: auf dem Handy eine
  Beacon-App (z. B. "Beacon Simulator") mit fester UUID laufen lassen.

EHRLICHE GRENZE: Ein Empfänger misst nur NÄHE (Signalstärke), keine Richtung
und keine echten Meter. Zum Finden reicht "näher/weiter" bestens.

Abhängigkeit:  pip install bleak
Start:         python beacon_radar.py     (dann http://localhost:8787 öffnen)
"""

import argparse
import asyncio
import json
import os
import sqlite3
import struct
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from bleak import BleakScanner
except ImportError:
    raise SystemExit("Fehlt: bleak  ->  pip install bleak")

VERSION = "36 (einheitlicher Name Beacon-Radar)"
APPLE_ID = 0x004C
STATE = {}
STATE_LOCK = threading.Lock()
EWMA_ALPHA = 0.35
HIST_SECONDS = 120

PIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beacon_radar_pin.json")
PIN = None


def load_pin():
    global PIN
    try:
        with open(PIN_FILE, "r", encoding="utf-8") as f:
            PIN = json.load(f).get("pin")
    except Exception:
        PIN = None


def save_pin():
    try:
        with open(PIN_FILE, "w", encoding="utf-8") as f:
            json.dump({"pin": PIN}, f)
    except Exception:
        pass


HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beacon_radar_history.jsonl")
HISTORY = []
HIST_MAX = 1000


def load_history():
    try:
        with open(HIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    HISTORY.append(json.loads(line))
        del HISTORY[0:max(0, len(HISTORY) - HIST_MAX)]
    except Exception:
        pass


def add_history(entry):
    HISTORY.append(entry)
    del HISTORY[0:max(0, len(HISTORY) - HIST_MAX)]
    try:
        with open(HIST_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


BLACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beacon_radar_blacklist.json")
BLACKLIST = {}   # key -> label (ausgeblendete Geräte)


def load_blacklist():
    global BLACKLIST
    try:
        with open(BLACK_FILE, "r", encoding="utf-8") as f:
            BLACKLIST = json.load(f)
    except Exception:
        BLACKLIST = {}


def save_blacklist():
    try:
        with open(BLACK_FILE, "w", encoding="utf-8") as f:
            json.dump(BLACKLIST, f)
    except Exception:
        pass


def dev_key(d):
    ib = d.get("ibeacon")
    if ib:
        return f"ib:{ib['uuid']}:{ib['major']}:{ib['minor']}"
    return f"mac:{d['id']}"


# ------------------------------------------------------------------ #
#  Konfiguration (einfache Optionen, in JSON gespeichert)
# ------------------------------------------------------------------ #
CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beacon_radar_cfg.json")
RECORD_MOVE = False   # Bewegungs-Aufzeichnung an/aus (Standard: aus)


def load_cfg():
    global RECORD_MOVE
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            RECORD_MOVE = bool(json.load(f).get("record_move", False))
    except Exception:
        RECORD_MOVE = False


def save_cfg():
    try:
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump({"record_move": RECORD_MOVE}, f)
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  Bewegungs-Statistik pro Gerät  (SQLite = historisch über Neustarts)
#  "Bewegung" = Schwankung der Signalstärke von EINEM Empfänger aus.
#  Das ist ein grober Indikator (Hindernisse/Körper/Antenne wirken mit),
#  KEINE echte Ortung. Zwei Kennzahlen:
#    cum  = Summe der |dB-Änderungen|  -> "wie viel bewegt / unruhig"
#    rmax-rmin = Spanne                -> "wie weit (Nähe) geändert"
# ------------------------------------------------------------------ #
MOVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beacon_movement.db")
MOVE = {}              # key -> {name,typ,cum,rmin,rmax,samples,first,last}
MOVE_MIN_SAMPLES = 5   # erst ab so vielen Messungen im Ranking zeigen


def move_db_init():
    try:
        con = sqlite3.connect(MOVE_FILE)
        con.execute("""CREATE TABLE IF NOT EXISTS movement(
            key TEXT PRIMARY KEY, name TEXT, typ TEXT,
            cum REAL, rmin REAL, rmax REAL, samples INTEGER,
            first_ts REAL, last_ts REAL)""")
        con.commit(); con.close()
    except Exception:
        pass


def move_load():
    try:
        con = sqlite3.connect(MOVE_FILE)
        for r in con.execute("SELECT key,name,typ,cum,rmin,rmax,samples,first_ts,last_ts FROM movement"):
            MOVE[r[0]] = {"name": r[1], "typ": r[2], "cum": r[3], "rmin": r[4],
                          "rmax": r[5], "samples": r[6], "first": r[7], "last": r[8]}
        con.close()
    except Exception:
        pass


def move_flush():
    try:
        with STATE_LOCK:
            items = [(k, m["name"], m["typ"], m["cum"], m["rmin"], m["rmax"],
                      m["samples"], m["first"], m["last"]) for k, m in MOVE.items()]
        con = sqlite3.connect(MOVE_FILE)
        con.executemany("""INSERT INTO movement
            (key,name,typ,cum,rmin,rmax,samples,first_ts,last_ts) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET name=excluded.name,typ=excluded.typ,cum=excluded.cum,
            rmin=excluded.rmin,rmax=excluded.rmax,samples=excluded.samples,last_ts=excluded.last_ts""",
            items)
        con.commit(); con.close()
    except Exception:
        pass


def move_clear():
    with STATE_LOCK:
        MOVE.clear()
    try:
        con = sqlite3.connect(MOVE_FILE); con.execute("DELETE FROM movement"); con.commit(); con.close()
    except Exception:
        pass


def move_flush_loop():
    while True:
        time.sleep(15)
        if RECORD_MOVE:
            move_flush()


def move_top(sort="akt", n=10):
    with STATE_LOCK:
        items = []
        for k, m in MOVE.items():
            if m["samples"] < MOVE_MIN_SAMPLES:
                continue
            items.append({"key": k, "name": m["name"], "typ": m["typ"],
                          "cum": round(m["cum"], 1), "range": round(m["rmax"] - m["rmin"], 1),
                          "samples": m["samples"]})
    items.sort(key=lambda x: x["range"] if sort == "spanne" else x["cum"], reverse=True)
    return items[:n]


def record_move(d, old_rssi, now):   # Aufruf NUR mit gehaltenem STATE_LOCK
    k = dev_key(d)
    if k in BLACKLIST:
        return
    r = d["rssi"]
    m = MOVE.get(k)
    if m is None:
        MOVE[k] = {"name": d["name"], "typ": classify(d), "cum": 0.0,
                   "rmin": r, "rmax": r, "samples": 1, "first": now, "last": now}
    else:
        if old_rssi is not None:
            m["cum"] += abs(r - old_rssi)
        if r < m["rmin"]:
            m["rmin"] = r
        if r > m["rmax"]:
            m["rmax"] = r
        m["samples"] += 1
        m["last"] = now
        if d["name"]:
            m["name"] = d["name"]
        if m.get("typ") in (None, "", "unbekannt"):   # Typ nur nachziehen, bis bekannt (spart classify im Hot-Path)
            m["typ"] = classify(d)


def parse_ibeacon(mfg: dict):
    if not mfg:
        return None
    p = mfg.get(APPLE_ID)
    if not p or len(p) < 23 or p[0] != 0x02 or p[1] != 0x15:
        return None
    major, minor, tx = struct.unpack(">HHb", p[18:23])
    return {"uuid": p[2:18].hex(), "major": major, "minor": minor, "tx": tx}


# Häufige BLE-Hersteller-Kennungen (Company Identifier aus den Manufacturer-Daten)
COMPANY = {
    0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google",
    0x0059: "Nordic", 0x012D: "Sony", 0x0157: "Huawei", 0x038F: "Xiaomi",
    0x0087: "Garmin", 0x009E: "Bose", 0x00D2: "Logitech",
}
# 16-Bit Service-UUIDs -> grobe Kategorie
SVC = {
    "1812": "Eingabegerät (HID)", "180d": "Wearable/Herzfreq.",
    "feaa": "Eddystone-Beacon", "fd6f": "Exposure-Notif.",
    "fe9f": "Google Fast Pair", "fef3": "Google Fast Pair",
}


def classify(d):
    name = d.get("name") or ""
    low = name.lower()
    parts = []
    vend = None
    for c in d.get("companies", []):
        if c in COMPANY:
            vend = COMPANY[c]
            break
    if "blukii" in low:
        vend = "blukii"
    if d.get("ibeacon") and vend == "Apple":
        vend = None   # iBeacon trägt immer Apples Company-ID -> kein Apple-Gerät
    if vend:
        parts.append(vend)
    cat = None
    for s in d.get("svcs", []):
        if s in SVC:
            cat = SVC[s]
            break
    if d.get("ibeacon"):
        cat = "iBeacon"
    if any(k in low for k in ["jbl", "bose", "buds", "airpod", "headphone",
                              "speaker", "soundcore", "flip", "kopfh"]):
        cat = cat or "Audio"
    if cat:
        parts.append(cat)
    return " · ".join(parts) if parts else "unbekannt"


def on_detect(device, adv):
    # Wrapper: NIEMALS eine Exception in den CoreBluetooth/pyobjc-Callback
    # zurückwerfen — das kann den gesamten Prozess (nativ) zum Absturz bringen.
    try:
        _on_detect(device, adv)
    except Exception:
        pass


def _on_detect(device, adv):
    now = time.time()
    rssi = adv.rssi
    if rssi is None:
        return
    ib = parse_ibeacon(adv.manufacturer_data)
    companies = list(adv.manufacturer_data.keys()) if adv.manufacturer_data else []
    svcs = []
    for u in (adv.service_uuids or []):
        u = str(u).lower()
        if len(u) >= 8:
            svcs.append(u[4:8])
    with STATE_LOCK:
        d = STATE.get(device.address)
        old_rssi = None
        if d is None:
            STATE[device.address] = {
                "id": device.address, "name": device.name, "rssi": float(rssi),
                "ibeacon": ib, "companies": companies, "svcs": svcs,
                "last": now, "hist": deque(maxlen=600),
            }
            d = STATE[device.address]
        else:
            old_rssi = d["rssi"]
            d["rssi"] = EWMA_ALPHA * rssi + (1 - EWMA_ALPHA) * d["rssi"]
            if device.name:
                d["name"] = device.name
            if ib:
                d["ibeacon"] = ib
            if companies:
                d["companies"] = companies
            if svcs:
                d["svcs"] = svcs
            d["last"] = now
        d["hist"].append([round(now, 1), round(d["rssi"], 1)])
        if RECORD_MOVE:
            record_move(d, old_rssi, now)


def snapshot():
    now = time.time()
    out = []
    with STATE_LOCK:
        stale = [sk for sk, sv in STATE.items() if now - sv["last"] > 60]
        for sk in stale:                         # Speicher begrenzen (rotierende MACs)
            del STATE[sk]
        for d in STATE.values():
            if now - d["last"] > 20:
                continue
            k = dev_key(d)
            if k in BLACKLIST:                   # ausgeblendete Geräte komplett weglassen
                continue
            out.append({
                "id": d["id"], "name": d["name"], "rssi": round(d["rssi"], 1),
                "ibeacon": d["ibeacon"], "key": k, "typ": classify(d),
                "age": round(now - d["last"], 1),
                # Verlauf nur fürs angeheftete Gerät senden (Payload sparen):
                "hist": ([h for h in d["hist"] if now - h[0] <= HIST_SECONDS] if k == PIN else []),
            })
    out.sort(key=lambda x: x["rssi"], reverse=True)
    return {"now": now, "pin": PIN, "blacklist": BLACKLIST, "devices": out}


def ble_loop():
    async def run():
        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        while True:
            await asyncio.sleep(1.0)
    asyncio.run(run())


HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0e1116">
<title>Beacon-Radar</title>
<style>
  :root{--bg:#0e1116;--panel:#161b22;--line:#30363d;--txt:#e6edf3;--muted:#8b949e;
    --accent:#58a6ff;--warn:#d29922;--hot:#f85149;--cold:#58a6ff;--me:#f0b429;--ok:#3fb950}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
  #flash{position:fixed;inset:0;background:#f85149;opacity:0;pointer-events:none;z-index:9999;display:none}
  #flash.flashing{display:block;animation:flashpulse 1.1s ease-out}
  @keyframes flashpulse{0%{opacity:0}9%{opacity:.55}22%{opacity:0}36%{opacity:.55}50%{opacity:0}64%{opacity:.55}100%{opacity:0}}
  header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  h1{font-size:16px;margin:0}
  .muted{color:var(--muted)}
  button{background:#21262d;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:4px 10px;cursor:pointer;font-size:13px}
  .wrap{display:flex;gap:16px;padding:16px;flex-wrap:wrap}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  #track{display:none;margin:12px 16px 0;padding:16px;border-radius:12px;background:var(--panel);border:1px solid var(--me)}
  #track h2{margin:0 0 10px;font-size:18px}
  #track.collapsed #tbody{display:none}
  #thead{cursor:pointer;user-select:none}
  .meter{height:34px;background:#0d1117;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .meter>div{height:100%;transition:width .3s,background .3s}
  .hint{font-size:28px;font-weight:800;margin-top:10px}
  canvas{display:block;max-width:100%;margin:0 auto}
  table{border-collapse:collapse;width:100%;font-size:11px}
  th,td{text-align:left;padding:2px 6px;border-bottom:1px solid var(--line);white-space:nowrap;line-height:1.15}
  th{color:var(--muted);font-weight:600}
  tr.sel{background:rgba(240,180,41,.14)}
  tr{cursor:pointer}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .tag{font-size:11px;color:var(--bg);background:var(--accent);border-radius:6px;padding:1px 6px;margin-left:6px}
  .pill{display:inline-block;min-width:74px;text-align:center;border-radius:12px;padding:2px 8px;font-size:12px;font-weight:700}
</style>
</head>
<body>
<header>
  <h1>📡 Beacon-Radar <span class="muted" id="count"></span></h1>
  <button id="sound" title="Hinweiston an/aus">🔔</button>
  <span class="muted">Klick ein Gerät zum Anheften/Verfolgen. Ton bei Eintritt in „sehr nah".</span>
  <span class="muted" style="margin-left:auto">Build 36</span>
</header>

<div id="blacklistbar" style="padding:5px 16px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line);display:none"></div>
<div id="flash"></div>

<div id="track">
  <div id="thead" style="display:flex;justify-content:space-between;align-items:center;gap:10px">
    <h2 id="tname" style="margin:0"></h2>
    <span id="tcollapse" title="Ein-/Ausklappen" style="font-size:20px">▾</span>
  </div>
  <div id="tbody">
    <div class="meter" style="margin-top:10px"><div id="tbar"></div></div>
    <div style="display:flex;justify-content:space-between;margin-top:6px">
      <span class="muted" id="tdbm"></span>
      <button id="untrack">Anheften lösen</button>
    </div>
    <div class="hint" id="thint"></div>
  </div>
</div>

<div class="wrap">
  <div class="card" style="flex:2;min-width:320px">
    <canvas id="radar" width="700" height="700"></canvas>
    <div class="muted" style="font-size:12px;margin-top:8px;max-width:400px">
      Mitte = du (Empfänger). Je näher ein Punkt an der Mitte, desto
      <b>stärker das Signal</b> (= näher). Der Winkel ist bedeutungslos.
      <br>Tipp: Die <b>Zonen-Ringe mit Maus oder Finger ziehen</b> (rein/raus), um die Zonen zu verändern.
    </div>
  </div>
  <div class="card" style="flex:1;min-width:240px">
    <table>
      <thead><tr><th>Gerät</th><th>Signal</th><th>Nähe</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</div>

<div class="wrap">
  <div class="card" style="flex:1;min-width:320px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <b>📋 Verlauf — Eintritte in „sehr nah" <span class="muted" id="histcount"></span></b>
      <button id="histclear">Verlauf löschen</button>
    </div>
    <table>
      <thead><tr><th>Zeit</th><th>Gerät</th><th>Typ</th><th>Signal</th></tr></thead>
      <tbody id="histrows"></tbody>
    </table>
  </div>
</div>

<div class="wrap">
  <div class="card" style="flex:1;min-width:320px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px;flex-wrap:wrap">
      <b>📈 Top 10 — meiste Bewegung <span class="muted" id="movenote"></span></b>
      <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <button id="moverec" title="Bewegung aufzeichnen an/aus">⏺ Aufzeichnung</button>
        <button id="movesort" title="Sortierung umschalten">Sortier: Aktivität</button>
        <button id="moveclear">Zurücksetzen</button>
      </span>
    </div>
    <table>
      <thead><tr><th>#</th><th>Gerät</th><th>Aktivität</th><th>Spanne</th><th>Messungen</th></tr></thead>
      <tbody id="moverows"></tbody>
    </table>
    <div class="muted" style="font-size:11px;margin-top:6px">
      „Aktivität" = Summe der Signal-Schwankungen (dB) · „Spanne" = max−min (dB).
      Grober Indikator für Unruhe/Nähe-Änderung von <b>einem</b> Empfänger aus — keine echte Ortung.
      Wird nur gezählt, solange die Aufzeichnung läuft; die Statistik bleibt über Neustarts erhalten.
    </div>
  </div>
</div>

<script>
let DATA={now:0,pin:null,devices:[]}, PIN=null, COLL=false;
try{ COLL=localStorage.getItem('trackcoll')==='1'; }catch(e){}
let TH={sehrnah:-60,nah:-72,mittel:-83,weit:-90};
try{ const s=JSON.parse(localStorage.getItem('th')); if(s) TH=Object.assign(TH,s); }catch(e){}

document.getElementById('untrack').onclick=(e)=>{ e.stopPropagation(); pin(null); };
function applyColl(){
  document.getElementById('track').classList.toggle('collapsed', COLL);
  document.getElementById('tcollapse').textContent = COLL?'▸':'▾';
}
document.getElementById('thead').addEventListener('click', ()=>{
  COLL=!COLL; try{ localStorage.setItem('trackcoll',COLL?'1':'0'); }catch(e){} applyColl();
});
applyColl();

// --- Hinweiston bei Eintritt in "nah" (mit Hysterese + Entprellung) ---
let soundOn=true; try{ soundOn=localStorage.getItem('snd')!=='0'; }catch(e){}
const sBtn=document.getElementById('sound');
function updSound(){ sBtn.textContent=soundOn?'🔔':'🔕'; }
sBtn.onclick=()=>{ soundOn=!soundOn; try{localStorage.setItem('snd',soundOn?'1':'0');}catch(e){} updSound(); if(soundOn) audio(); };
updSound();

let actx=null;
function audio(){ try{ if(!actx) actx=new (window.AudioContext||window.webkitAudioContext)();
  if(actx.state==='suspended') actx.resume(); }catch(e){} return actx; }
document.addEventListener('click', ()=>audio());   // Audio nach erster Interaktion freischalten
function playChime(){
  const ac=audio(); if(!ac) return; const t0=ac.currentTime;
  const notes=[988,1319,1760];      // ansteigender Dreiklang (B5-E6-A6)
  for(let rep=0; rep<2; rep++){     // 2x wiederholt -> auffälliger
    notes.forEach((f,i)=>{
      const o=ac.createOscillator(), g=ac.createGain();
      o.type='triangle'; o.frequency.value=f;   // schärfer/lauter als sine
      o.connect(g); g.connect(ac.destination);
      const s=t0 + rep*0.55 + i*0.13;
      g.gain.setValueAtTime(0.0001,s);
      g.gain.exponentialRampToValueAtTime(0.6,s+0.015);   // lauter (war 0.22)
      g.gain.exponentialRampToValueAtTime(0.0001,s+0.12);
      o.start(s); o.stop(s+0.14);
    });
  }
}
let nearState={}, lastBeep=0, lastFlash=0, startTime=performance.now();
const ENTER_MARGIN=3;   // dB "hinter" der Linie, damit es als voll überschritten zählt
const DWELL_MS=600;     // so lange muss es drinnen bleiben, bevor es auslöst
const WARMUP_MS=8000;   // Aufwärmphase: anfangs schon anwesende Geräte lösen NICHT aus
const GAP_MS=10000;     // nach so langer Abwesenheit gilt ein Gerät als "neu aufgetaucht"
let origTitle=document.title, titleTimer=null;
const flashEl=document.getElementById('flash');
if(flashEl) flashEl.addEventListener('animationend', ()=>flashEl.classList.remove('flashing'));
function flashScreen(){
  if(!flashEl) return;
  flashEl.classList.remove('flashing');
  void flashEl.offsetWidth;        // Reflow -> Animation startet sicher neu
  flashEl.classList.add('flashing');
}
function flashTitle(){
  clearInterval(titleTimer); let on=true, c=0;
  titleTimer=setInterval(()=>{ document.title=on?'🚨 GERÄT NAH!':origTitle; on=!on; c++;
    if(c>=12){ clearInterval(titleTimer); document.title=origTitle; } },450);
}
function onEnter(d){
  logEntry(d);                                            // Verlauf-Eintrag
  const t=performance.now();
  if(soundOn && t-lastBeep>1500){ lastBeep=t; playChime(); }
  if(t-lastFlash>1500){ lastFlash=t; flashScreen(); flashTitle(); }   // optischer Alarm (entprellt)
}
function checkProximity(){
  const now=performance.now();
  const enter=TH.sehrnah+ENTER_MARGIN;   // klar DRINNEN (stärkeres Signal)
  const exit =TH.sehrnah-ENTER_MARGIN;   // klar DRAUSSEN
  const warm = (now-startTime)>WARMUP_MS;
  for(const d of DATA.devices){
    let st=nearState[d.key];
    // Neu aufgetaucht (erstmals ODER nach längerer Abwesenheit, z. B. nach BT aus/an):
    if(!st || now-st.lastSeen>GAP_MS){
      const inside=d.rssi>=enter;
      nearState[d.key]={inside:inside, since:null, lastSeen:now};
      if(inside && warm && d.age<4) onEnter(d);           // schon drinnen beim Auftauchen -> Alarm
      continue;
    }
    st.lastSeen=now;
    if(!st.inside){
      if(d.rssi>=enter && d.age<4){                       // klar drinnen & frisch gesehen
        if(st.since==null) st.since=now;                  // Verweil-Timer starten
        else if(now-st.since>=DWELL_MS){ st.inside=true; st.since=null; onEnter(d); }
      } else { st.since=null; }                           // wieder rausgefallen -> Timer zurück
    } else {
      if(d.rssi<exit) st.inside=false;                    // erst "draußen", wenn klar drunter
    }
  }
  for(const k in nearState){ if(now-nearState[k].lastSeen>GAP_MS*3) delete nearState[k]; }  // Speicher begrenzen
}

// --- Zonen-Ringe per Maus ziehen ---
const RC=document.getElementById('radar');
RC.style.cursor='pointer';
const ZKEYS=['sehrnah','nah','mittel','weit'];
function radiusToRssi(r,maxR){ return Math.round((1-r/maxR)*50-95); }
function evtRadius(e){
  const b=RC.getBoundingClientRect();
  const x=(e.clientX-b.left)*(RC.width/b.width), y=(e.clientY-b.top)*(RC.height/b.height);
  const cx=RC.width/2, cy=RC.height/2;
  return {r:Math.hypot(x-cx,y-cy), maxR:Math.min(RC.width,RC.height)/2-16, scale:RC.width/b.width};
}
let dragKey=null;
function dragStart(e,tolPx){   // tolPx = Fangbereich in Bildschirm-Pixeln (Finger braucht mehr)
  const {r,maxR,scale}=evtRadius(e); let best=null,bd=tolPx*scale;
  ZKEYS.forEach(k=>{ const rr=(1-nearFrac(TH[k]))*maxR; const d=Math.abs(rr-r); if(d<bd){bd=d;best=k;} });
  dragKey=best;
  return !!best;
}
function dragMove(e){
  const {r,maxR}=evtRadius(e); const v=radiusToRssi(r,maxR);
  const i=ZKEYS.indexOf(dragKey);
  const upper = i>0 ? TH[ZKEYS[i-1]]-3 : -40;   // nicht über den inneren Ring
  const lower = i<3 ? TH[ZKEYS[i+1]]+3 : -99;   // nicht unter den äußeren Ring
  TH[dragKey]=Math.max(lower,Math.min(upper,v)); drawRadar();
}
function dragEnd(){
  if(dragKey){ try{localStorage.setItem('th',JSON.stringify(TH));}catch(e){} dragKey=null; }
}
RC.addEventListener('mousedown',e=>dragStart(e,14));
window.addEventListener('mousemove',e=>{ if(dragKey) dragMove(e); });
window.addEventListener('mouseup',dragEnd);
// Touch: nur wenn ein Ring getroffen wird, das Scrollen der Seite unterdrücken
RC.addEventListener('touchstart',e=>{
  if(e.touches.length===1 && dragStart(e.touches[0],24)) e.preventDefault();
},{passive:false});
RC.addEventListener('touchmove',e=>{
  if(dragKey){ e.preventDefault(); dragMove(e.touches[0]); }
},{passive:false});
RC.addEventListener('touchend',dragEnd);
RC.addEventListener('touchcancel',dragEnd);

async function pin(key){
  PIN=key;
  try{ await fetch('/pin?'+(key?('key='+encodeURIComponent(key)):'clear=1')); }catch(e){}
  render();
}

function nearFrac(rssi){ const r=Math.max(-95,Math.min(-45,rssi)); return (r+95)/50; }
function nearLabel(rssi){
  if(rssi>=TH.sehrnah) return ['sehr nah','#3fb950'];
  if(rssi>=TH.nah) return ['nah','#58a6ff'];
  if(rssi>=TH.mittel) return ['mittel','#d29922'];
  if(rssi>=TH.weit) return ['weit','#db6d28'];
  return ['sehr weit','#8b949e'];
}
// Nur kosmetische Grobschätzung der Entfernung (Pfadverlust, Indoor n=3). NICHT für Logik.
function distEst(rssi, tx){
  const n=3.0, t=tx||-59;
  const m=Math.pow(10,(t-rssi)/(10*n));
  if(m<1) return '<1 m';
  if(m>30) return '>30 m';
  return (m<10 ? m.toFixed(1) : Math.round(m))+' m';
}
function colorFor(id){ let h=0; for(const c of id) h=(h*31+c.charCodeAt(0))>>>0; return `hsl(${h%360} 70% 60%)`; }
function angleFor(id){ let h=0; for(const c of id) h=(h*131+c.charCodeAt(0))>>>0; return (h%360)*Math.PI/180; }
function swing(hist,sec){ if(!hist||!hist.length)return 0; const t=DATA.now-sec;
  const v=hist.filter(p=>p[0]>=t).map(p=>p[1]); if(v.length<2)return 0; return Math.max(...v)-Math.min(...v); }
function nm(d){ return d.name||('Gerät '+d.id.slice(-5)); }
function esc(s){ return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function isBlukii(d){ return /blukii/i.test(d.name||''); }
function isPinned(d){ return PIN && d.key===PIN; }

function drawRadar(){
  const cv=document.getElementById('radar'),ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,cx=W/2,cy=H/2,maxR=Math.min(W,H)/2-16;
  ctx.clearRect(0,0,W,H);
  ctx.textAlign='left';
  const zones=[['sehr nah',TH.sehrnah],['nah',TH.nah],['mittel',TH.mittel],['weit',TH.weit]];
  ctx.strokeStyle='#30363d';ctx.fillStyle='#8b949e';ctx.font='11px sans-serif';
  for(const [lbl,r] of zones){ const rad=(1-nearFrac(r))*maxR;
    ctx.beginPath();ctx.arc(cx,cy,rad,0,2*Math.PI);ctx.stroke();
    ctx.fillText(lbl+' ('+r+')',cx+4,cy-rad+12); }
  ctx.fillStyle='#3fb950';ctx.beginPath();ctx.arc(cx,cy,5,0,2*Math.PI);ctx.fill();
  ctx.fillStyle='#e6edf3';ctx.fillText('du',cx+8,cy+4);
  for(const d of DATA.devices){
    const sel=isPinned(d), blk=isBlukii(d), rad=(1-nearFrac(d.rssi))*maxR, a=angleFor(d.id);
    const x=cx+rad*Math.cos(a), y=cy+rad*Math.sin(a);
    ctx.globalAlpha=Math.max(0.35,1-d.age/20);
    ctx.fillStyle=sel?'#f0b429':(blk?'#2dd4bf':colorFor(d.id));
    ctx.beginPath();ctx.arc(x,y,sel?9:6,0,2*Math.PI);ctx.fill();
    ctx.globalAlpha=1;
    if(sel){ctx.strokeStyle='#f0b429';ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,13,0,2*Math.PI);ctx.stroke();ctx.lineWidth=1;}
    else if(blk){ctx.strokeStyle='#2dd4bf';ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,11,0,2*Math.PI);ctx.stroke();ctx.lineWidth=1;}
    ctx.fillStyle=sel?'#f0b429':(blk?'#2dd4bf':'#e6edf3');
    const lbl=(sel?'⭐ ':'')+(blk?'🔑 ':'')+nm(d)+'  ≈'+distEst(d.rssi,(d.ibeacon&&d.ibeacon.tx)||-59).replace(' ','');
    if(x>cx){ ctx.textAlign='right'; ctx.fillText(lbl, x-11, y+4); }   // rechte Hälfte: Label nach innen
    else    { ctx.textAlign='left';  ctx.fillText(lbl, x+11, y+4); }
  }
  ctx.textAlign='left';
}

function drawTable(){
  const tb=document.getElementById('rows');tb.innerHTML='';
  for(const d of DATA.devices){
    const [lab,col]=nearLabel(d.rssi), sel=isPinned(d), isBl=isBlukii(d);
    const tr=document.createElement('tr'); tr.className=sel?'sel':'';
    tr.onclick=(e)=>{
      if(e.target.closest('.blk')){ e.stopPropagation(); black(d.key, nm(d)); return; }
      pin(sel?null:d.key);
    };
    tr.innerHTML=`<td><span class="blk" data-k="${d.key}" data-l="${esc(nm(d))}" title="Ausblenden (Blacklist)" style="cursor:pointer;opacity:.5">🚫</span>
      <span class="dot" style="background:${sel?'#f0b429':(isBl?'#2dd4bf':colorFor(d.id))}"></span>
      ${(sel?'⭐ ':'')+(isBl?'🔑 ':'')+esc(nm(d))}${d.ibeacon?'<span class="tag">iBeacon</span>':''}${isBl?'<span class="tag" style="background:#2dd4bf;color:#0d1117">Fob</span>':''}
      <br><span class="muted" style="font-size:11px">${esc(d.id)}</span>
      <br><span class="muted" style="font-size:11px">🏷️ ${esc(d.typ||'unbekannt')}  ·  ≈ ${distEst(d.rssi,(d.ibeacon&&d.ibeacon.tx)||-59)}</span></td>
      <td>${d.rssi.toFixed(0)} dBm</td>
      <td><span class="pill" style="background:${col};color:#0d1117">${lab}</span></td>`;
    tb.appendChild(tr);
  }
  document.getElementById('count').textContent='('+DATA.devices.length+' Geräte)';
}

function drawTrack(){
  const box=document.getElementById('track');
  if(!PIN){ box.style.display='none'; return; }
  box.style.display='block';
  applyColl();
  const d=DATA.devices.find(x=>x.key===PIN);
  if(!d){
    document.getElementById('tname').textContent='Angeheftetes Gerät'+(COLL?' — nicht in Reichweite':'');
    document.getElementById('tbar').style.width='0%';
    document.getElementById('tdbm').textContent='gerade nicht in Reichweite …';
    const th=document.getElementById('thint'); th.textContent='❓ kein Signal'; th.style.color='#8b949e';
    return;
  }
  const frac=nearFrac(d.rssi), [lab,col]=nearLabel(d.rssi);
  document.getElementById('tname').textContent='Verfolge: '+nm(d)+(COLL?('  —  '+d.rssi.toFixed(0)+' dBm · '+lab):'');
  const bar=document.getElementById('tbar'); bar.style.width=(frac*100).toFixed(0)+'%'; bar.style.background=col;
  document.getElementById('tdbm').textContent=d.rssi.toFixed(0)+' dBm · '+lab+(d.age>5?'  (gerade nicht gesehen …)':'');
  const past=d.hist.filter(p=>p[0]<=DATA.now-4);
  let hint='· bleib in Bewegung ·', hc='#8b949e';
  if(past.length){ const diff=d.rssi-past[past.length-1][1];
    if(diff>2){hint='🔥 WÄRMER (näher dran)';hc='#f85149';}
    else if(diff<-2){hint='❄️ kälter (weiter weg)';hc='#58a6ff';}
    else{hint='↔ etwa gleich';hc='#8b949e';} }
  const th=document.getElementById('thint'); th.textContent=hint; th.style.color=hc;
}

function logEntry(d){
  const p=new URLSearchParams({id:d.id,name:nm(d),typ:d.typ||'',rssi:String(Math.round(d.rssi))});
  fetch('/log?'+p.toString()).catch(()=>{});
  setTimeout(loadHistory,300);
}
async function loadHistory(){
  try{ const r=await fetch('/history'); const j=await r.json();
    const tb=document.getElementById('histrows'); tb.innerHTML='';
    for(const h of j.history){
      const t=new Date(h.ts*1000).toLocaleString('de-DE');
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${t}</td><td>${esc(h.name||h.id)}<br><span class="muted">${esc(h.id)}</span></td>
        <td class="muted" style="font-size:12px">${esc(h.typ||'')}</td><td>${esc(String(h.rssi))} dBm</td>`;
      tb.appendChild(tr);
    }
    document.getElementById('histcount').textContent='('+j.history.length+')';
  }catch(e){}
}
document.getElementById('histclear').onclick=async()=>{ try{await fetch('/history?clear=1');}catch(e){} loadHistory(); };

// --- Blacklist (Geräte ausblenden) ---
async function black(key,label){ try{ await fetch('/black?add='+encodeURIComponent(key)+'&label='+encodeURIComponent(label||'')); }catch(e){} }
async function unblack(key){ try{ await fetch('/black?remove='+encodeURIComponent(key)); }catch(e){} }
document.getElementById('rows').addEventListener('click', e=>{
  const b=e.target.closest('.blk'); if(b){ e.stopPropagation(); black(b.dataset.k, b.dataset.l); }
});
document.getElementById('blacklistbar').addEventListener('click', e=>{
  const u=e.target.closest('.unblk'); if(u) unblack(u.dataset.k);
});
function renderBlacklist(){
  const bar=document.getElementById('blacklistbar'), bl=DATA.blacklist||{}, keys=Object.keys(bl);
  if(!keys.length){ bar.style.display='none'; return; }
  bar.style.display='block';
  bar.innerHTML='🚫 Ausgeblendet: '+keys.map(k=>
    `<span style="background:#21262d;border:1px solid var(--line);border-radius:10px;padding:1px 8px;margin:0 4px;white-space:nowrap">${esc(bl[k]||k)} <span class="unblk" data-k="${esc(k)}" style="cursor:pointer;color:#f85149">✕</span></span>`).join('');
}

function render(){ drawTrack(); drawRadar(); drawTable(); renderBlacklist(); }
async function tick(){
  try{ const r=await fetch('/data'); DATA=await r.json(); PIN=DATA.pin; }catch(e){ return; }
  checkProximity();
  render();
}
setInterval(tick,700); tick();
setInterval(loadHistory,4000); loadHistory();

// --- Bewegungs-Historie (Top 10) ---
let moveSort='akt', moveRec=false;
const recBtn=document.getElementById('moverec'), sortBtn=document.getElementById('movesort');
function updRecBtn(){
  recBtn.textContent = moveRec ? '⏹ Aufzeichnung läuft' : '⏺ Aufzeichnung aus';
  recBtn.style.background = moveRec ? '#238636' : '#21262d';
  recBtn.style.color = moveRec ? '#fff' : 'var(--txt)';
}
recBtn.onclick=async()=>{ moveRec=!moveRec; try{ await fetch('/cfg?record='+(moveRec?1:0)); }catch(e){} updRecBtn(); loadMove(); };
sortBtn.onclick=()=>{ moveSort=(moveSort==='akt'?'spanne':'akt');
  sortBtn.textContent='Sortier: '+(moveSort==='akt'?'Aktivität':'Spanne'); loadMove(); };
document.getElementById('moveclear').onclick=async()=>{
  if(!confirm('Bewegungs-Statistik wirklich zurücksetzen?')) return;
  try{ await fetch('/move?clear=1'); }catch(e){} loadMove();
};
async function loadMove(){
  try{
    const r=await fetch('/move?sort='+moveSort); const j=await r.json();
    moveRec=j.record; updRecBtn();
    const tb=document.getElementById('moverows'); tb.innerHTML='';
    j.top.forEach((m,i)=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${i+1}</td>
        <td>${esc(m.name||m.key)}<br><span class="muted" style="font-size:11px">🏷️ ${esc(m.typ||'')}</span></td>
        <td><b>${m.cum.toFixed(0)}</b> dB</td>
        <td>${m.range.toFixed(0)} dB</td>
        <td class="muted">${m.samples}</td>`;
      tb.appendChild(tr);
    });
    document.getElementById('movenote').textContent = moveRec
      ? '('+j.top.length+' · zeichnet auf)'
      : (j.top.length ? '('+j.top.length+' · pausiert)' : '(aus — zum Starten „Aufzeichnung")');
  }catch(e){}
}
setInterval(loadMove,4000); loadMove();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        global PIN, RECORD_MOVE
        parsed = urlparse(self.path)
        if parsed.path == "/pin":
            qs = parse_qs(parsed.query)
            if "clear" in qs:
                PIN = None; save_pin()
            elif "key" in qs:
                PIN = qs["key"][0]; save_pin()
            body = json.dumps({"pin": PIN}).encode(); ctype = "application/json"
        elif parsed.path == "/log":
            qs = parse_qs(parsed.query)
            add_history({
                "ts": time.time(),
                "id": qs.get("id", [""])[0],
                "name": qs.get("name", [""])[0],
                "typ": qs.get("typ", [""])[0],
                "rssi": qs.get("rssi", [""])[0],
            })
            body = json.dumps({"ok": True}).encode(); ctype = "application/json"
        elif parsed.path == "/history":
            qs = parse_qs(parsed.query)
            if "clear" in qs:
                HISTORY.clear()
                try:
                    open(HIST_FILE, "w").close()
                except Exception:
                    pass
            body = json.dumps({"history": HISTORY[-200:][::-1]}).encode(); ctype = "application/json"
        elif parsed.path == "/black":
            qs = parse_qs(parsed.query)
            if "add" in qs:
                BLACKLIST[qs["add"][0]] = qs.get("label", [""])[0]
                save_blacklist()
            elif "remove" in qs:
                BLACKLIST.pop(qs["remove"][0], None)
                save_blacklist()
            body = json.dumps({"blacklist": BLACKLIST}).encode(); ctype = "application/json"
        elif parsed.path == "/move":
            qs = parse_qs(parsed.query)
            if "clear" in qs:
                move_clear()
            sort = qs.get("sort", ["akt"])[0]
            body = json.dumps({"record": RECORD_MOVE, "top": move_top(sort)}).encode()
            ctype = "application/json"
        elif parsed.path == "/cfg":
            qs = parse_qs(parsed.query)
            if "record" in qs:
                RECORD_MOVE = qs["record"][0] in ("1", "true", "on")
                save_cfg()
                if not RECORD_MOVE:
                    move_flush()   # beim Ausschalten noch sichern
            body = json.dumps({"record": RECORD_MOVE}).encode(); ctype = "application/json"
        elif parsed.path == "/data":
            body = json.dumps(snapshot()).encode(); ctype = "application/json"
        else:
            body = HTML.encode(); ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    load_pin()
    load_history()
    load_blacklist()
    load_cfg()
    move_db_init()
    move_load()
    threading.Thread(target=ble_loop, daemon=True).start()
    threading.Thread(target=move_flush_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Beacon-Radar läuft (Build {VERSION}):  http://localhost:{args.port}")
    print("Scanne BLE … (STRG+C beendet)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
