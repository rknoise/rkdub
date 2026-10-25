# -*- coding: utf-8 -*-
"""A szinkron lépései egy munkamappában. Minden lépés eredménye megmarad, így ugyanazt a fájlt
újra elindítva onnan folytatja, ahol abbamaradt, és egy szerkesztett sor után csak az a sor
készül el újra."""

import hashlib
import os
import time

import numpy as np

from . import media
from .util import OUT_DIR, WORK_DIR, fmt_time, free_gpu, read_json, short_err, write_json

DEFAULT_OPTIONS = {
    "translator": "ollama",          # ollama | deepl
    "ollama_model": "",
    "deepl_key": "",
    "formality": "auto",             # auto | informal | formal
    "notes": "",                     # plusz utasítás a fordítónak (nevek, szakszavak, stílus)
    "source_lang": "",               # "" = automatikus felismerés
    "num_speakers": 0,               # 0 = automatikus
    "speaker_genders": [],           # a szereplők neme, ha ismert: pl. ["f", "f", "m"] = 2 nő, 1 férfi
    "ref_mode": "hu",                # hu: magyarosított minta (akcentus nélkül) | line: soronként az eredeti
                                     # hangsúllyal | speaker: beszélőnként egységes (angol minta)
    "keep_background": True,         # zene és zajok megtartása
    "keep_nonspeech": True,          # nevetés, kiáltás stb. az eredeti hangból
    "keep_original_track": True,     # a videóban második hangsávként az eredeti
    "subtitles": False,              # magyar felirat beágyazása a videóba (a külön .srt mindig elkészül;
                                     # MP4-ben a lejátszók alapból bekapcsolva mutatják)
    "qc": True,                      # minőségellenőrzés visszahallgatással
    "review": True,                  # a helyi AI még egyszer átnézi a fordítást
    "stop_after_translate": False,   # a fordítás után megáll (átnézés, export/import), a hang csak utána készül
    "asr_model": "large-v3-turbo",   # beszédfelismerő: large-v3-turbo (gyors) | large-v3 (pontosabb)
    "tempo": 1.0,                    # beszédtempó a magyar hangnál (1.0 = a beszélő természetes tempója)
}

STAGES = [("prepare", 2), ("separate", 12), ("transcribe", 8), ("speakers", 3), ("translate", 15),
          ("synthesize", 55), ("mix", 5)]
STAGE_NAMES = {"prepare": "Előkészítés", "separate": "Beszéd és háttér szétválasztása",
               "transcribe": "Beszédfelismerés", "speakers": "Beszélők azonosítása",
               "translate": "Fordítás", "synthesize": "Magyar hang készítése", "mix": "Keverés és mentés"}
LINE_REF_MIN = 4.0       # ennél rövidebb sornál a beszélő összesített hangmintája a referencia
SPK_REF_TARGET = 8.0     # a beszélőnkénti hangminta hossza (mp)
REF_MAX = 11.0
REF_HEAD_PAD = 0.15      # a hangminta eleje és vége ennyivel bővül (a szomszéd mondatig)
REF_TAIL_PAD = 0.4
QC_FLAG = 0.35           # e fölött jelzi a felület, hogy a sort érdemes meghallgatni


def file_signature(path):
    st = os.stat(path)
    h = hashlib.sha1(f"{os.path.basename(path)}|{st.st_size}".encode("utf-8"))
    with open(path, "rb") as f:
        h.update(f.read(4 << 20))
    return h.hexdigest()[:10]


def _sig(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def safe_stem(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    keep = "".join(c if c.isalnum() or c in " -_.()" else "_" for c in stem).strip(" .")
    return (keep or "video")[:80]


class Cancelled(Exception):
    pass


class DubJob:
    def __init__(self, src, options=None, log=print, progress=None, cancel=None, job_dir=None):
        self.src = os.path.abspath(src)
        self.opt = dict(DEFAULT_OPTIONS, **(options or {}))
        self._log = log
        self._progress = progress
        self.cancel = cancel                       # threading.Event (megszakítás)
        if job_dir:
            self.dir = job_dir
        else:
            self.dir = os.path.join(WORK_DIR, f"{safe_stem(src)}-{file_signature(src)}")
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "job.json")
        self.state = read_json(self.state_path, {}) or {}
        self.state.setdefault("src", self.src)
        self.state.setdefault("sigs", {})
        self.state["src"] = self.src
        self._stage_i = 0

    # --- segédek --------------------------------------------------------------
    def p(self, name):
        return os.path.join(self.dir, name)

    def log(self, msg):
        self._log(msg)

    def check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def progress(self, frac, desc=""):
        done = sum(w for _, w in STAGES[: self._stage_i])
        w = STAGES[self._stage_i][1] if self._stage_i < len(STAGES) else 0
        total = sum(w for _, w in STAGES)
        if self._progress:
            self._progress((done + w * max(0.0, min(1.0, frac))) / total, desc)

    def save_state(self):
        write_json(self.state_path, self.state)

    def lines(self):
        return read_json(self.p("lines.json"), []) or []

    def save_lines(self, lines):
        write_json(self.p("lines.json"), lines)

    def _fresh(self, stage, sig, *files):
        return self.state["sigs"].get(stage) == sig and all(os.path.exists(self.p(f)) for f in files)

    def _done(self, stage, sig):
        self.state["sigs"][stage] = sig
        # ami utána jön, annak újra kell futnia
        names = [s for s, _ in STAGES]
        for later in names[names.index(stage) + 1:]:
            if later != "synthesize":          # a hangfájlok soronként, saját kulccsal frissülnek
                self.state["sigs"].pop(later, None)
        self.save_state()

    # --- futtatás -------------------------------------------------------------
    def run(self, until=None):
        t_all = time.time()
        if until is None and self.opt.get("stop_after_translate"):
            until = "translate"                # átnézés / export-import a hang készítése előtt
        steps = [self.prepare, self.separate, self.transcribe, self.speakers, self.translate,
                 self.synthesize, self.mix]
        for i, (fn, (name, _w)) in enumerate(zip(steps, STAGES)):
            self._stage_i = i
            self.check_cancel()
            self.progress(0.0, STAGE_NAMES[name])
            t = time.time()
            fn()
            self.log(f"[{STAGE_NAMES[name]}] kész ({time.time() - t:.1f} mp)")
            if until == name:
                if self._progress:
                    self._progress(1.0, STAGE_NAMES[name] + " kész")
                self.log(f"Megállítva a(z) „{STAGE_NAMES[name]}” lépés után ({fmt_time(time.time() - t_all)})")
                return {"stopped": name, "lines": len(self.lines())}
        self._stage_i = len(STAGES)
        if self._progress:
            self._progress(1.0, "Kész")
        self.log(f"Összesen: {fmt_time(time.time() - t_all)}")
        return self.state.get("result", {})

    # 1. hang kibontása
    def prepare(self):
        info = media.probe(self.src)
        if not info["has_audio"]:
            raise RuntimeError("a fájlban nincs hang")
        self.state["info"] = info
        sig = _sig(file_signature(self.src))
        if self._fresh("prepare", sig, "audio.wav"):
            return
        self.log(f"Bemenet: {os.path.basename(self.src)} ({fmt_time(info['duration'])}, "
                 f"{'videó' if info['has_video'] else 'csak hang'})")
        media.extract_audio(self.src, self.p("audio.wav"))
        self._done("prepare", sig)

    # 2. beszéd / háttér
    def separate(self):
        sig = _sig(self.state["sigs"].get("prepare"), "bsr1297")
        if self._fresh("separate", sig, "vocals.wav", "background.wav"):
            return
        from .separate import separate

        separate(self.p("audio.wav"), self.dir, log=self.log)
        self._done("separate", sig)

    # 3. beszédfelismerés + sorokra bontás
    def transcribe(self):
        asr_model = self.opt.get("asr_model") or "large-v3-turbo"
        sig = _sig(self.state["sigs"].get("separate"), self.opt["source_lang"],
                   "turbo-v1" if asr_model == "large-v3-turbo" else asr_model)
        if self._fresh("transcribe", sig, "words.json"):
            return
        from .transcribe import transcribe

        y16, _ = media.read_audio(self.p("vocals.wav"), sr=16000, mono=True)
        words, lang = transcribe(y16, model_name=asr_model, language=self.opt["source_lang"] or None, log=self.log,
                                 progress=lambda f: self.progress(f, "Beszédfelismerés"))
        if not words:
            raise RuntimeError("nem találtam beszédet a fájlban")
        write_json(self.p("words.json"), {"lang": lang, "words": words})
        self._done("transcribe", sig)

    # 4. beszélők
    def speakers(self):
        sig = _sig(self.state["sigs"].get("transcribe"), self.opt["num_speakers"],
                   ",".join(sorted(self.opt.get("speaker_genders") or [])), "v2-ecapa-pitch")
        if self._fresh("speakers", sig, "lines.json"):
            return
        from .speakers import assign_speakers
        from .transcribe import merge_short, split_lines

        data = read_json(self.p("words.json"))
        lines = split_lines(data["words"])
        y16, _ = media.read_audio(self.p("vocals.wav"), sr=16000, mono=True)
        lines = assign_speakers(lines, y16, int(self.opt["num_speakers"] or 0), log=self.log,
                                genders=self.opt.get("speaker_genders") or [])
        lines = merge_short(lines)
        self.log(f"{len(lines)} szinkronsor")
        self.save_lines(lines)
        self._done("speakers", sig)

    # 5. fordítás
    def translate(self):
        o = self.opt
        model = o["ollama_model"] if o["translator"] == "ollama" else "deepl"
        sig = _sig(self.state["sigs"].get("speakers"), o["translator"], model, o["formality"], o["notes"],
                   o.get("review", True), "v3-hustyle-say")
        lines = self.lines()
        if self._fresh("translate", sig, "lines.json") and all(ln.get("hu") for ln in lines):
            return
        if self.state["sigs"].get("translate") != sig:
            for ln in lines:                 # új beállítás: mindent újrafordít (a kézi javítást is)
                for k in ("hu", "edited", "say", "say_edited", "hu_draft", "hu_long"):
                    ln.pop(k, None)
        lang = (read_json(self.p("words.json")) or {}).get("lang", "en")
        from . import translate as tr

        prog = lambda f: self.progress(f, "Fordítás")    # noqa: E731
        if o["translator"] == "deepl":
            key = o.get("deepl_key") or tr.load_deepl_key({})
            if not key:
                raise RuntimeError("nincs DeepL-kulcs")
            tr.translate_deepl(lines, key, src_lang=lang, formality=o["formality"], log=self.log, progress=prog)
            pmodel = tr.pick_default_model(tr.ollama_models()) if tr.ensure_ollama(self.log) else ""
            if pmodel:                       # a kiejtési lépéshez DeepL mellett is jó a helyi AI, ha van
                free_gpu()
                try:
                    self.state["pron_terms"] = tr.pronounce_ollama(lines, pmodel, log=self.log)
                finally:
                    tr.unload_ollama(pmodel)
        else:
            if not tr.ensure_ollama(self.log):
                raise RuntimeError("az Ollama nem fut, és nem is tudtam elindítani")
            available = tr.ollama_models()
            if not available:
                raise RuntimeError("az Ollama fut, de nem lát egyetlen modellt sem. Ha a modelljeid másik mappában "
                                   "vannak (Ollama beállítások: Model location), lépj ki az Ollamából a tálcán, és "
                                   "indítsd újra; ha még nincs modelled: ollama pull gemma3:27b")
            if model not in available:
                if model:
                    self.log(f"A választott modell ({model}) nincs meg az Ollamában, helyette: "
                             f"{tr.pick_default_model(available)}")
                model = tr.pick_default_model(available)
                o["ollama_model"] = model
            free_gpu()
            self.log(f"Fordítás helyi AI-val: {model}")
            try:
                tr.translate_ollama(lines, model, src_lang=lang, formality=o["formality"], notes=o["notes"],
                                    log=self.log, progress=lambda f: prog(0.5 * f))
                if o.get("review", True):
                    self.log("A fordítás átnézése (második kör)...")
                    tr.review_ollama(lines, model, src_lang=lang, formality=o["formality"], notes=o["notes"],
                                     log=self.log, progress=lambda f: prog(0.5 + 0.2 * f))
                    self.log("Célzott javítás: tükörfordítás, túl hosszú sorok...")
                    tr.fix_problems_ollama(lines, model, src_lang=lang, formality=o["formality"], notes=o["notes"],
                                           log=self.log, progress=lambda f: prog(0.7 + 0.15 * f))
                self.log("Kiejtés: számok, rövidítések, nevek magyarosan...")
                self.state["pron_terms"] = tr.pronounce_ollama(lines, model, log=self.log,
                                                               progress=lambda f: prog(0.85 + 0.15 * f))
            finally:
                tr.unload_ollama(model)
        self.save_lines(lines)
        missing = [ln["id"] + 1 for ln in lines if not ln.get("hu")]
        if missing:
            self.log(f"Figyelem: {len(missing)} sor fordítás nélkül maradt: {missing[:20]}")
        self._done("translate", sig)

    # 6. magyar hang
    def _refs(self, lines, vocals, sr, mode=None):
        """Referencia hangminta soronként: (kulcs, (hullámforma, sr), szöveg)."""
        mode = mode or self.opt["ref_mode"]
        import torch

        from .transcribe import join_words

        words = (read_json(self.p("words.json")) or {}).get("words", [])
        mono = vocals.mean(axis=1) if vocals.ndim > 1 else vocals

        total = len(mono) / sr
        index = {ln["id"]: i for i, ln in enumerate(lines)}

        def span(ln, end=None, next_start=None):
            """A minta határai bővítve: a Whisper szóvégei gyakran korán vannak, és ha a minta utolsó
            szótagja csonka, a hangmodell a magyar mondat elején "befejezi" (pl. "Ebb..."). A bővítés
            a szomszéd mondatig tart; a felesleges csendet a hangmodell levágja."""
            i = index[ln["id"]]
            lo = lines[i - 1]["end"] + 0.03 if i > 0 else 0.0
            hi = next_start if next_start is not None else (lines[i + 1]["start"] if i + 1 < len(lines) else total)
            a = max(lo, ln["start"] - REF_HEAD_PAD)
            b = min(hi - 0.03, (end if end is not None else ln["end"]) + REF_TAIL_PAD)
            return min(a, ln["start"]), max(b, end if end is not None else ln["end"])

        def clip(a, b):
            seg = mono[int(a * sr): int(b * sr)]
            return torch.from_numpy(np.ascontiguousarray(seg)), sr

        # beszélőnkénti hangminta: a legtisztább, legkerekebb sorokból, 8-11 mp
        spk_ref = {}
        for spk in sorted({ln.get("speaker", 0) for ln in lines}):
            cands = sorted([ln for ln in lines if ln.get("speaker", 0) == spk],
                           key=lambda ln: -(min(ln["end"] - ln["start"], 9.0) * ln.get("conf", 0.8)))
            picked, total = [], 0.0
            for ln in cands:
                d = ln["end"] - ln["start"]
                if d > REF_MAX or (picked and total + d > REF_MAX):
                    continue
                picked.append(ln)
                total += d
                if total >= SPK_REF_TARGET:
                    break
            if not picked:
                picked = cands[:1]
            picked.sort(key=lambda ln: ln["start"])
            gap = np.zeros(int(0.25 * sr), dtype=np.float32)
            parts = []
            for ln in picked:
                a, b = span(ln)
                parts += [mono[int(a * sr): int(b * sr)], gap]
            spk_ref[spk] = (f"spk{spk}", (torch.from_numpy(np.concatenate(parts[:-1])), sr),
                            " ".join(ln["en"] for ln in picked))

        refs = {}
        for ln in lines:
            d = ln["end"] - ln["start"]
            if mode == "line" and d >= LINE_REF_MIN:
                if d <= REF_MAX:
                    refs[ln["id"]] = (f"line{ln['id']}", clip(*span(ln)), ln["en"])
                    continue
                # hosszú sor: az első ~10 mp, szóhatáron vágva (a következő szó elejéig bővítve)
                inl = [w for w in words if w["start"] >= ln["start"] - 0.01 and w["end"] <= ln["end"] + 0.01]
                ws = [w for w in inl if w["end"] - ln["start"] <= REF_MAX - 1]
                if len(ws) >= 3 and len(ws) < len(inl):
                    nxt = inl[len(ws)]["start"]
                    refs[ln["id"]] = (f"line{ln['id']}", clip(*span(ln, ws[-1]["end"], nxt)), join_words(ws))
                    continue
            refs[ln["id"]] = spk_ref[ln.get("speaker", 0)]
        return refs

    def _avail(self, lines, i, total):
        ln = lines[i]
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else total
        slot = ln["end"] - ln["start"]
        return max(0.6, min(nxt - ln["start"] - 0.08, slot + 1.5))

    def synthesize(self):
        from .tts import HungarianVoice, speakable

        lines = self.lines()
        total = self.state["info"]["duration"] or 0
        vocals, sr = media.read_audio(self.p("vocals.wav"), sr=24000)
        total = total or len(vocals) / sr
        os.makedirs(self.p("tts"), exist_ok=True)
        hu_mode = self.opt["ref_mode"] == "hu"
        refs = self._refs(lines, vocals, sr, mode="speaker" if hu_mode else None)
        eng_refs = dict(refs)
        if hu_mode:        # a sorok a magyarosított mintát kapják; a hangja a hangmodell betöltése után készül el
            from .tts import ANCHOR_TEXT

            for ln in lines:
                spk = ln.get("speaker", 0)
                refs[ln["id"]] = (f"hu{spk}-{_sig(eng_refs[ln['id']][2], ANCHOR_TEXT, 'a1')}", None, ANCHOR_TEXT)

        def key_of(ln, i):
            ref = refs[ln["id"]]
            return _sig(ln.get("hu", ""), ln.get("say", ""), ref[0], ref[2], round(self._avail(lines, i, total), 2),
                        self.opt["ref_mode"], round(float(self.opt.get("tempo") or 1.0), 2), "ov2")

        todo = [i for i, ln in enumerate(lines)
                if ln.get("hu") and not (ln.get("tts_key") == key_of(ln, i)
                                         and os.path.exists(self.p(ln.get("tts_file", "-"))))]
        if not todo:
            self.log("Minden sor hangja kész (gyorsítótárból)")
            return
        self.log(f"{len(todo)} sor hangja készül" + (f" (a többi {len(lines) - len(todo)} kész)"
                                                       if len(todo) < len(lines) else ""))
        voice = HungarianVoice(log=self.log, qc=self.opt["qc"], tempo=self.opt.get("tempo", 1.0))
        too_long = []
        try:
            if hu_mode:
                self._hu_anchors(voice, lines, refs, eng_refs, vocals, sr)
            t0 = time.time()
            for n, i in enumerate(todo):
                self.check_cancel()
                too_long += self._synth_one(voice, lines, i, refs, total, speakable, key_of)
                self.progress(0.85 * (n + 1) / len(todo), f"Magyar hang: {n + 1}/{len(todo)} sor")
                if (n + 1) % 10 == 0:
                    self.save_lines(lines)
                    el = time.time() - t0
                    self.log(f"  {n + 1}/{len(todo)} sor ({el / (n + 1):.1f} mp/sor, "
                             f"hátra kb. {fmt_time(el / (n + 1) * (len(todo) - n - 1))})")
            self.save_lines(lines)
            if too_long and self.opt["translator"] == "ollama" and self.opt.get("ollama_model"):
                voice.close()
                voice = None
                self._shorten(lines, too_long, total)
                self.save_lines(lines)
                voice = HungarianVoice(log=self.log, qc=self.opt["qc"], tempo=self.opt.get("tempo", 1.0))
                for n, i in enumerate(too_long):
                    self.check_cancel()
                    self._synth_one(voice, lines, i, refs, total, speakable, key_of)
                    self.progress(0.85 + 0.15 * (n + 1) / len(too_long), "Rövidített sorok")
                self.save_lines(lines)
        finally:
            self.save_lines(lines)          # megszakításkor is megmarad, ami elkészült
            if voice:
                voice.close()
        bad = [ln["id"] + 1 for ln in lines if ln.get("qc_cer", 0) > QC_FLAG]
        if bad:
            self.log(f"Figyelem: {len(bad)} sor a visszahallgatás szerint eltér a szövegtől "
                     f"(érdemes meghallgatni): {bad[:20]}")

    def _hu_anchors(self, voice, lines, refs, eng_refs, vocals, sr):
        """Beszélőnként egy "magyarosított" hangminta (a beszélő hangján elmondott magyar mondat), a
        munkamappában megőrizve (refs/hu_<kulcs>.wav), hogy az újragenerálásnál ne kelljen újra elkészíteni."""
        import soundfile as sf
        import torch

        os.makedirs(self.p("refs"), exist_ok=True)
        need = {}
        for ln in lines:
            key = refs[ln["id"]][0]
            if key not in need:
                need[key] = (ln.get("speaker", 0), eng_refs[ln["id"]])
        emb = None
        made = {}
        for key, (spk, (ekey, eaudio, etext)) in need.items():
            path = self.p(f"refs/{key}.wav")
            if os.path.exists(path):
                y, _ = media.read_audio(path, mono=True)
            else:
                if emb is None and voice.asr is not None:
                    from .speakers import SpeakerEmbedder

                    emb = SpeakerEmbedder()
                sim_fn = None
                if emb is not None:
                    import librosa

                    mono = vocals.mean(axis=1) if vocals.ndim > 1 else vocals
                    seg = np.concatenate([mono[int(x["start"] * sr): int(x["end"] * sr)]
                                          for x in lines if x.get("speaker", 0) == spk][:12])
                    target = emb.embed(librosa.resample(seg, orig_sr=sr, target_sr=16000))
                    sim_fn = lambda y, t=target: float(emb.embed(librosa.resample(y, orig_sr=24000, target_sr=16000)) @ t)  # noqa: E731
                y, c, ph, sim = voice.hu_anchor(voice.prompt(ekey, eaudio, etext), sim_fn)
                sf.write(path, y, 24000)
                self.log(f"  magyarosított hangminta, {spk + 1}. beszélő: magyar {ph:.0%}, "
                         f"hasonlóság {sim:.2f}, eltérés {c:.0%}")
            made[key] = (torch.from_numpy(np.ascontiguousarray(y, dtype=np.float32)), 24000)
        if emb is not None:
            emb.close()
        for ln in lines:
            key, _, text = refs[ln["id"]]
            refs[ln["id"]] = (key, made[key], text)

    def _synth_one(self, voice, lines, i, refs, total, speakable, key_of):
        import soundfile as sf

        ln = lines[i]
        key, ref_audio, ref_text = refs[ln["id"]]
        say = ln.get("say")
        if not say and self.state.get("pron_terms"):      # pl. kézzel átírt sor: a mentett kiejtési szótárral
            from .translate import apply_terms

            say = apply_terms(ln["hu"], self.state["pron_terms"])
        text = speakable(say or ln["hu"])                 # a kiejtés szerinti változat, ha van
        avail = self._avail(lines, i, total)
        try:
            prompt = voice.prompt(key, ref_audio, ref_text)
            r = voice.synth_fit(text, prompt, avail, seed0=ln["id"] * 7, refs=[ln["hu"], say, text])
        except Exception as e:
            self.log(f"  {i + 1}. sor: hiba a hang készítésekor: {short_err(e)}")
            return []
        f = f"tts/line_{ln['id']:04d}.wav"
        sf.write(self.p(f), r["y"], 24000)
        ln.update({"tts_file": f, "tts_key": key_of(ln, i), "tts_dur": round(r["dur"], 2),
                   "tts_natural": round(r["natural"], 2), "avail": round(avail, 2),
                   "qc_cer": round(r["cer"], 3), "qc_heard": r["hyp"], "fits": r["fits"]})
        return [] if r["fits"] or ln.get("edited") else [i]

    def _shorten(self, lines, idx, total):
        from . import translate as tr

        model = self.opt["ollama_model"]
        self.log(f"{len(idx)} sor túl hosszú, rövidebb fordítást kérek ({model})...")
        free_gpu()
        try:
            for i in idx:
                ln = lines[i]
                ratio = ln["avail"] / max(0.1, ln["tts_natural"])
                target = max(6, int(len(ln["hu"]) * ratio * 0.95))
                try:
                    new = tr.shorten_ollama(ln, target, model, formality=self.opt["formality"],
                                            notes=self.opt["notes"])
                    if new and len(new) < len(ln["hu"]):
                        self.log(f"  {i + 1}. sor: „{ln['hu']}” -> „{new}”")
                        ln["hu_long"], ln["hu"] = ln["hu"], new
                except Exception as e:
                    self.log(f"  {i + 1}. sor rövidítése nem sikerült: {short_err(e)}")
            more = tr.pronounce_ollama([lines[i] for i in idx], model, log=self.log)   # az új szöveg kiejtése
            self.state.setdefault("pron_terms", {}).update(more or {})
        finally:
            tr.unload_ollama(model)

    # 7. keverés + mentés
    def mix(self):
        from .mix import duck_original, limiter, match_loudness, place_lines, to_srt, trim_silence

        lines = self.lines()
        info = self.state["info"]
        bg, sr = media.read_audio(self.p("background.wav"), sr=media.MIX_SR)
        vocals, _ = media.read_audio(self.p("vocals.wav"), sr=media.MIX_SR)
        n = len(bg)
        if bg.ndim == 1:
            bg = np.stack([bg, bg], axis=1)
        items, used = [], []
        import librosa

        for i, ln in enumerate(lines):
            f = ln.get("tts_file")
            if not f or not os.path.exists(self.p(f)):
                continue
            y, _ = media.read_audio(self.p(f), mono=True)
            y = trim_silence(y, 24000)
            ref = vocals[int(ln["start"] * sr): int(ln["end"] * sr)]
            y = match_loudness(y, ref, 24000, sr)
            y = librosa.resample(y, orig_sr=24000, target_sr=sr, res_type="soxr_hq").astype(np.float32)
            nxt = lines[i + 1]["start"] if i + 1 < len(lines) else n / sr
            items.append({"start": ln["start"], "end": ln["end"], "y": y, "hard_end": nxt + 0.3})
            used.append(i)
        self.progress(0.3, "Keverés")
        dub, placed = place_lines(items, n, sr)
        out = np.repeat(dub[:, None], 2, axis=1)
        if self.opt["keep_background"]:
            out += bg[:n]
        if self.opt["keep_nonspeech"]:
            spans = [(ln["start"], ln["end"]) for ln in lines] + placed
            out += duck_original(vocals[:n], spans, sr)
        out = limiter(out, sr)
        media.write_audio(self.p("dub_mix.wav"), out, sr)
        self.progress(0.6, "Mentés")
        # felirat a tényleges elhelyezés szerint
        times = [None] * len(lines)
        for i, (s, e) in zip(used, placed):
            times[i] = (s, max(e, lines[i]["end"]))
        sub_lines = [ln for i, ln in enumerate(lines) if times[i]]
        sub_times = [t for t in times if t]
        os.makedirs(OUT_DIR, exist_ok=True)
        stem = safe_stem(self.src) + "_magyar"
        srt = os.path.join(OUT_DIR, stem + ".srt")
        to_srt(sub_lines, srt, "hu", sub_times)
        to_srt(lines, os.path.join(OUT_DIR, safe_stem(self.src) + "_eredeti.srt"), "en")
        out_path = os.path.join(OUT_DIR, stem + media.output_ext(self.src, info["has_video"]))
        if info["has_video"]:
            media.mux_video(self.src, self.p("dub_mix.wav"), out_path,
                            keep_original=self.opt["keep_original_track"],
                            srt_path=srt if self.opt["subtitles"] else None)
            hu_audio = os.path.join(OUT_DIR, stem + "_hangsav.m4a")      # a magyar hangsáv külön is
            media.encode_audio(self.p("dub_mix.wav"), hu_audio)
        else:
            media.encode_audio(self.p("dub_mix.wav"), out_path)
            hu_audio = None
        self.state["result"] = {"output": out_path, "srt": srt, "has_video": info["has_video"],
                                "hu_audio": hu_audio, "lines": len(lines), "dubbed": len(used)}
        self.save_state()
        self.log(f"Kész: {out_path}")
