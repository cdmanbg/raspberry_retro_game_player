#!/usr/bin/env python3
"""
cdman-web - Web UI for cdman-player
Run:  python3 cdman-web.py
Open http://<pi-ip>:8080 in a browser.

Also writes status.json next to this script so cdman-display.py can
show the current song on the Pirate Audio LCD.
"""
import os
import sys
import json
import threading
import subprocess
import signal
import importlib.util
from importlib.machinery import SourceFileLoader
from flask import Flask, request, jsonify, abort

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Allow deployment to override where user data lives so songs/status survive
# a `git pull`. Defaults preserve the original single-folder layout.
SONGS_DIR   = os.environ.get("CDMAN_SONGS_DIR")   or os.path.join(SCRIPT_DIR, "songs")
STATUS_FILE = os.environ.get("CDMAN_STATUS_FILE") or os.path.join(SCRIPT_DIR, "status.json")
PLAYER = os.path.join(SCRIPT_DIR, "cdman-player")
CONVERTER = os.path.join(SCRIPT_DIR, "cdman-convert")
HOST = "0.0.0.0"
PORT = 8080
ALLOWED_EXTS = {".txt", ".json"}
MAX_UPLOAD_BYTES = 256 * 1024

# Load the converter module (its filename has no .py extension)
_convert_mod = None
def _load_converter():
    global _convert_mod
    if _convert_mod is not None or not os.path.isfile(CONVERTER):
        return _convert_mod
    loader = SourceFileLoader("cdman_convert", CONVERTER)
    spec = importlib.util.spec_from_loader("cdman_convert", loader)
    mod = importlib.util.module_from_spec(spec)
    try:
        loader.exec_module(mod)
        _convert_mod = mod
    except Exception as e:
        print(f"converter load failed: {e}", file=sys.stderr)
    return _convert_mod

os.makedirs(SONGS_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

_play_lock = threading.Lock()
_current_proc = None
_current_song = None
_current_tempo = None
_current_volume = None


def _write_status():
    """Persist current playback state for cdman-display."""
    data = {
        "playing": _current_song,
        "tempo":   _current_tempo,
        "volume":  _current_volume,
    }
    tmp = STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_FILE)   # atomic on POSIX
    except Exception as e:
        print(f"status write failed: {e}", file=sys.stderr)


def _watch_proc(proc):
    """Reap the player when it finishes and clear 'playing' state."""
    global _current_proc, _current_song, _current_tempo, _current_volume
    proc.wait()
    with _play_lock:
        # Only clear if this proc is still the current one
        if _current_proc is proc:
            _current_proc = None
            _current_song = None
            _current_tempo = None
            _current_volume = None
            _write_status()


def _stop_current():
    global _current_proc, _current_song, _current_tempo, _current_volume
    if _current_proc and _current_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_current_proc.pid), signal.SIGTERM)
            try:
                _current_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(_current_proc.pid), signal.SIGKILL)
                _current_proc.wait(timeout=2)
        except ProcessLookupError:
            pass
    _current_proc = None
    _current_song = None
    _current_tempo = None
    _current_volume = None
    _write_status()


def _safe_song_path(name):
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidates = [name] if os.path.splitext(name)[1] in ALLOWED_EXTS else [name + ".txt", name + ".json"]
    for c in candidates:
        p = os.path.abspath(os.path.join(SONGS_DIR, c))
        if not p.startswith(SONGS_DIR + os.sep):
            return None
        if os.path.isfile(p):
            return p
    return None


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/songs")
def api_list():
    items = []
    for fn in sorted(os.listdir(SONGS_DIR)):
        if os.path.splitext(fn)[1].lower() not in ALLOWED_EXTS:
            continue
        full = os.path.join(SONGS_DIR, fn)
        items.append({
            "name": fn,
            "size": os.path.getsize(full),
            "mtime": int(os.path.getmtime(full)),
        })
    with _play_lock:
        playing = _current_song
    return jsonify({"songs": items, "playing": playing})


@app.route("/api/play", methods=["POST"])
def api_play():
    global _current_proc, _current_song, _current_tempo, _current_volume
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    path = _safe_song_path(name)
    if not path:
        return jsonify({"error": "song not found"}), 404

    tempo_val = None
    vol_val   = None
    cmd = [sys.executable, PLAYER, os.path.splitext(os.path.basename(path))[0]]
    if "tempo" in data and data["tempo"] is not None:
        try:
            tempo_val = float(data["tempo"])
            cmd += ["--tempo", str(tempo_val)]
        except (TypeError, ValueError):
            pass
    if "volume" in data and data["volume"] is not None:
        try:
            vol_val = max(0.0, min(1.0, float(data["volume"])))
            cmd += ["--volume", str(vol_val)]
        except (TypeError, ValueError):
            pass

    with _play_lock:
        _stop_current()
        proc = subprocess.Popen(
            cmd,
            cwd=SCRIPT_DIR,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _current_proc = proc
        _current_song = os.path.basename(path)
        _current_tempo = tempo_val
        _current_volume = vol_val
        _write_status()
    threading.Thread(target=_watch_proc, args=(proc,), daemon=True).start()
    return jsonify({"ok": True, "playing": _current_song})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _play_lock:
        _stop_current()
    return jsonify({"ok": True})


@app.route("/api/song/<name>", methods=["GET"])
def api_get_song(name):
    path = _safe_song_path(name)
    if not path:
        abort(404)
    with open(path, "r") as f:
        return jsonify({"name": os.path.basename(path), "content": f.read()})


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    content = data.get("content", "")
    if not name:
        return jsonify({"error": "missing name"}), 400
    if "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"error": "invalid name"}), 400
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        name += ".txt"
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "song too large"}), 413
    path = os.path.abspath(os.path.join(SONGS_DIR, name))
    if not path.startswith(SONGS_DIR + os.sep):
        return jsonify({"error": "invalid path"}), 400
    with open(path, "w") as f:
        f.write(content)
    return jsonify({"ok": True, "name": name})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    path = _safe_song_path(name)
    if not path:
        return jsonify({"error": "song not found"}), 404
    with _play_lock:
        if _current_song == os.path.basename(path):
            _stop_current()
    os.remove(path)
    return jsonify({"ok": True})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    name = os.path.basename(f.filename or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"error": "invalid filename"}), 400
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": "only .txt or .json allowed"}), 400
    path = os.path.abspath(os.path.join(SONGS_DIR, name))
    if not path.startswith(SONGS_DIR + os.sep):
        return jsonify({"error": "invalid path"}), 400
    f.save(path)
    return jsonify({"ok": True, "name": name})


@app.route("/api/convert", methods=["POST"])
def api_convert():
    """Convert Arduino Tone song to cdman .txt."""
    mod = _load_converter()
    if mod is None:
        return jsonify({"error": "converter not available on server"}), 500
    data = request.get_json(silent=True) or {}
    src = data.get("source", "")
    if not src.strip():
        return jsonify({"error": "empty source"}), 400
    try:
        tempo = int(data.get("tempo", 120))
    except (TypeError, ValueError):
        tempo = 120
    try:
        volume = float(data.get("volume", 0.01))
    except (TypeError, ValueError):
        volume = 0.01
    try:
        text, warnings, stats = mod.convert(src, tempo, volume)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"convert failed: {e}"}), 500
    return jsonify({
        "ok": True,
        "content": text,
        "warnings": warnings,
        "stats": stats,
    })


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cdman player</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: ui-monospace, "Cascadia Mono", Menlo, monospace;
    background: #0c0f14; color: #c8d3e0; min-height: 100vh;
  }
  header {
    padding: 14px 20px; border-bottom: 1px solid #1d2430;
    display: flex; align-items: center; justify-content: space-between;
    background: #0f1420; gap: 16px; flex-wrap: wrap;
  }
  header h1 { margin: 0; font-size: 18px; color: #7cd1ff; letter-spacing: 1px; }
  .now { color: #9aa6b8; font-size: 13px; }
  main {
    display: grid; grid-template-columns: 375px 1fr; gap: 14px;
    padding: 14px; max-width: 1400px; margin: 0 auto;
  }
  @media (max-width: 800px) { main { grid-template-columns: 1fr; } }
  .panel { background: #11172380; border: 1px solid #1d2430; border-radius: 10px; padding: 12px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #7cd1ff; margin: 4px 0 10px; }
  ul.songs { list-style: none; margin: 0; padding: 0; max-height: 50vh; overflow: auto; }
  ul.songs li { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; }
  ul.songs li:hover { background: #1a2233; }
  ul.songs li.active { background: #1d3550; color: #fff; }
  ul.songs li .nm { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  ul.songs li .sz { color: #6b7790; font-size: 11px; }
  button, input[type=text], input[type=number], textarea, input[type=file], select {
    font-family: inherit; font-size: 13px;
    background: #0c1220; color: #d9e2f0; border: 1px solid #28324a;
    border-radius: 6px; padding: 6px 9px;
  }
  button { cursor: pointer; } button:hover { background: #16213a; }
  button.primary { background: #1f4d80; border-color: #2d6aaa; color: #fff; }
  button.primary:hover { background: #2a5fa0; }
  button.danger  { background: #5a1f25; border-color: #883039; color: #ffd9dc; }
  button.danger:hover { background: #74262f; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  textarea { width: 100%; min-height: 280px; resize: vertical; font-family: ui-monospace, monospace; line-height: 1.45; }
  input[type=text] { width: 100%; }
  .hint { color: #6b7790; font-size: 12px; margin: 6px 0 0; }
  .toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
    background: #1d3550; color: #fff; padding: 8px 14px; border-radius: 6px;
    opacity: 0; transition: opacity .2s; pointer-events: none; z-index: 10; }
  .toast.show { opacity: 1; }
  .led { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #344055; margin-right: 6px; vertical-align: middle; }
  .led.on { background: #6ee06e; box-shadow: 0 0 6px #6ee06e; }
  .dirty-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #d6a14a;
    margin-left: 6px; vertical-align: middle; box-shadow: 0 0 4px #d6a14a; visibility: hidden; }
  .dirty-dot.show { visibility: visible; }

  /* Output destination toggle: Device | Browser */
  .out-toggle {
    display: inline-flex; align-items: center; gap: 8px;
    background: #0c1220; border: 1px solid #28324a; border-radius: 999px;
    padding: 3px; user-select: none;
  }
  .out-toggle button {
    background: transparent; border: 0; color: #9aa6b8;
    font-family: inherit; font-size: 12px; letter-spacing: 1px;
    padding: 5px 12px; border-radius: 999px; cursor: pointer;
  }
  .out-toggle button.on { background: #1f4d80; color: #fff; }
  .out-toggle .lbl { color: #9aa6b8; font-size: 12px; padding: 0 4px; }
  .sliders { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  .slider-grp { display: flex; align-items: center; gap: 8px; min-width: 220px; }
  .slider-grp label { color: #9aa6b8; font-size: 12px; min-width: 50px; }
  input[type=range] { flex: 1; accent-color: #2d6aaa; }
  .slider-grp .val { min-width: 56px; text-align: right; color: #d9e2f0; font-size: 12px;
    background: #0c1220; padding: 3px 6px; border-radius: 4px; border: 1px solid #28324a; }
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid #1d2430; margin-bottom: 10px; }
  .tab { padding: 6px 12px; cursor: pointer; border: 1px solid transparent; border-bottom: none;
    border-radius: 6px 6px 0 0; color: #9aa6b8; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
  .tab.active { background: #11172380; border-color: #1d2430; color: #7cd1ff; }
  .view { display: none; } .view.active { display: block; }
  .roll-toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
  .roll-toolbar label { color: #9aa6b8; font-size: 12px; }
  .roll-wrap { overflow: auto; border: 1px solid #1d2430; border-radius: 6px; background: #07090d; max-height: 60vh; }
  .roll { display: grid; user-select: none; }
  .roll .lbl { background: #0f1420; color: #9aa6b8; font-size: 11px; padding: 0 6px;
    position: sticky; left: 0; z-index: 2; display: flex; align-items: center;
    border-right: 1px solid #1d2430; border-bottom: 1px solid #131a26; height: 18px; min-width: 42px; }
  .roll .lbl.sharp { color: #6b7790; background: #0a0e17; }
  .roll .lbl.octC  { color: #7cd1ff; font-weight: 600; }
  .roll .cell { height: 18px; border-right: 1px solid #131a26; border-bottom: 1px solid #131a26; background: #0a0e17; cursor: pointer; }
  .roll .cell.sharp { background: #08090f; }
  .roll .cell.beat  { border-right-color: #1d2839; }
  .roll .cell.bar   { border-right-color: #2a3a55; }
  .roll .cell.on    { background: #2d6aaa; }
  .roll .cell.on.sharp { background: #1f4d80; }
  .roll .cell:hover { outline: 1px solid #3a5a88; outline-offset: -1px; }
  .roll .hdr { position: sticky; top: 0; z-index: 3; background: #0f1420; color: #6b7790;
    font-size: 10px; text-align: center; line-height: 18px; height: 18px;
    border-bottom: 1px solid #1d2430; border-right: 1px solid #131a26; }
  .roll .hdr.beat { color: #9aa6b8; }
  .roll .hdr.bar  { color: #7cd1ff; }
  .roll .hdr.corner { position: sticky; left: 0; z-index: 4; min-width: 42px; }
</style>
</head>
<body>
<header>
  <h1>♪ cdman player</h1>
  <div class="sliders">
    <div class="slider-grp">
      <label>Tempo</label>
      <input type="range" id="tempoSlider" min="40" max="400" step="1" value="220">
      <span class="val" id="tempoVal">220</span>
    </div>
    <div class="slider-grp">
      <label>Volume</label>
      <input type="range" id="volSlider" min="0" max="100" step="1" value="10">
      <span class="val" id="volVal">0.010</span>
    </div>
  </div>
  <div class="out-toggle" title="Where to play the sound">
    <span class="lbl">OUT</span>
    <button id="outDevice"  class="on"  onclick="setOutput('device')">Device</button>
    <button id="outBrowser" onclick="setOutput('browser')">Browser</button>
  </div>
  <div class="now"><span id="led" class="led"></span><span id="nowPlaying">idle</span></div>
</header>

<main>
  <section class="panel">
    <h2>Library</h2>
    <div class="row" style="margin-bottom: 8px;">
      <button onclick="refresh()">⟳ Refresh</button>
    </div>
    <ul id="songList" class="songs"></ul>
    <h2 style="margin-top:14px;">Upload</h2>
    <div class="row">
      <input type="file" id="fileInput" accept=".txt,.json">
      <button onclick="uploadFile()">Upload</button>
    </div>
    <p class="hint">.txt or .json, max 256 KB</p>
  </section>

  <section class="panel">
    <div class="row" style="margin-bottom: 10px;">
      <input type="text" id="songName" placeholder="filename.txt or .json" style="flex:1; min-width: 180px;">
      <span id="dirtyDot" class="dirty-dot" title="unsaved changes"></span>
      <button class="primary" onclick="saveSong()">💾 Save</button>
      <button onclick="newSong()">＋ New</button>
      <button class="primary" onclick="playEditor()">▶ Play this</button>
      <button class="danger" onclick="stopSong()">■ Stop</button>
      <button class="danger" onclick="deleteSong()">🗑 Delete</button>
    </div>
    <div class="tabs">
      <div class="tab active" data-view="text" onclick="switchView('text')">Text</div>
      <div class="tab" data-view="roll" onclick="switchView('roll')">Piano Roll</div>
      <div class="tab" data-view="import" onclick="switchView('import')">Import</div>
    </div>
    <div class="view active" id="view-text">
      <textarea id="editor" spellcheck="false" placeholder="# Format: NOTE DIVIDER&#10;TEMPO 220&#10;VOLUME 0.01&#10;&#10;A3 4&#10;C4 4&#10;E3 4"></textarea>
      <p class="hint">Text format: <code>NOTE DIVIDER</code> per line. <code>REST</code> for silence. Header lines <code>TEMPO N</code> / <code>VOLUME N</code> optional.</p>
    </div>
    <div class="view" id="view-roll">
      <div class="roll-toolbar">
        <label>Step:</label>
        <select id="stepDiv"><option value="4">1/4</option><option value="8">1/8</option><option value="16" selected>1/16</option><option value="32">1/32</option></select>
        <label>Steps:</label>
        <input type="number" id="stepCount" min="4" max="256" step="4" value="64" style="width:70px">
        <label>Range:</label>
        <select id="rangeLow"><option>C1</option><option selected>C2</option><option>C3</option></select>
        <span style="color:#6b7790">→</span>
        <select id="rangeHigh"><option>C5</option><option selected>C6</option><option>C7</option></select>
        <button onclick="rebuildRoll(true)">Resize</button>
        <button onclick="clearRoll()">Clear</button>
        <button onclick="rollToMelody()">Apply ⤴</button>
        <span class="hint" style="margin-left:auto">Click cells to toggle. Apply writes back to text.</span>
      </div>
      <div class="roll-wrap"><div id="roll" class="roll"></div></div>
    </div>
    <div class="view" id="view-import">
      <div class="row" style="margin-bottom: 8px;">
        <label style="color:#9aa6b8;font-size:12px;">Default tempo:</label>
        <input type="number" id="impTempo" min="40" max="400" value="120" style="width:70px">
        <label style="color:#9aa6b8;font-size:12px;">Volume:</label>
        <input type="number" id="impVolume" min="0" max="1" step="0.01" value="0.01" style="width:70px">
        <button class="primary" onclick="doConvert()">Convert ↓</button>
        <span class="hint" style="margin-left:auto">Paste Arduino code below. Tempo is used if the source doesn't declare one.</span>
      </div>
      <textarea id="impSource" spellcheck="false" placeholder="int melody[] = { NOTE_C4, NOTE_E4, ... };&#10;int durations[] = { 4, 8, ... };"></textarea>
      <p class="hint" id="impStatus">Output goes to the Text tab. Set a filename and press Save.</p>
    </div>
  </section>
</main>

<div id="toast" class="toast"></div>

<script>
let songs = [], playing = null, selected = null;
let rollNotes = [], rollGrid = [];
let stepDivider = 16, stepCount = 64, rangeLow = 'C2', rangeHigh = 'C6';
let dirty = false;

function markDirty(yes){
  dirty = !!yes;
  const d = document.getElementById('dirtyDot');
  if (d) d.classList.toggle('show', dirty);
}
// Editor text changes mark the song as dirty.
// (Attached after DOM is ready below.)

function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');clearTimeout(toast._h);toast._h=setTimeout(()=>t.classList.remove('show'),1800);}
async function api(p,o={}){const r=await fetch(p,o);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;}

const tempoSlider=document.getElementById('tempoSlider'),volSlider=document.getElementById('volSlider');
const tempoVal=document.getElementById('tempoVal'),volVal=document.getElementById('volVal');
function currentTempo(){return parseInt(tempoSlider.value,10);}
function currentVolume(){return parseInt(volSlider.value,10)/1000;}
function updateSliderLabels(){tempoVal.textContent=currentTempo();volVal.textContent=currentVolume().toFixed(3);}

// Update the TEMPO/VOLUME line in the editor textarea to match a slider.
// If the line doesn't exist yet, insert one near the top. Set markDirty so
// the change gets saved on next play/save.
function syncHeaderFromSlider(key, value){
  const ta = document.getElementById('editor');
  const text = ta.value;
  const lines = text.split(/\r?\n/);
  // build the replacement line, preserving the user's spacing style if any
  const newLine = (key === 'TEMPO') ? `TEMPO  ${value}` : `VOLUME ${value}`;
  let found = -1;
  for (let i = 0; i < lines.length; i++) {
    // strip comments and split
    const body = lines[i].split('#')[0].trim();
    if (!body) continue;
    const parts = body.split(/\s+/);
    if (parts[0].toUpperCase() === key) { found = i; break; }
  }
  if (found >= 0) {
    if (lines[found] === newLine) return; // already matches; no change
    lines[found] = newLine;
  } else {
    // Insert at top, after any leading comment lines
    let insertAt = 0;
    while (insertAt < lines.length && lines[insertAt].trim().startsWith('#')) insertAt++;
    lines.splice(insertAt, 0, newLine);
  }
  const newText = lines.join('\n');
  if (newText !== text) {
    ta.value = newText;
    markDirty(true);
  }
}

let _silenceSliderSync = false; // suppress sync while we set sliders from a loaded file
tempoSlider.oninput = () => {
  updateSliderLabels();
  if (!_silenceSliderSync) syncHeaderFromSlider('TEMPO', currentTempo());
};
volSlider.oninput = () => {
  updateSliderLabels();
  if (!_silenceSliderSync) syncHeaderFromSlider('VOLUME', currentVolume().toFixed(3));
};
updateSliderLabels();

// ===== Browser-side playback state =====
// These need to be declared BEFORE setOutput() runs, because setOutput calls
// browserIsPlaying() which reads browserActive.
let audioCtx = null;
let browserSession = 0;
let browserActive = false;
let browserCurrentName = null;

function browserIsPlaying() { return browserActive; }

// ===== Output destination (Device | Browser) =====
// Persisted across reloads so the user's choice sticks.
let outputDest = 'device';
try {
  const saved = localStorage.getItem('cdman.output');
  if (saved === 'device' || saved === 'browser') outputDest = saved;
} catch (e) { /* localStorage may be disabled */ }

function setOutput(which){
  if (which !== 'device' && which !== 'browser') return;
  // Switching destination cancels any current playback so we don't
  // end up with sound coming out of both the Pi and the browser.
  if (browserIsPlaying()) browserStop();
  outputDest = which;
  try { localStorage.setItem('cdman.output', which); } catch (e) {}
  const dev = document.getElementById('outDevice');
  const brw = document.getElementById('outBrowser');
  if (dev) dev.classList.toggle('on',  which === 'device');
  if (brw) brw.classList.toggle('on', which === 'browser');
}
// NOTE: don't call setOutput() yet — the DOM elements it touches may not be
// ready depending on script position, and browserStop() (which it may call)
// is defined further down. The init block at the bottom calls setOutput().

// ===== Browser synthesizer =====
// Mirrors cdman-player: square wave + short fade in/out, monophonic.
// Plays the same .txt/.json content the Pi player consumes.
const NOTE_FREQS = {
  C0:16.35,CS0:17.32,D0:18.35,DS0:19.45,E0:20.60,F0:21.83,FS0:23.12,G0:24.50,GS0:25.96,A0:27.50,AS0:29.14,B0:30.87,
  C1:32.70,CS1:34.65,D1:36.71,DS1:38.89,E1:41.20,F1:43.65,FS1:46.25,G1:49.00,GS1:51.91,A1:55.00,AS1:58.27,B1:61.74,
  C2:65.41,CS2:69.30,D2:73.42,DS2:77.78,E2:82.41,F2:87.31,FS2:92.50,G2:98.00,GS2:103.83,A2:110.00,AS2:116.54,B2:123.47,
  C3:130.81,CS3:138.59,D3:146.83,DS3:155.56,E3:164.81,F3:174.61,FS3:185.00,G3:196.00,GS3:207.65,A3:220.00,AS3:233.08,B3:246.94,
  C4:261.63,CS4:277.18,D4:293.66,DS4:311.13,E4:329.63,F4:349.23,FS4:369.99,G4:392.00,GS4:415.30,A4:440.00,AS4:466.16,B4:493.88,
  C5:523.25,CS5:554.37,D5:587.33,DS5:622.25,E5:659.25,F5:698.46,FS5:739.99,G5:783.99,GS5:830.61,A5:880.00,AS5:932.33,B5:987.77,
  C6:1046.50,CS6:1108.73,D6:1174.66,DS6:1244.51,E6:1318.51,F6:1396.91,FS6:1479.98,G6:1567.98,GS6:1661.22,A6:1760.00,AS6:1864.66,B6:1975.53,
  C7:2093.00,CS7:2217.46,D7:2349.32,DS7:2489.02,E7:2637.02,F7:2793.83,FS7:2959.96,G7:3135.96,GS7:3322.44,A7:3520.00,AS7:3729.31,B7:3951.07,
  C8:4186.01
};

function ensureAudio() {
  if (!audioCtx) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) throw new Error('Web Audio not supported in this browser');
    audioCtx = new Ctor();
  }
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

function browserStop() {
  browserSession++;
  browserActive = false;
  browserCurrentName = null;
  if (audioCtx && window._cdmanMaster) {
    try {
      const t = audioCtx.currentTime;
      window._cdmanMaster.gain.cancelScheduledValues(t);
      window._cdmanMaster.gain.setValueAtTime(window._cdmanMaster.gain.value, t);
      window._cdmanMaster.gain.linearRampToValueAtTime(0, t + 0.01);
    } catch (e) {}
  }
  playing = null;
  updateNow();
}

// Schedule one track on Web Audio at its own timeline (starts at startTime).
// Returns the end-time (when the last note's gap is over).
function _scheduleTrack(ctx, master, track, tempo, masterVol, ampScale, startTime, noiseBuffer) {
  const beat = 60 / tempo;
  const vol = (track.volume != null) ? track.volume : masterVol;
  let t = startTime;
  for (const [note, divider] of track.notes) {
    const fullDur = (4 / divider) * beat * 1.10;
    const soundDur = fullDur / 1.10;
    if (note !== 'REST') {
      if (track.wave === 'NOISE') {
        // Noise: use a short white-noise buffer source.
        const src = ctx.createBufferSource();
        src.buffer = noiseBuffer;
        // Slight pitch shift for variety based on note name (optional flavor)
        src.playbackRate.value = 1.0;
        src.loop = true;
        const g = ctx.createGain();
        const fade = Math.min(0.005, soundDur / 10);
        g.gain.setValueAtTime(0, t);
        g.gain.linearRampToValueAtTime(vol * ampScale, t + fade);
        g.gain.setValueAtTime(vol * ampScale, t + soundDur - fade);
        g.gain.linearRampToValueAtTime(0, t + soundDur);
        src.connect(g).connect(master);
        src.start(t);
        src.stop(t + soundDur + 0.02);
      } else {
        const freq = NOTE_FREQS[note];
        if (freq) {
          const osc = ctx.createOscillator();
          const g = ctx.createGain();
          osc.type = (track.wave === 'TRIANGLE') ? 'triangle' : 'square';
          osc.frequency.value = freq;
          const fade = Math.min(0.01, soundDur / 10);
          g.gain.setValueAtTime(0, t);
          g.gain.linearRampToValueAtTime(vol * ampScale, t + fade);
          g.gain.setValueAtTime(vol * ampScale, t + soundDur - fade);
          g.gain.linearRampToValueAtTime(0, t + soundDur);
          osc.connect(g).connect(master);
          osc.start(t);
          osc.stop(t + soundDur + 0.02);
        }
      }
    }
    t += fullDur;
  }
  return t;
}

function browserPlay(name, parsed) {
  ensureAudio();
  const ctx = audioCtx;
  const session = ++browserSession;
  browserActive = true;
  browserCurrentName = name;
  playing = name;
  updateNow();

  const master = ctx.createGain();
  master.gain.value = 1.0;
  master.connect(ctx.destination);
  window._cdmanMaster = master;

  const tempo  = currentTempo();
  const volume = currentVolume();
  // Per-track scaling: matches the Pi engine's "1/sqrt(N) if N>1 else 1"
  const activeTracks = (parsed.tracks || []).filter(tr => tr.notes && tr.notes.length > 0);
  const N = activeTracks.length;
  const ampScale = (N <= 1) ? 0.6 : (0.6 / Math.sqrt(N));

  // Pre-build a small white-noise buffer (200ms) shared by all NOISE notes.
  const noiseBuffer = ctx.createBuffer(1, ctx.sampleRate * 0.2, ctx.sampleRate);
  const nch = noiseBuffer.getChannelData(0);
  for (let i = 0; i < nch.length; i++) nch[i] = Math.random() * 2 - 1;

  const startTime = ctx.currentTime + 0.05;
  let maxEnd = startTime;
  for (const tr of activeTracks) {
    const end = _scheduleTrack(ctx, master, tr, tempo, volume, ampScale, startTime, noiseBuffer);
    if (end > maxEnd) maxEnd = end;
  }
  const totalDur = maxEnd - ctx.currentTime;

  setTimeout(() => {
    if (browserSession !== session) return;
    browserActive = false;
    browserCurrentName = null;
    playing = null;
    updateNow();
  }, Math.max(50, totalDur * 1000 + 100));

  const trackInfo = N > 1 ? ` (${N} voices)` : '';
  toast('Playing ' + name + trackInfo + ' (browser)');
}

let currentView = 'text';
function switchView(name){
  const prev = currentView;
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.view===name));
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id==='view-'+name));
  // Only sync between roll and text. Import is one-way into text.
  if (prev === 'roll' && name === 'text') {
    rollToMelody(true);          // leaving roll -> push roll into text
  } else if (name === 'roll') {
    melodyToRoll();              // entering roll -> pull text into roll
  }
  currentView = name;
}

async function doConvert(){
  const src = document.getElementById('impSource').value;
  if(!src.trim()){toast('Paste Arduino source first'); return;}
  const tempo  = parseInt(document.getElementById('impTempo').value, 10) || 120;
  const volume = parseFloat(document.getElementById('impVolume').value) || 0.01;
  const status = document.getElementById('impStatus');
  status.textContent = 'Converting...';
  try{
    const d = await api('/api/convert', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({source: src, tempo, volume})
    });
    document.getElementById('editor').value = d.content;
    markDirty(true);
    // sync sliders to converted song
    const p = parseSong(d.content);
    _silenceSliderSync = true;
    if(p.tempo  != null){ tempoSlider.value = p.tempo;  updateSliderLabels(); }
    if(p.volume != null){ volSlider.value   = Math.round(p.volume * 1000); updateSliderLabels(); }
    _silenceSliderSync = false;
    const s = d.stats || {};
    const w = (d.warnings || []).length;
    status.textContent = `OK: ${s.notes_out} notes, tempo=${s.tempo}` + (w ? `  (${w} warning${w>1?'s':''})` : '');
    if(w){ console.warn('convert warnings:', d.warnings); }
    toast('Converted ' + s.notes_out + ' notes — now in Text tab');
    switchView('text');
  }catch(e){
    status.textContent = 'Failed: ' + e.message;
    toast('Convert failed: ' + e.message);
  }
}

async function refresh(){try{const d=await api('/api/songs');songs=d.songs;
  // Server only knows about device playback. If we're playing in the browser,
  // keep showing our own "now playing" instead of clobbering it with null.
  playing = browserIsPlaying() ? browserCurrentName : d.playing;
  renderList();updateNow();}catch(e){toast('Refresh failed: '+e.message);}}
function renderList(){const ul=document.getElementById('songList');ul.innerHTML='';
  if(songs.length===0){ul.innerHTML='<li style="color:#6b7790;cursor:default;">(no songs yet)</li>';return;}
  for(const s of songs){const li=document.createElement('li');li.className=(s.name===playing?'active':'');li.onclick=()=>loadSong(s.name);li.ondblclick=()=>playSong(s.name);li.innerHTML=`<span class="nm">${s.name}</span><span class="sz">${(s.size/1024).toFixed(1)}k</span>`;ul.appendChild(li);}}
function updateNow(){
  const where = browserIsPlaying() ? ' (browser)' : '';
  document.getElementById('nowPlaying').textContent = playing ? ('playing: ' + playing + where) : 'idle';
  document.getElementById('led').classList.toggle('on', !!playing);
}

async function loadSong(name){try{const d=await api('/api/song/'+encodeURIComponent(name));
  document.getElementById('songName').value=d.name;document.getElementById('editor').value=d.content;selected=name;renderList();
  markDirty(false);
  const p=parseSong(d.content);
  _silenceSliderSync = true;
  if(p.tempo!=null){tempoSlider.value=p.tempo;updateSliderLabels();}
  if(p.volume!=null){volSlider.value=Math.round(p.volume*1000);updateSliderLabels();}
  _silenceSliderSync = false;
  if(document.getElementById('view-roll').classList.contains('active'))melodyToRoll();
}catch(e){toast('Load failed: '+e.message);}}

async function playSong(name){
  // If user has unsaved edits to *this same* song, save them first
  // so playback uses the latest version.
  const editorName = document.getElementById('songName').value.trim();
  if (dirty && editorName && editorName === name) {
    const ok = await saveSong(true);
    if (!ok) return;
  }
  if (outputDest === 'browser') {
    // Stop any device playback so we don't get parallel sound.
    try { await api('/api/stop', {method:'POST'}); } catch (e) {}
    // Use editor text if it matches this song, otherwise fetch from server.
    let content;
    if (editorName === name) {
      content = document.getElementById('editor').value;
    } else {
      try {
        const d = await api('/api/song/' + encodeURIComponent(name));
        content = d.content;
      } catch (e) { toast('Load failed: ' + e.message); return; }
    }
    const parsed = parseSong(content);
    if (!parsed.melody.length) { toast('Empty song'); return; }
    try { browserPlay(name, parsed); }
    catch (e) { toast('Browser play failed: ' + e.message); }
    return;
  }
  // Device output: hit the API.
  if (browserIsPlaying()) browserStop();
  try{await api('/api/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,tempo:currentTempo(),volume:currentVolume()})});toast('Playing '+name);setTimeout(refresh,200);}catch(e){toast('Play failed: '+e.message);}
}

async function playEditor(){
  if(document.getElementById('view-roll').classList.contains('active'))rollToMelody(true);
  const name=document.getElementById('songName').value.trim();
  if(!name){toast('Enter a filename first');return;}
  // Save first so disk matches what we're about to play (also makes the file
  // visible in the library after creating something new).
  const ok = await saveSong(true);
  if (!ok) return;
  await playSong(name);
}

async function stopSong(){
  // Stop both, regardless of toggle, so "Stop" is always a hard stop.
  if (browserIsPlaying()) browserStop();
  try{await api('/api/stop',{method:'POST'});toast('Stopped');setTimeout(refresh,200);}catch(e){toast('Stop failed: '+e.message);}
}

async function saveSong(silent){if(document.getElementById('view-roll').classList.contains('active'))rollToMelody(true);
  const name=document.getElementById('songName').value.trim(),content=document.getElementById('editor').value;
  if(!name){toast('Enter a filename');return false;}
  try{const d=await api('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,content})});
    if(!silent)toast('Saved '+d.name);document.getElementById('songName').value=d.name;
    markDirty(false);
    await refresh();
    return true;
  }catch(e){toast('Save failed: '+e.message);return false;}}

async function deleteSong(){const name=document.getElementById('songName').value.trim();if(!name){toast('Pick a song first');return;}
  if(!confirm('Delete '+name+'?'))return;
  try{await api('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});toast('Deleted');newSong();await refresh();}catch(e){toast('Delete failed: '+e.message);}}

function newSong(){document.getElementById('songName').value='';document.getElementById('editor').value='# new song\nTEMPO  220\nVOLUME 0.01\n\nA3 4\nC4 4\nE3 4\nB3 4\n';selected=null;markDirty(false);renderList();}

async function uploadFile(){const f=document.getElementById('fileInput').files[0];if(!f){toast('Pick a file first');return;}
  const fd=new FormData();fd.append('file',f);
  try{const r=await fetch('/api/upload',{method:'POST',body:fd});const d=await r.json();if(!r.ok)throw new Error(d.error||'upload failed');toast('Uploaded '+d.name);document.getElementById('fileInput').value='';await refresh();}catch(e){toast('Upload failed: '+e.message);}}

function parseSong(text){
  // Returns {tempo, volume, melody, tracks}
  // - melody: Track 1's notes (for piano-roll compat with single-voice files)
  // - tracks: array of {index, wave, volume, notes} - always present
  const out = {tempo: null, volume: null, melody: [], tracks: []};
  const t = text.trim();
  if (t.startsWith('{')) {
    try {
      const j = JSON.parse(t);
      out.tempo  = j.tempo  ?? null;
      out.volume = j.volume ?? null;
      out.melody = (j.melody || []).map(p => [String(p[0]).toUpperCase(), Number(p[1])]);
      out.tracks = [{index:1, wave:'PULSE', volume:null, notes:out.melody.slice()}];
      return out;
    } catch (e) {}
  }
  let cur = null; // current track being filled
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.split('#')[0].trim();
    if (!line) continue;
    const parts = line.split(/\s+/);
    const key = parts[0].toUpperCase();
    if (key === 'TEMPO' && parts[1]) {
      out.tempo = parseFloat(parts[1]);
    } else if (key === 'VOLUME' && parts[1]) {
      const v = parseFloat(parts[1]);
      if (cur === null) out.volume = v;   // before any TRACK -> master
      else              cur.volume = v;   // inside a track -> per-track
    } else if (key === 'TRACK') {
      let idx = 1, wave = 'PULSE';
      if (parts[1]) { const i = parseInt(parts[1], 10); if (!isNaN(i)) idx = i; }
      if (parts[2]) { const w = parts[2].toUpperCase(); if (w==='PULSE'||w==='TRIANGLE'||w==='NOISE') wave = w; }
      cur = {index: idx, wave: wave, volume: null, notes: []};
      out.tracks.push(cur);
    } else if (parts.length >= 2) {
      if (cur === null) {
        cur = {index: 1, wave: 'PULSE', volume: null, notes: []};
        out.tracks.push(cur);
      }
      // Multiple "NOTE DIVIDER" pairs per line are allowed.
      let i = 0;
      while (i < parts.length - 1) {
        const noteName = parts[i].toUpperCase();
        const div = parseFloat(parts[i + 1]);
        if (isNaN(div)) break;
        cur.notes.push([noteName, div]);
        i += 2;
      }
    }
  }
  if (out.tracks.length === 0) {
    out.tracks.push({index:1, wave:'PULSE', volume:null, notes:[]});
  }
  // 'melody' mirrors Track 1's notes for piano-roll compatibility
  const t1 = out.tracks.find(tr => tr.index === 1) || out.tracks[0];
  out.melody = t1.notes.slice();
  return out;
}
function serializeSong({tempo,volume,melody}){const lines=['# generated by cdman web editor'];if(tempo!=null)lines.push('TEMPO  '+tempo);if(volume!=null)lines.push('VOLUME '+volume);lines.push('');for(const [n,d] of melody)lines.push(n+' '+d);return lines.join('\n')+'\n';}

const SEMITONES=['C','CS','D','DS','E','F','FS','G','GS','A','AS','B'];
function noteToIndex(n){const m=n.match(/^([A-G]S?)(\d)$/);if(!m)return -1;return parseInt(m[2],10)*12+SEMITONES.indexOf(m[1]);}
function indexToNote(i){const o=Math.floor(i/12);return SEMITONES[i-o*12]+o;}
function isSharp(n){return n.length===3;}
function buildNoteList(lo,hi){const a=noteToIndex(lo),b=noteToIndex(hi);if(a<0||b<0||b<a)return [];const out=[];for(let i=b;i>=a;i--)out.push(indexToNote(i));return out;}

function rebuildRoll(preserve){
  stepDivider=parseInt(document.getElementById('stepDiv').value,10);
  stepCount=Math.max(4,Math.min(256,parseInt(document.getElementById('stepCount').value,10)||64));
  rangeLow=document.getElementById('rangeLow').value;rangeHigh=document.getElementById('rangeHigh').value;
  const newNotes=buildNoteList(rangeLow,rangeHigh);const newGrid=newNotes.map(()=>new Array(stepCount).fill(false));
  if(preserve){for(let r=0;r<rollNotes.length;r++){const nr=newNotes.indexOf(rollNotes[r]);if(nr<0)continue;
    for(let c=0;c<Math.min(stepCount,(rollGrid[r]||[]).length);c++)newGrid[nr][c]=rollGrid[r][c];}}
  rollNotes=newNotes;rollGrid=newGrid;renderRoll();}

function renderRoll(){const el=document.getElementById('roll');const cellW=22;
  el.style.gridTemplateColumns=`42px repeat(${stepCount}, ${cellW}px)`;el.style.gridAutoRows='18px';
  const parts=[];parts.push(`<div class="hdr corner"></div>`);
  const spb=stepDivider/4,spbr=spb*4;
  for(let c=0;c<stepCount;c++){let cls='hdr',label='';
    if(c%spbr===0){cls+=' bar';label=(c/spbr+1);}else if(c%spb===0){cls+=' beat';}
    parts.push(`<div class="${cls}">${label}</div>`);}
  for(let r=0;r<rollNotes.length;r++){const note=rollNotes[r];const sharp=isSharp(note);const octC=(!sharp&&note.startsWith('C')&&note.length===2);
    let lblCls='lbl'+(sharp?' sharp':'')+(octC?' octC':'');parts.push(`<div class="${lblCls}">${note}</div>`);
    for(let c=0;c<stepCount;c++){let cls='cell'+(sharp?' sharp':'');
      if(c%spbr===0&&c>0)cls+=' bar';else if(c%spb===0&&c>0)cls+=' beat';
      if(rollGrid[r][c])cls+=' on';
      parts.push(`<div class="${cls}" data-r="${r}" data-c="${c}"></div>`);}}
  el.innerHTML=parts.join('');
  let painting=false,paintVal=true;
  el.onmousedown=(e)=>{const cell=e.target.closest('.cell');if(!cell)return;const r=+cell.dataset.r,c=+cell.dataset.c;
    paintVal=!rollGrid[r][c];rollGrid[r][c]=paintVal;
    if(paintVal)for(let rr=0;rr<rollNotes.length;rr++)if(rr!==r)rollGrid[rr][c]=false;
    painting=true;redrawColumn(c);};
  el.onmousemove=(e)=>{if(!painting)return;const cell=e.target.closest('.cell');if(!cell)return;
    const r=+cell.dataset.r,c=+cell.dataset.c;if(rollGrid[r][c]===paintVal)return;
    rollGrid[r][c]=paintVal;if(paintVal)for(let rr=0;rr<rollNotes.length;rr++)if(rr!==r)rollGrid[rr][c]=false;
    redrawColumn(c);};
  document.addEventListener('mouseup',()=>{painting=false;},{once:true});}

function redrawColumn(c){for(let r=0;r<rollNotes.length;r++){const cell=document.querySelector(`.cell[data-r="${r}"][data-c="${c}"]`);if(cell)cell.classList.toggle('on',!!rollGrid[r][c]);}}
function clearRoll(){for(let r=0;r<rollGrid.length;r++)rollGrid[r].fill(false);renderRoll();}

function melodyToRoll(){rebuildRoll(false);const p=parseSong(document.getElementById('editor').value);let cursor=0;
  for(const [note,d] of p.melody){const span=Math.max(1,Math.round(stepDivider/d));if(cursor>=stepCount)break;
    if(note!=='REST'){const r=rollNotes.indexOf(note);if(r>=0)for(let i=0;i<span&&cursor+i<stepCount;i++)rollGrid[r][cursor+i]=true;}
    cursor+=span;}renderRoll();}

function rollToMelody(silent){const seq=[];let curNote=null,curLen=0;
  for(let c=0;c<stepCount;c++){let pitch=null;for(let r=0;r<rollNotes.length;r++)if(rollGrid[r][c]){pitch=rollNotes[r];break;}
    const sym=pitch||'REST';if(sym===curNote)curLen++;else{if(curNote!==null)seq.push({note:curNote,len:curLen});curNote=sym;curLen=1;}}
  if(curNote!==null&&curLen>0)seq.push({note:curNote,len:curLen});
  const melody=[];for(const it of seq){let len=it.len;while(len>0){let chunk=len;while(chunk>1&&(stepDivider%chunk)!==0)chunk--;const divider=stepDivider/chunk;melody.push([it.note,divider]);len-=chunk;}}
  while(melody.length&&melody[melody.length-1][0]==='REST')melody.pop();
  const p=parseSong(document.getElementById('editor').value);
  const out=serializeSong({tempo:p.tempo!=null?p.tempo:currentTempo(),volume:p.volume!=null?p.volume:currentVolume(),melody});
  const prev=document.getElementById('editor').value;
  document.getElementById('editor').value=out;
  if(out!==prev) markDirty(true);
  if(!silent)toast('Applied to text ('+melody.length+' notes)');}

document.getElementById('stepDiv').onchange=()=>rebuildRoll(true);
document.getElementById('stepCount').onchange=()=>rebuildRoll(true);
document.getElementById('rangeLow').onchange=()=>rebuildRoll(true);
document.getElementById('rangeHigh').onchange=()=>rebuildRoll(true);
// Update sliders to match TEMPO/VOLUME lines in the editor textarea.
// Called as the user types so changing those lines directly keeps the
// sliders (and the override values sent on Play) in sync with the file.
function syncSlidersFromEditor(){
  const p = parseSong(document.getElementById('editor').value);
  _silenceSliderSync = true;
  if (p.tempo != null) {
    const v = Math.max(40, Math.min(400, Math.round(p.tempo)));
    if (parseInt(tempoSlider.value, 10) !== v) tempoSlider.value = v;
  }
  if (p.volume != null) {
    const v = Math.max(0, Math.min(100, Math.round(p.volume * 1000)));
    if (parseInt(volSlider.value, 10) !== v) volSlider.value = v;
  }
  _silenceSliderSync = false;
  updateSliderLabels();
}

document.getElementById('editor').addEventListener('input', () => {
  markDirty(true);
  syncSlidersFromEditor();
});
// Warn before leaving with unsaved changes
window.addEventListener('beforeunload', (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ''; }
});
rebuildRoll(false);
setOutput(outputDest);   // Apply persisted toggle now that everything is defined
setInterval(refresh,3000);
refresh();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    # ensure status file exists at startup
    if not os.path.exists(STATUS_FILE):
        _write_status()
    print(f"cdman-web on http://{HOST}:{PORT}   songs dir: {SONGS_DIR}")
    app.run(host=HOST, port=PORT, threaded=True)
