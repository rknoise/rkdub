# -*- coding: utf-8 -*-
"""Beszédfelismerés (faster-whisper, GPU) szóidőkkel, és mondatokra (szinkronsorokra) bontás."""

import os
import sys

from .util import free_gpu

SENT_END = (".", "!", "?", "…")
SOFT_END = (",", ";", ":", "—", "–")


def add_nvidia_dll_dirs():
    """A pip-es nvidia-* csomagok DLL-jei (cuBLAS, cuDNN) a ctranslate2-nek."""
    if os.name != "nt":
        return
    for sp in {p for p in sys.path if "site-packages" in p}:
        nv = os.path.join(sp, "nvidia")
        if not os.path.isdir(nv):
            continue
        for root, _d, _f in os.walk(nv):
            if os.path.basename(root).lower() == "bin":
                try:
                    os.add_dll_directory(root)
                except OSError:
                    pass
                os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")


def transcribe(vocals_16k, model_name="large-v3-turbo", language=None, log=print, progress=None):
    """Szavak listája: [{"start", "end", "word", "p"}] és a felismert nyelv kódja."""
    add_nvidia_dll_dirs()
    import torch
    from faster_whisper import WhisperModel

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"Beszédfelismerő betöltése: {model_name} ({'GPU' if device == 'cuda' else 'CPU'})")
    model = WhisperModel(model_name, device=device, compute_type="float16" if device == "cuda" else "int8")
    total = len(vocals_16k) / 16000
    segments, info = model.transcribe(
        vocals_16k,
        language=language or None,
        task="transcribe",
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
        condition_on_previous_text=False,
        temperature=[0.0, 0.2, 0.4],
        no_speech_threshold=0.6,
    )
    words = []
    for seg in segments:
        if progress and total:
            progress(min(1.0, seg.end / total))
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                # "raw": a Whisper szódarabja a vezető szóközzel (pl. " 6" + ".30" -> "6.30")
                words.append({"start": round(w.start, 3), "end": round(w.end, 3), "word": text,
                              "raw": w.word, "p": round(float(w.probability), 3)})
    lang = info.language
    log(f"Felismerve: {len(words)} szó, nyelv: {lang} ({info.language_probability:.0%})")
    del model
    free_gpu()
    return words, lang


def join_words(ws):
    return " ".join("".join(w.get("raw", " " + w["word"]) for w in ws).split())


def split_lines(words, max_len=12.0, hard_max=18.0, pause_break=0.7):
    """Szavakból szinkronsorok: mondatvégnél vagy hosszabb szünetnél tör, a túl hosszú
    mondatot vesszőnél vagy rövid szünetnél, végső esetben szóhatáron."""
    lines, cur = [], []

    def flush():
        if cur:
            lines.append({"start": cur[0]["start"], "end": cur[-1]["end"], "en": join_words(cur),
                          "conf": round(sum(w["p"] for w in cur) / len(cur), 3)})
            cur.clear()

    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 99.0
        dur = w["end"] - cur[0]["start"]
        txt = w["word"]
        if gap >= pause_break:
            flush()
        elif txt.endswith(SENT_END) and (dur >= 1.2 or gap >= 0.25):
            flush()
        elif dur >= max_len and (txt.endswith(SOFT_END) or gap >= 0.25):
            flush()
        elif dur >= hard_max:
            flush()
    flush()
    for i, ln in enumerate(lines):
        ln["id"] = i
    return lines


def merge_short(lines, min_len=1.0, max_gap=0.4, max_len=10.0):
    """A nagyon rövid sorok (pl. "Yeah.") az ugyanattól a beszélőtől jövő szomszédjukhoz kerülnek,
    hogy a hangmodell ne 1 mp-es töredékekből dolgozzon."""
    out = []
    for ln in lines:
        prev = out[-1] if out else None
        short = (ln["end"] - ln["start"]) < min_len or (prev and (prev["end"] - prev["start"]) < min_len)
        if (prev and short and ln["start"] - prev["end"] <= max_gap
                and ln.get("speaker") == prev.get("speaker") and ln["end"] - prev["start"] <= max_len):
            prev["end"] = ln["end"]
            prev["en"] = prev["en"] + " " + ln["en"]
            prev["conf"] = round((prev["conf"] + ln["conf"]) / 2, 3)
            continue
        out.append(dict(ln))
    for i, ln in enumerate(out):
        ln["id"] = i
    return out
