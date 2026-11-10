# -*- coding: utf-8 -*-
"""Közös segédek: mappák, ffmpeg, JSON, GPU-memória, naplózás."""

import gc
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_DIR = os.path.join(ROOT, "munkak")      # lépésenkénti köztes fájlok (folytatható, újragenerálható)
OUT_DIR = os.path.join(ROOT, "kesz")         # a kész, szinkronizált fájlok
HOME_DIR = os.path.join(os.path.expanduser("~"), ".rkdub")
SETTINGS_PATH = os.path.join(HOME_DIR, "settings.json")
RKSUBBY_DEEPL_KEY = os.path.join(os.path.expanduser("~"), ".rksubby", "deepl_key.txt")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".ts", ".mpg", ".mpeg"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


MODELS_OK = os.path.join(ROOT, ".models", "letoltve.txt")   # a telepítő írja, ha minden modell megvan


def use_offline_models():
    """Ha a telepítő már letöltött minden modellt, a Hugging Face-t nem kérdezzük le minden indításnál
    (gyorsabb, és internet nélkül is működik). A huggingface_hub importja ELŐTT kell hívni."""
    if os.path.exists(MODELS_OK) and os.environ.get("RKDUB_ONLINE") != "1":
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        return True
    return False


def is_offline_error(e):
    s = f"{type(e).__name__} {e}".lower()
    return any(k in s for k in ("offline", "localentrynotfound", "local_files_only", "cannot find the requested files"))


def ffmpeg_exe():
    """A PATH-on lévő ffmpeg, különben az imageio-ffmpeg csomagé."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def ensure_ffmpeg_on_path():
    """Az audio-separator (és a pydub) a PATH-on keresi az "ffmpeg" parancsot, és nélküle leáll ("FFmpeg is not
    installed"). Ha a gépen nincs ffmpeg, az imageio-ffmpeg csomagét ffmpeg(.exe) néven a .tools\\ffmpeg mappába
    másoljuk, és azt tesszük a PATH elejére. Az audio_separator importálása ELŐTT kell hívni."""
    if shutil.which("ffmpeg"):
        return
    import imageio_ffmpeg

    src = imageio_ffmpeg.get_ffmpeg_exe()
    folder = os.path.join(ROOT, ".tools", "ffmpeg")
    dst = os.path.join(folder, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not (os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src)):
        os.makedirs(folder, exist_ok=True)
        tmp = dst + ".tmp"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        if os.name != "nt":
            os.chmod(dst, 0o755)
    os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def run_ffmpeg(args, what="ffmpeg"):
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"{what} nem sikerült: {p.stderr.strip()[-800:]}")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_settings():
    return read_json(SETTINGS_PATH, {}) or {}


def save_settings(data):
    os.makedirs(HOME_DIR, exist_ok=True)
    write_json(SETTINGS_PATH, data)


def apply_cpu_override():
    """RKDUB_CPU=1: processzoron fut akkor is, ha van NVIDIA-kártya (a processzoros mód kipróbálásához).
    A torch importja ELŐTT kell hívni; a feladatot futtató gyerekfolyamat örökli."""
    if os.environ.get("RKDUB_CPU") == "1":
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"      # az üres érték Windowson törölné a változót


_cpu_mode = None


def cpu_mode():
    """Nincs használható NVIDIA-kártya (nincs, vagy a GTX 10xx-nél régebbi, amit a PyTorch már nem támogat; vagy
    RKDUB_CPU=1): a szinkron processzoron, könnyített beállításokkal fut. Az nvidia-smi alapján dönt, a torch
    betöltése nélkül (a felületnek is gyorsan kell)."""
    global _cpu_mode
    if _cpu_mode is None:
        _cpu_mode = True
        exe = shutil.which("nvidia-smi")
        if exe and os.environ.get("RKDUB_CPU") != "1":
            run = lambda *a: subprocess.run([exe, *a], capture_output=True, text=True, timeout=10,  # noqa: E731
                                            creationflags=NO_WINDOW).stdout.strip()
            try:
                try:
                    _cpu_mode = float(run("--query-gpu=compute_cap", "--format=csv,noheader").splitlines()[0]) < 6.1
                except (ValueError, IndexError):      # a régi meghajtó nem ismeri a mezőt
                    _cpu_mode = "GPU" not in run("-L")
            except Exception:
                pass
    return _cpu_mode


def ct2_compute_type(device):
    """A Whisper (CTranslate2) számítási típusa. GPU-n float16, ha a kártya hatékonyan tudja (RTX 20-astól);
    a GTX 10xx-en a CTranslate2 a float16-ot hibával elutasítja, ott int8 (azt a kártya gyorsan számolja)."""
    if device != "cuda":
        return "int8"
    try:
        import ctranslate2

        types = ctranslate2.get_supported_compute_types("cuda")
    except Exception:
        return "float16"
    return next((t for t in ("float16", "int8_float32", "int8") if t in types), "float32")


def free_gpu():
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def fmt_time(t):
    t = max(0.0, float(t))
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def short_err(e):
    s = str(e).strip().splitlines()
    return f"{type(e).__name__}: {s[-1] if s else ''}"[:400]


def log_to_console(msg):
    try:
        print(msg, flush=True)
    except Exception:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))


# --- platformfüggő segédek (Windows / Linux) ----------------------------------------------

def open_folder(path):
    """Mappa megnyitása a fájlkezelőben."""
    os.makedirs(path, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(path)   # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _app_browsers():
    """Chromium-alapú böngészők, amelyek "--app" módban önálló ablakként nyitják meg a felületet."""
    if os.name == "nt":
        return [shutil.which("msedge"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
    return [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                                      "microsoft-edge", "microsoft-edge-stable", "brave-browser")]


def browser_open(url, size="1500,1000"):
    """Alkalmazásablakban nyílik (mint egy önálló program), különben az alapértelmezett böngészőben."""
    import webbrowser

    for exe in _app_browsers():
        if exe and os.path.exists(exe):
            try:
                prof = os.path.join(HOME_DIR, "edge" if os.name == "nt" else "browser")
                subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}", f"--window-size={size}",
                                  "--no-first-run", "--no-default-browser-check"], creationflags=NO_WINDOW,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
    webbrowser.open(url)


def pick_media_file(exts):
    """Fájlválasztó ablak; a kiválasztott út, vagy "" (mégse). Linuxon a zenity / kdialog natív ablaka, ha van,
    különben (és Windowson) a tkinter-é. A tkinter külön folyamatban fut, hogy a szerver szálait ne zavarja."""
    title = "Videó vagy hangfájl kiválasztása"
    kw = dict(capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
    if os.name != "nt" and sys.platform != "darwin":
        if shutil.which("zenity"):
            p = subprocess.run(["zenity", "--file-selection", f"--title={title}",
                                f"--file-filter=Videó és hang | {' '.join(exts)}", "--file-filter=Minden fájl | *"], **kw)
            return p.stdout.strip()
        if shutil.which("kdialog"):
            p = subprocess.run(["kdialog", "--title", title, "--getopenfilename", os.path.expanduser("~"),
                                f"{' '.join(exts)}|Videó és hang"], **kw)
            return p.stdout.strip()
    code = ("import tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw();r.attributes('-topmost',True)\n"
            f"p=filedialog.askopenfilename(title={title!r},filetypes=[('Videó és hang',{' '.join(exts)!r}),"
            "('Minden fájl','*')])\nprint(p or '')")
    return subprocess.run([sys.executable, "-c", code], creationflags=NO_WINDOW, **kw).stdout.strip()
