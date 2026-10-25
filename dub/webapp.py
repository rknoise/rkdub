# -*- coding: utf-8 -*-
"""Az rkdub felülete: saját weboldal (dub/web) + kis helyi API (FastAPI). Csak a saját gépről érhető el.
A szinkron külön folyamatban fut (worker.JobProcess); ez a szerver csak indítja, figyeli és kiszolgálja."""

import io
import os
import shutil
import subprocess
import sys
import threading
import time
import wave

import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, media, script
from . import translate as tr
from .pipeline import DEFAULT_OPTIONS, QC_FLAG, DubJob, safe_stem
from .util import NO_WINDOW, OUT_DIR, ROOT, WORK_DIR, fmt_time, load_settings, read_json, save_settings
from .worker import JobProcess

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
UPLOAD_DIR = os.path.join(ROOT, "feltoltes")

TRANSLATORS = [("ollama", "Helyi AI (Ollama)"), ("deepl", "DeepL")]
FORMALITY = [("auto", "Automatikus"), ("informal", "Tegező"), ("formal", "Magázó")]
REF_MODES = [("hu", "Eredeti hang, magyarosítva (ajánlott)"), ("line", "Eredeti hang, soronkénti hangsúllyal"),
             ("speaker", "Eredeti hang, beszélőnként (angol mintából)")]
ASR_MODELS = [("large-v3-turbo", "large-v3-turbo (gyors, ajánlott)"), ("large-v3", "large-v3 (legpontosabb)")]
OPTION_KEYS = ["translator", "ollama_model", "deepl_key", "formality", "num_speakers", "speaker_genders",
               "ref_mode", "notes",
               "keep_background", "keep_nonspeech", "keep_original_track", "subtitles", "stop_after_translate",
               "asr_model", "tempo"]


def _level(msg):
    m = msg.strip()
    if m.startswith("HIBA") or "Traceback" in m:
        return "error"
    if m.startswith("Figyelem") or "nem sikerült" in m:
        return "warn"
    if "] kész (" in m or m.startswith("Kész") or m.startswith("Megállítva") or m.startswith("Összesen"):
        return "success"
    if msg.startswith("  "):
        return "detail"
    return "process"


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.opts = None
        self.proc = None
        self.status = "idle"            # idle | running | done | stopped | error
        self.progress, self.desc = 0.0, ""
        self.t0, self.elapsed = 0.0, 0.0
        self.logs = []
        self.result = None
        self.error = ""
        self.message = ""
        self.exported = ""
        self.settings = dict(DEFAULT_OPTIONS, **load_settings())
        self.settings.setdefault("asr_model", "large-v3-turbo")
        self.settings.setdefault("tempo", 1.0)

    def log(self, msg, level=None):
        with self.lock:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "level": level or _level(msg), "msg": msg})
            del self.logs[:-2000]

    # --- munka indítása, figyelése --------------------------------------------------
    def start(self, src, opts, message):
        with self.lock:
            if self.proc and not self.proc.finished:
                raise HTTPException(409, "Már fut egy szinkron. Várd meg, vagy szakítsd meg.")
            self.status, self.progress, self.desc = "running", 0.0, message
            self.result, self.error, self.message = None, "", ""
            self.t0 = time.time()
        self.log(message, "info")
        self.proc = JobProcess(src, opts, self.job.dir)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        res, err = None, ""
        while not proc.finished:
            for ev in proc.events(0.5):
                if ev[0] == "log":
                    self.log(ev[1])
                elif ev[0] == "p":
                    with self.lock:
                        self.progress, self.desc = ev[1], ev[2] or self.desc
                elif ev[0] == "done":
                    res = ev[1]
                elif ev[0] == "err":
                    err = ev[1]
            with self.lock:
                self.elapsed = time.time() - self.t0
        with self.lock:
            self.elapsed = time.time() - self.t0
            if err or not res:
                self.status = "stopped" if err.startswith("Megszakítva") else "error"
                self.error = err or "ismeretlen hiba"
            elif res.get("stopped"):
                self.status, self.result = "review", res
                self.message = (f"A fordítás kész ({res.get('lines', 0)} sor). Nézd át lent, vagy exportáld "
                                "javításra, aztán: Hang készítése.")
            else:
                self.status, self.result = "done", res
                self.message = f"Kész! {res.get('dubbed', 0)}/{res.get('lines', 0)} sor szinkronizálva."
            self.progress = 1.0 if self.status in ("done", "review") else self.progress


S = AppState()
app = FastAPI(title="rkdub", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Csak a saját gépről, és más weboldalakról érkező kéréseket nem fogad el."""
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"detail": "csak helyi elérés"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
        return JSONResponse({"detail": "idegen oldal"}, status_code=403)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    # frissítés után a böngésző ne a régi app.js-t használja a gyorsítótárból
    stamp = int(os.path.getmtime(os.path.join(WEB_DIR, "static", "app.js")))
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={stamp}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# --- beállítások, állapot ------------------------------------------------------------

@app.get("/api/init")
def api_init():
    models = tr.ollama_models()
    s = dict(S.settings)
    if s.get("ollama_model") not in models:
        s["ollama_model"] = tr.pick_default_model(models)
    s["deepl_key"] = tr.load_deepl_key(s)
    return {"settings": {k: s.get(k) for k in OPTION_KEYS}, "models": models, "version": __version__,
            "choices": {"translator": TRANSLATORS, "formality": FORMALITY, "ref_mode": REF_MODES,
                        "asr_model": ASR_MODELS}, "gpu": gpu_info()}


def _options(body):
    o = {k: body.get(k, S.settings.get(k)) for k in OPTION_KEYS}
    o["num_speakers"] = max(0, min(10, int(o.get("num_speakers") or 0)))
    g = o.get("speaker_genders") or []
    o["speaker_genders"] = [x for x in g if x in ("f", "m")][: o["num_speakers"]] if isinstance(g, list) else []
    o["tempo"] = float(o.get("tempo") or 1.0)
    for k in ("keep_background", "keep_nonspeech", "keep_original_track", "subtitles", "stop_after_translate"):
        o[k] = bool(o.get(k))
    S.settings.update(o)
    saved = dict(S.settings)
    if saved.get("deepl_key", "") == tr.load_deepl_key({}):   # az rksubby kulcsa: nem kell még egy példány
        saved.pop("deepl_key", None)
    save_settings(saved)
    return o


@app.post("/api/settings")
async def api_settings(request: Request):
    _options(await request.json())
    return {"ok": True}


@app.get("/api/state")
def api_state(since: int = 0):
    with S.lock:
        logs = S.logs[since:]
        st = {"status": S.status, "progress": S.progress, "desc": S.desc, "elapsed": fmt_time(S.elapsed),
              "message": S.message, "error": S.error, "logs": logs, "log_total": len(S.logs),
              "job": safe_stem(S.job.src) if S.job else "", "has_lines": bool(S.job and
                                                                              os.path.exists(S.job.p("lines.json")))}
    if S.status == "running":
        with S.lock:
            S.elapsed = time.time() - S.t0
            st["elapsed"] = fmt_time(S.elapsed)
    return st


_gpu_cache = {"t": 0.0, "v": None}


def gpu_info():
    """GPU neve, terhelése, memóriája és hőmérséklete (nvidia-smi), 1 mp-es gyorsítótárral."""
    if time.time() - _gpu_cache["t"] < 1.0 and _gpu_cache["v"]:
        return _gpu_cache["v"]
    exe = shutil.which("nvidia-smi")
    v = {"ok": False, "name": "Nincs NVIDIA-kártya"}
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                                 creationflags=NO_WINDOW).stdout.strip().splitlines()[0]
            name, util, used, total, temp = [x.strip() for x in out.split(",")]
            v = {"ok": True, "name": name, "util": int(util), "mem_used": int(used) / 1024,
                 "mem_total": int(total) / 1024, "temp": int(temp)}
        except Exception:
            pass
    _gpu_cache.update(t=time.time(), v=v)
    return v


@app.get("/api/gpu")
def api_gpu():
    return gpu_info()


# --- bemenet ---------------------------------------------------------------------------

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    name = os.path.basename(file.filename or "video.mp4")
    if not media.is_media(name):
        raise HTTPException(400, "Ezt a fájltípust nem ismerem (videó: mp4, mkv, mov, webm...; hang: wav, mp3, m4a...).")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        while True:
            chunk = await file.read(8 << 20)
            if not chunk:
                break
            f.write(chunk)
    return {"path": path, "size": os.path.getsize(path)}


@app.post("/api/browse")
def api_browse():
    """Windows fájlválasztó ablak (a szerver a saját gépen fut, így ez a felhasználó gépén nyílik meg)."""
    exts = " ".join("*" + e for e in sorted(media.VIDEO_EXT | media.AUDIO_EXT))
    code = ("import tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw();r.attributes('-topmost',True)\n"
            f"p=filedialog.askopenfilename(title='Videó vagy hangfájl kiválasztása',filetypes=[('Videó és hang','{exts}'),"
            "('Minden fájl','*.*')])\nprint(p or '')")
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=600,
                             encoding="utf-8", errors="replace", creationflags=NO_WINDOW).stdout.strip()
    except Exception as e:
        raise HTTPException(500, f"a fájlválasztó nem nyílt meg: {e}")
    return {"path": os.path.normpath(out) if out else ""}


# --- futtatás ----------------------------------------------------------------------------

@app.post("/api/run")
async def api_run(request: Request):
    body = await request.json()
    src = (body.get("path") or "").strip().strip('"')
    if not src or not os.path.isfile(src):
        raise HTTPException(400, "Válassz ki egy videót vagy hangfájlt (húzd be, tallózd ki, vagy írd be az útját).")
    if not media.is_media(src):
        raise HTTPException(400, "Ezt a fájltípust nem ismerem (videó: mp4, mkv, mov, webm...; hang: wav, mp3, m4a...).")
    o = _options(body.get("options", {}))
    S.job, S.opts = DubJob(src, o), o
    with S.lock:
        S.logs = []
    S.start(src, o, f"Indul: {os.path.basename(src)}")
    return {"ok": True}


@app.post("/api/stop")
def api_stop():
    if S.proc and not S.proc.finished:
        S.proc.stop()
        S.log("Megszakítás kérve (a folyamatban lévő lépés végén áll meg)...", "warn")
    return {"ok": True}


@app.get("/api/lines")
def api_lines():
    if not S.job:
        return {"lines": []}
    rows = []
    for ln in S.job.lines():
        note = ""
        if ln.get("qc_cer", 0) > QC_FLAG:
            note = f"A visszahallgatás eltérést talált: „{ln.get('qc_heard', '')}”"
        elif ln.get("fits") is False:
            note = f"Hosszú: {ln.get('tts_dur', 0):.1f} mp a {ln.get('avail', 0):.1f} mp helyett"
        rows.append({"no": ln["id"] + 1, "start": ln["start"], "time": fmt_time(ln["start"]),
                     "speaker": ln.get("speaker", 0) + 1, "en": ln["en"], "hu": ln.get("hu", ""),
                     "say": ln.get("say", ""), "note": note, "edited": bool(ln.get("edited")),
                     "has_tts": bool(ln.get("tts_file") and os.path.exists(S.job.p(ln["tts_file"])))})
    return {"lines": rows}


@app.post("/api/regenerate")
async def api_regenerate(request: Request):
    if not S.job or not S.opts:
        raise HTTPException(400, "Előbb futtass le egy szinkront (vagy legalább a fordítást).")
    body = await request.json()
    lines = S.job.lines()
    by_no = {ln["id"] + 1: ln for ln in lines}
    changed = 0
    for e in body.get("edits", []):
        ln = by_no.get(int(e.get("no", 0)))
        if not ln:
            continue
        hu, say = str(e.get("hu", "")).strip(), str(e.get("say", "")).strip()
        spk = e.get("speaker")
        if spk and int(spk) - 1 != ln.get("speaker", 0):     # más szereplőhöz rendelték: az ő hangján készül
            ln["speaker"], ln["speaker_edited"] = int(spk) - 1, True
            changed += 1
        if say != (ln.get("say") or ""):              # a kiejtést írták át
            if say:
                ln["say"], ln["say_edited"] = say, True
            else:
                ln.pop("say", None)
                ln.pop("say_edited", None)
            changed += 1
        if hu and hu != ln.get("hu"):                  # a szöveget írták át
            ln["hu"], ln["edited"] = hu, True
            if not ln.get("say_edited"):              # a régi kiejtés már nem érvényes
                ln.pop("say", None)
            changed += 1
    S.job.save_lines(lines)
    # a hanggal és keveréssel kapcsolatos beállítások változhatnak; a fordításé nem (az újrafordítaná,
    # és elveszne a kézi javítás)
    new = body.get("options") or {}
    voice_keys = ("ref_mode", "tempo", "keep_background", "keep_nonspeech", "keep_original_track", "subtitles")
    o = dict(S.opts, stop_after_translate=False)
    o.update({k: new[k] for k in voice_keys if k in new})
    o["tempo"] = float(o.get("tempo") or 1.0)
    S.opts = o
    S.start(S.job.src, o, f"Hang készítése ({changed} módosított sor)...")
    return {"ok": True, "changed": changed}


def _wav_bytes(y, sr):
    y = np.clip(y, -1, 1)
    if y.ndim > 1:
        y = y.mean(axis=1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


@app.get("/api/clip")
def api_clip(no: int, track: str = "hu"):
    """Egy sor meghallgatása: az eredeti (a videó hangjából) vagy a kész magyar."""
    if not S.job:
        raise HTTPException(404, "nincs munka")
    ln = next((x for x in S.job.lines() if x["id"] + 1 == no), None)
    if not ln:
        raise HTTPException(404, "nincs ilyen sor")
    if track == "hu":
        f = ln.get("tts_file")
        if not f or not os.path.exists(S.job.p(f)):
            raise HTTPException(404, "ennek a sornak még nincs magyar hangja")
        return FileResponse(S.job.p(f), media_type="audio/wav", headers={"Cache-Control": "no-store"})
    import soundfile as sf

    info = sf.info(S.job.p("audio.wav"))
    a = int(max(0.0, ln["start"] - 0.2) * info.samplerate)
    b = int((ln["end"] + 0.3) * info.samplerate)
    y, sr = sf.read(S.job.p("audio.wav"), start=a, stop=b, dtype="float32")
    return Response(_wav_bytes(y, sr), media_type="audio/wav", headers={"Cache-Control": "no-store"})


# --- export / import ----------------------------------------------------------------------

@app.get("/api/export")
def api_export():
    if not S.job or not os.path.exists(S.job.p("lines.json")):
        raise HTTPException(400, "Előbb futtass le egy szinkront (vagy legalább a fordítást).")
    lang = (read_json(S.job.p("words.json")) or {}).get("lang", "en")
    path, text = script.export_script(S.job, lang)
    S.exported = text
    return {"path": path, "text": text}


def _apply_import(text, src_name):
    try:
        st = script.apply_script(S.job, text)
    except ValueError as e:
        raise HTTPException(400, f"Ezt nem tudtam beolvasni: {e}")
    S.log(f"Importálva ({src_name}): {st['changed']} sor szövege, {st['say']} kiejtés módosult", "success")
    return dict(st, source=src_name)


@app.post("/api/import")
async def api_import(request: Request):
    if not S.job:
        raise HTTPException(400, "Előbb futtass le egy szinkront (vagy legalább a fordítást).")
    text = ((await request.json()).get("text") or "").strip()
    if text and text != S.exported.strip():
        return _apply_import(text, "a beillesztett szöveg")
    path = script.export_path(S.job)                    # a kesz mappában (pl. kívülről) javított fájl
    if not os.path.exists(path):
        raise HTTPException(400, "Nincs mit importálni: illeszd be a javított szöveget, vagy válassz fájlt.")
    with open(path, encoding="utf-8-sig") as f:
        return _apply_import(f.read(), os.path.basename(path))


@app.post("/api/import-file")
async def api_import_file(file: UploadFile = File(...)):
    if not S.job:
        raise HTTPException(400, "Előbb futtass le egy szinkront (vagy legalább a fordítást).")
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    return _apply_import(text, os.path.basename(file.filename or "fájl"))


# --- eredmény, fájlok ------------------------------------------------------------------------

def _allowed(path):
    p = os.path.normcase(os.path.abspath(path))
    roots = [OUT_DIR, WORK_DIR] + ([os.path.dirname(S.job.src)] if S.job else [])
    return any(p.startswith(os.path.normcase(os.path.abspath(r)) + os.sep) for r in roots)


@app.get("/api/files")
def api_files():
    if not S.job:
        return {"files": [], "preview": None}
    res = (read_json(S.job.state_path) or {}).get("result") or {}
    stem = safe_stem(S.job.src)
    cands = [(res.get("output"), "video" if res.get("has_video") else "audio",
              "Szinkronizált videó" if res.get("has_video") else "Szinkronizált hang"),
             (res.get("hu_audio"), "audio", "Magyar hangsáv"),
             (res.get("srt"), "srt", "Magyar felirat"),
             (os.path.join(OUT_DIR, stem + "_eredeti.srt"), "srt", "Eredeti felirat"),
             (script.export_path(S.job), "txt", "Szinkronszöveg (export)")]
    files = []
    for path, kind, label in cands:
        if path and os.path.exists(path):
            files.append({"path": path, "name": os.path.basename(path), "kind": kind, "label": label,
                          "size": os.path.getsize(path), "mtime": os.path.getmtime(path)})
    preview = None
    if res.get("output") and os.path.exists(res["output"]):
        preview = {"path": res["output"], "video": bool(res.get("has_video")),
                   "mtime": os.path.getmtime(res["output"])}
    return {"files": files, "preview": preview}


@app.get("/api/media")
def api_media(path: str, download: int = 0):
    if not os.path.isfile(path) or not _allowed(path):
        raise HTTPException(404, "nincs ilyen fájl")
    kw = {"filename": os.path.basename(path)} if download else {}
    return FileResponse(path, **kw)


@app.get("/api/orig-audio")
def api_orig_audio():
    """Az eredeti hang kis méretű változata a lejátszó "Eredeti" sávjához (egyszer készül el)."""
    if not S.job or not os.path.exists(S.job.p("audio.wav")):
        raise HTTPException(404, "nincs eredeti hang")
    out = S.job.p("eredeti_elonezet.m4a")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(S.job.p("audio.wav")):
        media.encode_audio(S.job.p("audio.wav"), out)
    return FileResponse(out, media_type="audio/mp4")


def _open(path):
    os.makedirs(path, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)   # noqa: S606


@app.post("/api/open-folder")
async def api_open_folder(request: Request):
    which = (await request.json()).get("which")
    _open(OUT_DIR if which == "out" else WORK_DIR)
    return {"ok": True}


def _dir_size(path):
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@app.get("/api/work-size")
def api_work_size():
    n = len([d for d in os.listdir(WORK_DIR)]) if os.path.isdir(WORK_DIR) else 0
    return {"jobs": n, "bytes": _dir_size(WORK_DIR) if n else 0}


def _browser_open(url):
    """Edge-alkalmazásablakban nyílik (mint egy önálló program), különben a böngészőben."""
    import webbrowser

    for exe in (shutil.which("msedge"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")):
        if exe and os.path.exists(exe):
            try:
                prof = os.path.join(os.path.expanduser("~"), ".rkdub", "edge")
                subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1500,1000",
                                  "--no-first-run", "--no-default-browser-check"], creationflags=NO_WINDOW)
                return
            except OSError:
                pass
    webbrowser.open(url)


def _port_in_use(port):
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch(port=7870, open_browser=True):
    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if _port_in_use(port):              # már fut (pl. kétszer kattintottak az ikonra): csak az ablakot nyitjuk meg
        print(f"Az rkdub már fut: {url}", flush=True)
        if open_browser:
            _browser_open(url)
        return
    if open_browser:
        threading.Timer(1.5, _browser_open, args=(url,)).start()
    print(f"rkdub fut: {url}  (bezárás: ez az ablak)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@app.post("/api/clear-work")
def api_clear_work():
    """A köztes fájlok törlése (munkak). A kész fájlok (kesz) megmaradnak."""
    if S.proc and not S.proc.finished:
        raise HTTPException(409, "Szinkron közben nem törölhető.")
    freed = _dir_size(WORK_DIR) if os.path.isdir(WORK_DIR) else 0
    for d in os.listdir(WORK_DIR) if os.path.isdir(WORK_DIR) else []:
        shutil.rmtree(os.path.join(WORK_DIR, d), ignore_errors=True)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    S.job, S.opts = None, None
    with S.lock:
        S.status, S.message, S.result = "idle", "", None
    S.log(f"Munkamappák törölve ({freed / 2**30:.1f} GB felszabadult)", "success")
    return {"ok": True, "freed": freed}
