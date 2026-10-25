# -*- coding: utf-8 -*-
"""rkdub - helyi magyar szinkron. Indítás: grafikus felület (alapértelmezés), vagy
parancssorból: rkdub.py --cli video.mp4 [--speakers 2] [--deepl] [--model qwen3.8:27b]"""

import argparse
import os
import sys
import warnings

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for _s in (sys.stdout, sys.stderr):     # a konzolra írt magyar szöveg ne okozzon hibát
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="rkdub - helyi magyar szinkron")
    ap.add_argument("--cli", metavar="FÁJL", help="parancssori futtatás felület nélkül")
    ap.add_argument("--speakers", type=int, default=0, help="beszélők száma (0 = automatikus)")
    ap.add_argument("--deepl", action="store_true", help="fordítás DeepL-lel a helyi AI helyett")
    ap.add_argument("--model", default="", help="Ollama-modell a fordításhoz")
    ap.add_argument("--ref", choices=["hu", "line", "speaker"], default="hu",
                    help="hangminta: hu = magyarosított (akcentus nélkül), line = soronként, speaker = beszélőnként")
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--classic", action="store_true", help="a régi (Gradio-s) felület")
    a = ap.parse_args()
    if a.cli:
        from dub.pipeline import DubJob
        from dub.worker import JobProcess

        opts = {"num_speakers": a.speakers, "translator": "deepl" if a.deepl else "ollama",
                "ollama_model": a.model, "ref_mode": a.ref}
        proc = JobProcess(os.path.abspath(a.cli), opts, DubJob(a.cli, opts).dir)
        code = 0
        while not proc.finished:
            for ev in proc.events(0.5):
                if ev[0] == "log":
                    print(ev[1], flush=True)
                elif ev[0] == "done":
                    print("Eredmény:", ev[1].get("output"), flush=True)
                elif ev[0] == "err":
                    print("HIBA:", ev[1], flush=True)
                    code = 1
        sys.exit(code)
    if a.classic:
        from dub.ui import launch
    else:
        from dub.webapp import launch

    launch(port=a.port, open_browser=not a.no_browser)


if __name__ == "__main__":
    main()
