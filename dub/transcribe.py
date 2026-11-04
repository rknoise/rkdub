# -*- coding: utf-8 -*-
"""Beszédfelismerés (faster-whisper, GPU) szóidőkkel, és mondatokra (szinkronsorokra) bontás."""

import os
import sys

from .util import ct2_compute_type, free_gpu

SENT_END = (".", "!", "?", "…")
SOFT_END = (",", ";", ":", "—", "–")
# ponttal végződő rövidítések, amelyek után nem ér véget a mondat ("Mr. Sterling")
ABBREV = {"mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "jr.", "sr.", "vs.", "mt.", "gen.", "col.", "lt.", "sgt.",
          "capt.", "mme.", "mlle.", "hr.", "fr.", "sra.", "srta.", "dott.", "sig."}

# Írásjeles "súgó" a Whisper minden ablakához (hotwords). Enélkül a Whisper néha hosszú szakaszokon csupa
# kisbetűs, írásjel nélküli szöveget ír, és akkor a sorok nem a mondatoknál (így a beszélőváltásoknál sem)
# törnek: egy sorba több szereplő szövege kerül, és mind egy hangon szól. Mérve (2026-11-04, 170 mp-es dráma):
# 25 vegyes sor helyett 58 tiszta mondat. A súgós futás viszont néha kihagy egy szakaszt (ott 30 szót), ezért
# egy súgó nélküli futás is van, és ahol az talál beszédet, a súgós nem, azt a részt külön (súgóval) újra
# átírja, vagy ha az sem sikerül, a súgó nélküli futás szavait veszi.
PUNCT_HINTS = {
    "en": "Hello, how are you? I'm fine, thanks. Really? Yes! Let's go.",
    "fr": "Bonjour, comment ça va ? Très bien, merci. Vraiment ? Oui ! Allons-y.",
    "de": "Hallo, wie geht's? Gut, danke. Wirklich? Ja! Los geht's.",
    "es": "Hola, ¿cómo estás? Bien, gracias. ¿En serio? ¡Sí! Vamos.",
    "it": "Ciao, come stai? Bene, grazie. Davvero? Sì! Andiamo.",
    "pt": "Olá, como vai? Bem, obrigado. Sério? Sim! Vamos.",
    "nl": "Hallo, hoe gaat het? Goed, dank je. Echt? Ja! Kom op.",
    "pl": "Cześć, jak się masz? Dobrze, dziękuję. Naprawdę? Tak! Chodźmy.",
    "hu": "Szia, hogy vagy? Jól, köszönöm. Tényleg? Igen! Gyerünk.",
    "ru": "Привет, как дела? Хорошо, спасибо. Правда? Да! Пошли.",
}
VAD = {"min_silence_duration_ms": 300, "speech_pad_ms": 200}


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
    from faster_whisper.vad import VadOptions

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"Beszédfelismerő betöltése: {model_name} ({'GPU' if device == 'cuda' else 'CPU'})")
    model = WhisperModel(model_name, device=device, compute_type=ct2_compute_type(device))
    total = len(vocals_16k) / 16000
    prob = 1.0
    if not language:
        language, prob, _ = model.detect_language(vocals_16k, vad_filter=True, vad_parameters=VadOptions(**VAD))
    hint = PUNCT_HINTS.get(language)

    def run(audio, hotwords, offset=0.0, part=None):
        segments, _info = model.transcribe(
            audio, language=language, task="transcribe", beam_size=5, word_timestamps=True, vad_filter=True,
            vad_parameters=VAD, condition_on_previous_text=False, temperature=[0.0, 0.2, 0.4],
            no_speech_threshold=0.6, hotwords=hotwords)
        out = []
        for seg in segments:
            if progress and total and part:
                progress(min(1.0, part[0] + (part[1] - part[0]) * seg.end / total))
            for w in seg.words or []:
                text = w.word.strip()
                if text:
                    # "raw": a Whisper szódarabja a vezető szóközzel (pl. " 6" + ".30" -> "6.30")
                    out.append({"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": text,
                                "raw": w.word, "p": round(float(w.probability), 3)})
        return out

    words = run(vocals_16k, hint, part=(0.0, 0.55 if hint else 1.0))
    filled = 0
    if hint:
        plain = run(vocals_16k, None, part=(0.55, 0.95))
        words, filled = fill_gaps(words, plain, lambda a, b: run(vocals_16k[int(a * 16000): int(b * 16000)], hint,
                                                                 offset=a))
    log(f"Felismerve: {len(words)} szó, nyelv: {language} ({prob:.0%})"
        + (f"; {filled} kihagyott szót pótoltam" if filled else ""))
    model = None                               # (a run() is erre hivatkozik) a videómemória felszabadítása
    free_gpu()
    return words, language


def _norm_word(w):
    return "".join(c for c in w.lower() if c.isalnum())


def fill_gaps(main, other, retranscribe, tol=0.15, max_word=1.0, max_gap=1.0):
    """A fő (súgós) futás szavai, kiegészítve ott, ahol a másik futás beszédet talált, a fő nem.
    retranscribe(a, b): az [a, b] mp-es szakasz újra átírva (a fő futás beállításaival). -> (szavak, pótolt)"""
    import bisect

    # a fő futás lefedte időszakaszok; a gyanúsan hosszú szó (a Whisper az átugrott részt ráhúzza) csak a
    # végétől számít
    spans = [(max(w["start"], w["end"] - max_word), w["end"]) for w in main]
    starts = [a for a, _ in spans]

    def covered(w):
        i = bisect.bisect_right(starts, w["end"] + tol)
        return any(spans[j][1] > w["start"] - tol for j in range(max(0, i - 4), i))

    runs, cur = [], []
    for w in other:
        if covered(w):
            if cur:
                runs.append(cur)
            cur = []
        elif cur and w["start"] - cur[-1]["end"] > max_gap:
            runs.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        runs.append(cur)
    runs = [r for r in runs if len(r) > 1 or (r[0]["p"] >= 0.5 and _norm_word(r[0]["word"]) not in {
        _norm_word(x["word"]) for x in main if abs(x["start"] - r[0]["start"]) < 1.5})]
    # egyetlen szó csak akkor, ha biztos, és nem egy közeli szó eltérő időzítéssel (fent); a fő futás 1-2
    # magányos szava két kihagyott darab között (gyakran rossz helyre időzítve) az újra átírt szakaszba kerül
    merged = []
    for r in runs:
        if merged:
            between = [x for x in main if merged[-1][-1]["end"] - tol < x["start"] and x["end"] < r[0]["start"] + tol]
            if len(between) <= 2 and r[0]["start"] - merged[-1][-1]["end"] < 2.0:
                merged[-1] = merged[-1] + r
                continue
        merged.append(r)
    out, filled = list(main), 0
    for run in merged:
        lo = max([x["end"] for x in out if x["end"] <= run[0]["start"] + tol and not x.get("fill")
                  and not run[0]["start"] - tol < x["start"] < run[-1]["end"] + tol] or [0.0])
        hi = min([x["start"] for x in out if x["start"] >= run[-1]["end"] - tol and not x.get("fill")] or
                 [run[-1]["end"] + 0.5])
        a, b = max(lo, run[0]["start"] - 0.25), min(hi, run[-1]["end"] + 0.25)
        try:
            new = [w for w in retranscribe(a, b) if a <= (w["start"] + w["end"]) / 2 <= b]
        except Exception:
            new = []
        inside = lambda w: a < (w["start"] + w["end"]) / 2 < b      # noqa: E731
        if len(new) >= 0.6 * len(run):
            out = [w for w in out if not inside(w)]           # a szakasz újra átírva, írásjelekkel
            out += [dict(w, fill="hint") for w in new]
        else:                                  # az újra átírás sem találta meg: a súgó nélküli szavak
            keep = {_norm_word(w["word"]) for w in run}
            out = [w for w in out if not (inside(w) and _norm_word(w["word"]) in keep)]
            out += [dict(w, fill="plain") for w in run]
            new = run
        filled += len(new)
        out.sort(key=lambda w: w["start"])
    return out, filled


def join_words(ws):
    return " ".join("".join(w.get("raw", " " + w["word"]) for w in ws).split())


def sentence_end(txt):
    return txt.endswith(SENT_END) and txt.lower() not in ABBREV


def _continues(w):
    """A Whisper-szódarab az előző szó folytatása (nincs előtte szóköz, pl. "'est", "-être", ".30")."""
    raw = w.get("raw")
    return bool(raw) and not raw[0].isspace()


def split_lines(words, max_len=12.0, hard_max=18.0, pause_break=0.7):
    """Szavakból szinkronsorok: mondatvégnél vagy hosszabb szünetnél tör, a túl hosszú
    mondatot vesszőnél vagy rövid szünetnél, végső esetben szóhatáron. A pótolt (a súgós futásból kimaradt)
    írásjel nélküli szakasz külön sorba kerül."""
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
        if nxt is not None and _continues(nxt):
            continue                           # szó közepén nem törünk ("C" + "'est", "Peut" + "-être")
        if gap >= pause_break:
            flush()
        elif nxt is not None and (w.get("fill") == "plain") != (nxt.get("fill") == "plain"):
            flush()
        elif sentence_end(txt) and (dur >= 1.2 or gap >= 0.25):
            flush()
        elif dur >= max_len and (txt.endswith(SOFT_END) or gap >= 0.25):
            flush()
        elif dur >= hard_max:
            flush()
    flush()
    for i, ln in enumerate(lines):
        ln["id"] = i
    return lines


def wrap_two(text, width=42):
    """A hosszú feliratsor két sorba, a közepéhez legközelebbi szóközön."""
    if len(text) <= width:
        return text
    cuts = [i for i, c in enumerate(text) if c == " "]
    if not cuts:
        return text
    i = min(cuts, key=lambda k: abs(k - len(text) / 2))
    return text[:i] + "\n" + text[i + 1:]


def subtitle_lines(words, max_chars=84, max_dur=7.0, pause_break=0.6):
    """Feliratsorok a "csak felirat" módhoz (pl. dalszöveg): rövidebbek, mint a szinkronsorok; szünetnél,
    mondatvégnél, 2x42 karakternél vagy 7 másodpercnél törnek; a 42 karakternél hosszabb sor két sorba kerül."""
    lines, cur = [], []

    def flush():
        if cur:
            lines.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": wrap_two(join_words(cur))})
            cur.clear()

    for i, w in enumerate(words):
        if cur and not _continues(w) and (len(join_words(cur + [w])) > max_chars
                                          or w["end"] - cur[0]["start"] > max_dur):
            flush()
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if not nxt or (not _continues(nxt) and (nxt["start"] - w["end"] >= pause_break or sentence_end(w["word"]))):
            flush()
    # a rövid sor kicsit tovább látszik, de nem lóg bele a következőbe
    for a, b in zip(lines, lines[1:]):
        a["end"] = max(a["end"], min(a["start"] + 1.2, b["start"] - 0.05))
    if lines:
        lines[-1]["end"] = max(lines[-1]["end"], lines[-1]["start"] + 1.2)
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
