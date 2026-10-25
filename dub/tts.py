# -*- coding: utf-8 -*-
"""Magyar beszéd az eredeti beszélő hangján (OmniVoice, hangklónozás), időre igazítva, és
minőségellenőrzéssel: minden kész mondatot a Whisper visszahallgat, és ha nem azt mondja,
amit kell (elakad, ismétel, kihagy), új próbálkozás jön."""

import re
import unicodedata

import numpy as np

from .util import free_gpu

TTS_SR = 24000
MAX_SPEEDUP = 1.35             # ennél jobban nem gyorsítjuk a beszédet (utána: rövidebb fordítás)
QC_MAX_CER = 0.2               # a visszahallgatott szöveg ennyiben térhet el a várttól
QC_MODEL = "large-v3"          # magyarul jóval pontosabb a turbónál (kevesebb téves riasztás)
FIT_SLACK = 0.35               # ennyivel (mp) nyúlhat túl egy sor a helyén
# A "magyarosított" hangminta szövege: természetes, sokféle magyar hangot tartalmazó, kb. 8 mp-es mondat.
ANCHOR_TEXT = ("Sziasztok! Ma egy nagyon izgalmas dologról szeretnék mesélni nektek, "
               "és remélem, hogy ti is legalább annyira élvezni fogjátok, mint én.")


# -----------------------------------------------------------------------------
# Szöveg előkészítése a felolvasáshoz
# -----------------------------------------------------------------------------

_SYMBOLS = {"%": " százalék", "&": " és ", "+": " plusz ", "=": " egyenlő ", "@": " kukac ",
            "°C": " Celsius-fok", "°": " fok", "€": " euró", "$": " dollár", "£": " font", "#": " "}
_ABBR = [(r"\bkb\.", "körülbelül"), (r"\bpl\.", "például"), (r"\bstb\.", "és így tovább"),
         (r"\bill\.", "illetve"), (r"\bún\.", "úgynevezett"), (r"\bvmi\b", "valami"), (r"\bvki\b", "valaki"),
         (r"\bdb\b\.?", "darab"), (r"\bkg\b", "kiló"), (r"\bkm\b", "kilométer"), (r"\bcm\b", "centi"),
         (r"\bmm\b", "milliméter"), (r"\bmp\b", "másodperc"), (r"\bmin\.", "minimum"), (r"\bmax\.", "maximum"),
         (r"\b[Dd]r\.", "doktor"), (r"\bFt\b", "forint"), (r"\bOK\b", "oké")]


def _num_hu(n):
    try:
        from num2words import num2words

        return num2words(int(n), lang="hu")
    except Exception:
        return n


_LETTERS = {"A": "á", "B": "bé", "C": "cé", "D": "dé", "E": "é", "F": "ef", "G": "gé", "H": "há", "I": "í",
            "J": "jé", "K": "ká", "L": "el", "M": "em", "N": "en", "O": "ó", "P": "pé", "Q": "kú", "R": "er",
            "S": "es", "T": "té", "U": "ú", "V": "vé", "W": "dupla vé", "X": "iksz", "Y": "ipszilon", "Z": "zé"}
# egyben kimondott rövidítések (nem betűzzük őket)
_WORD_ACRONYMS = {"ASUS": "ászusz", "NASA": "nászá", "NATO": "nátó", "RAM": "rem", "ROM": "rom", "LED": "led",
                  "ROG": "rog", "GIF": "gif", "LAN": "lan", "WIFI": "vájfáj", "COVID": "kovid", "OLED": "óled"}


def _spell(m):
    w, suffix = m.group(1), m.group(2) or ""
    spoken = _WORD_ACRONYMS.get(w) or "-".join(_LETTERS.get(c, c) for c in w)
    return spoken + suffix                     # a rag egybeírva: AMD-vel -> á-em-dével


_VOWELS = "aáeéiíoóöőuúüű"
# számnév vége magánhangzóval kezdődő rag előtt: hétezer+es -> hétezres, három+at -> hármat, hét+es -> hetes
_NUM_STEMS = (("ezer", "ezr"), ("kettő", "kett"), ("három", "hárm"), ("hét", "het"))


def _num_suffix(words, suffix):
    if suffix and suffix[0].lower() in _VOWELS:
        for full, stem in _NUM_STEMS:
            if words.endswith(full):
                return words[: -len(full)] + stem + suffix
    return words + suffix


def _num_match(m):
    """Szám (verzió, tizedes, egész) a ragjával együtt: 4.1-et -> négy pont egyet, 7000-es -> hétezres."""
    a, sep, b, unit, suffix = m.group(1), m.group(2), m.group(3), m.group(4) or "", m.group(5) or ""
    words = _num_hu(a)
    if b:
        if sep == ",":                                                   # 2,5 tizedes: két egész öt
            words = re.sub(r"kettő$", "két", words) + " egész " + _num_hu(b)
        else:                                                            # 4.1 verzió: négy pont egy
            words += " pont " + _num_hu(b)
    if unit:                                                             # 1440p, 4K
        return words + " " + {"p": "pé", "k": "ká"}[unit.lower()] + suffix
    return _num_suffix(words, suffix)


def speakable(text):
    """Felolvasható változat: számok, jelek, rövidítések kiírva úgy, ahogy magyarul mondjuk, a ragokkal együtt.
    A helyi AI kiejtési lépése ("say") a neveket és rövidítéseket írja át; a számokat mindig ez alakítja
    szöveggé (az AI néha elrontja őket)."""
    t = unicodedata.normalize("NFC", text)
    for pat, rep in _ABBR:
        t = re.sub(pat, rep, t)
    t = re.sub(r"(\d{1,2}):(\d{2})(?:-(\w+))?",
               lambda m: _num_hu(m.group(1)) + " óra " + _num_suffix(_num_hu(m.group(2)), m.group(3) or ""), t)
    t = re.sub(r"%(?:-(\w+))?", lambda m: " százalék" + (m.group(1) or ""), t)    # 5%-kal -> öt százalékkal
    for k, v in _SYMBOLS.items():
        t = t.replace(k, v)
    t = re.sub(r"\b([A-Z])(?=\d)", lambda m: _LETTERS[m.group(1)] + " ", t)   # A14 -> á 14, Z13 -> zé 13
    t = re.sub(r"(?<=\d)([A-JL-Z])\b", lambda m: " " + _LETTERS[m.group(1)], t)  # Z2E -> ... kettő é
    t = re.sub(r"(?<=[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű])(?=\d)", " ", t)       # PS5 -> PS 5
    t = re.sub(r"\bfps\b(?:-(\w+))?", lambda m: "ef-pé-es" + (m.group(1) or ""), t, flags=re.I)
    t = re.sub(r"(\d+)(?:([.,])(\d+))?(?:\s?([pPkK])\b)?(?:-(\w+))?", _num_match, t)
    t = re.sub(r"\b([A-Z]{2,5})\b(?:-(\w+))?", _spell, t)                    # AMD-vel -> á-em-dével
    t = re.sub(r"[\"“”„»«*_~^<>\[\]{}|]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t and t[-1] not in ".!?…":
        t += "."
    return t


def _norm(t):
    t = unicodedata.normalize("NFC", t.lower())
    t = re.sub(r"\d+", lambda m: _num_hu(m.group(0)), t)
    t = re.sub(r"[^\w\s]", " ", t)
    return "".join(t.split())                  # szóközök nélkül: az egybe/külön írás ne számítson


def cer(ref, hyp):
    """Karakterhiba-arány. A ref lehet több változat is (felirat és kiejtés): a legjobban illeszkedőt
    számolja, mert a Whisper az "ikszboksz"-ot "Xbox"-nak, a "hatvan"-at "60"-nak írja."""
    if isinstance(ref, (list, tuple)):
        refs = [r for r in ref if r]
        return min(cer(r, hyp) for r in refs) if refs else 0.0
    a, b = _norm(ref), _norm(hyp)
    if not a:
        return 0.0 if not b else 1.0
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (ca != cb))
            prev, d[j] = d[j], cur
    return d[len(b)] / len(a)


# -----------------------------------------------------------------------------
# Hangmotor
# -----------------------------------------------------------------------------

class HungarianVoice:
    def __init__(self, log=print, qc=True, num_step=32, tempo=1.0):
        import torch
        from omnivoice import OmniVoice

        self.torch = torch
        self.log = log
        self.num_step = num_step
        self.tempo = float(tempo or 1.0)        # beszédtempó (1.0 = a beszélő természetes tempója)
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        log("Hangmodell betöltése (OmniVoice)...")
        self.model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=self.device,
                                               dtype=torch.float16 if self.device != "cpu" else torch.float32)
        self.prompts = {}
        self.asr = None
        if qc:
            from faster_whisper import WhisperModel

            self.asr = WhisperModel(QC_MODEL, device="cuda" if self.device != "cpu" else "cpu",
                                    compute_type="float16" if self.device != "cpu" else "int8")

    def close(self):
        self.model = self.asr = None
        self.prompts.clear()
        free_gpu()

    def prompt(self, key, ref_wav, ref_text):
        """Hangminta (a referencia hang + a szövege), kulcs szerint gyorsítótárazva."""
        p = self.prompts.get(key)
        if p is None:
            p = self.model.create_voice_clone_prompt(ref_audio=ref_wav, ref_text=ref_text)
            self.prompts[key] = p
        return p

    def _gen(self, text, prompt, duration=None, seed=0, speed=None):
        self.torch.manual_seed(seed)
        kw = {"text": text, "language": "hu", "voice_clone_prompt": prompt, "num_step": self.num_step}
        sp = self.tempo if speed is None else speed
        if duration:
            kw["duration"] = float(duration)
        elif abs(sp - 1.0) > 0.01:
            kw["speed"] = sp
        y = self.model.generate(**kw)[0]
        return np.asarray(y, dtype=np.float32).reshape(-1)

    def lang_prob(self, y, lang="hu"):
        """Mennyire hallja a Whisper magyarnak (0-1): erős akcentusnál alacsony."""
        if self.asr is None:
            return 1.0
        import librosa

        y16 = librosa.resample(y, orig_sr=TTS_SR, target_sr=16000, res_type="soxr_hq")
        segs, info = self.asr.transcribe(y16, language=None, beam_size=1, without_timestamps=True,
                                         condition_on_previous_text=False)
        list(segs)
        return float(dict(info.all_language_probs or []).get(lang, 0.0))

    def hu_anchor(self, eng_prompt, similarity=None, tries=4, seed0=100):
        """"Magyarosított" hangminta: a beszélő hangján (az angol mintából) elmondatott magyar mondat. Ezt
        használva mintának a magyar sorok magyar kiejtéssel szólnak (az angol minta angolos akcentust ad).
        A próbálkozások közül a hibátlan, biztosan magyarnak hallott változatok közül a leghasonlóbb hangú nyer.
        similarity: fn(hullámforma) -> hasonlóság az eredeti hanghoz (vagy None)."""
        text = speakable(ANCHOR_TEXT)
        cands = []
        for k in range(tries if self.asr is not None else 1):
            y = self._gen(text, eng_prompt, seed=seed0 + k, speed=1.0)   # a minta mindig természetes tempójú
            c, _ = self._check(y, [ANCHOR_TEXT, text])
            ph = self.lang_prob(y)
            sim = similarity(y) if similarity else 0.0
            cands.append((y, c, ph, sim))
        good = [x for x in cands if x[1] <= 0.08 and x[2] >= 0.9]
        best = max(good, key=lambda x: x[3]) if good else min(cands, key=lambda x: x[1] - 0.3 * x[2])
        return best

    def _check(self, y, refs):
        if self.asr is None:
            return 0.0, ""
        import librosa

        y16 = librosa.resample(y, orig_sr=TTS_SR, target_sr=16000, res_type="soxr_hq")
        segs, _ = self.asr.transcribe(y16, language="hu", beam_size=5, condition_on_previous_text=False,
                                      vad_filter=False, without_timestamps=True)
        hyp = " ".join(s.text for s in segs).strip()
        return cer(refs, hyp), hyp

    def synth(self, text, prompt, duration=None, tries=3, seed0=0, refs=None):
        """A legjobb próbálkozás: (hang, cer, visszahallgatott szöveg, próbák száma).
        refs: a szöveg írott változatai az ellenőrzéshez (felirat + kiejtés)."""
        best = None
        for k in range(tries):
            y = self._gen(text, prompt, duration, seed=seed0 + k)
            c, hyp = self._check(y, refs or [text])
            if best is None or c < best[1]:
                best = (y, c, hyp, k + 1)
            if c <= QC_MAX_CER:
                break
        return best

    def synth_fit(self, text, prompt, avail, seed0=0, refs=None):
        """Természetes tempóban generál; ha nem fér bele az `avail` másodpercbe, legfeljebb
        MAX_SPEEDUP-szoros gyorsítással újra. -> dict(y, dur, natural, cer, hyp, tries, fits)"""
        y, c, hyp, n = self.synth(text, prompt, seed0=seed0, refs=refs)
        natural = len(y) / TTS_SR
        if natural > avail + 0.05:
            target = max(avail, natural / MAX_SPEEDUP)
            y2, c2, hyp2, n2 = self.synth(text, prompt, duration=target, seed0=seed0 + 10, refs=refs)
            if c2 <= max(QC_MAX_CER, c + 0.05):
                y, c, hyp, n = y2, c2, hyp2, n + n2
        dur = len(y) / TTS_SR
        # egy kis túlnyúlást a keverés elnyel (a következő sor pár tized mp-cel később indul)
        return {"y": y, "dur": dur, "natural": natural, "cer": c, "hyp": hyp, "tries": n,
                "fits": dur <= avail + FIT_SLACK}
