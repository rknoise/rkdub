# -*- coding: utf-8 -*-
"""Fordítás szinkronszöveggé: helyi AI (Ollama) vagy DeepL.

A helyi AI egyszerre több sort kap a környezetükkel együtt, és tudja, nagyjából hány
karakterbe kell beleférnie a magyar mondatnak, hogy ugyanannyi idő alatt elhangozzon."""

import json
import os
import re

import requests

from .util import RKSUBBY_DEEPL_KEY

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
HU_MIN_CHARS_PER_SEC = 11.0     # lassú beszélőnél is legalább ennyi karakter fér el másodpercenként
LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
              "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ru": "Russian", "pt": "Portuguese",
              "pl": "Polish", "nl": "Dutch", "tr": "Turkish", "uk": "Ukrainian", "sv": "Swedish"}


# -----------------------------------------------------------------------------
# Ollama
# -----------------------------------------------------------------------------

def ollama_models():
    """A telepített Ollama-modellek nevei (üres lista, ha az Ollama nem fut)."""
    try:
        r = requests.get(OLLAMA_URL + "/api/tags", timeout=2)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []))
    except Exception:
        return []


def ensure_ollama(log=print):
    """Ha az Ollama telepítve van, de nem fut, elindítja.
    Az Ollama ALKALMAZÁST indítja (nem egy csupasz "ollama serve"-t): az alkalmazás a saját beállításaival
    (pl. egy másik meghajtón lévő modellmappa) indítja a szervert. A csupasz szerver az üres
    alapértelmezett mappát használná, és foglalná a portot, így az alkalmazás nem tudna elindulni."""
    if _ollama_alive():
        return True
    import shutil
    import subprocess
    import time

    app = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama app.exe")
    exe = shutil.which("ollama") or os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.path.exists(app):
        log("Az Ollama nem fut, elindítom az Ollama alkalmazást...")
        subprocess.Popen([app], creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.path.exists(exe):              # alkalmazás nélküli telepítés: a szerver a környezeti változókat használja
        log("Az Ollama nem fut, elindítom...")
        subprocess.Popen([exe, "serve"], creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        return False
    for _ in range(120):
        time.sleep(0.5)
        if _ollama_alive():
            time.sleep(1.0)                # a modellek listája kicsivel a szerver után áll össze
            return True
    return False


def _ollama_alive():
    try:
        return requests.get(OLLAMA_URL + "/api/version", timeout=1).ok
    except Exception:
        return False


def pick_default_model(models):
    # Tesztelve (2026. október, Ollama 0.40.2, RTX 3090): a gemma3:27b fordít a legtermészetesebben magyarra.
    # A gemma4 ebben az Ollama-verzióban hibás magyar szavakat ír (idegen betűk, torz szavak), ezért hátul van.
    prefs = ("gemma3:27b", "qwen3.8:27b", "gemma3:12b", "qwen3:32b", "qwen3:30b", "qwen3:14b",
             "gemma4:31b", "gemma4:26b", "gemma4")
    for p in prefs:
        for m in models:
            if m.startswith(p):
                return m
    return next((m for m in models if "vl" not in m and "embed" not in m), models[0] if models else "")


def unload_ollama(model):
    """A modell kiürítése a videókártyáról (a hangmodellnek kell a hely)."""
    try:
        requests.post(OLLAMA_URL + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


# Szinkronfordítói stílus: a leggyakoribb tükörfordítási hibák jó/rossz példákkal (a modellek ezekből
# tanulnak a legtöbbet). Szándékosan általános példák, nem egy konkrét videóból.
HU_STYLE = (
    "Hungarian style (this is the most important part - the result must sound like a professional Hungarian "
    "dub, never like a translation):\n"
    "- Translate the meaning, not the words. Rebuild the sentence the Hungarian way: Hungarian word order "
    "(the focus goes right before the verb), short clear sentences, verbs instead of noun chains.\n"
    "- Drop pronouns Hungarian does not need: 'Mi szeretnénk' -> 'Szeretnénk', 'a mi csapatunk' -> 'a csapatunk', "
    "'ő elmondta nekem' -> 'elmondta'.\n"
    "- Avoid anglicisms (Hunglish): no 'Ez egy ...' for 'It's a ...', no '-val/-vel való', no chains of '-ként', "
    "'számára', 'által', 'kerül', 'történik', no passive voice, no 'izgatottak vagyunk' for 'we're excited'.\n"
    "  Examples (wrong -> right):\n"
    "  'Ez egy felelősség, amit komolyan veszünk.' -> 'Ezt a felelősséget komolyan vesszük.'\n"
    "  'a Microsofttal való együttműködésünk' -> 'a Microsofttal közös munkánk'\n"
    "  'Izgatottak vagyunk, hogy bejelenthetjük' -> 'Örömmel jelentjük be'\n"
    "  'Ez a játék jövője.' (for 'This is the future of gaming.') -> 'Így néz ki a játék jövője.'\n"
    "  'Szenvedélyes játékosként sok időt töltök azzal, hogy...' -> 'Világéletemben játszottam, és sokat "
    "gondolkodom azon, hogy...'\n"
    "- Prefer real Hungarian words to English loanwords: gaming -> játék / játékosok (never 'gaming'), "
    "gamer -> játékos. 'gaming' before a noun becomes a compound: gaming hardware -> játékhardver, gaming device -> "
    "játékeszköz, gaming laptop -> játékra szánt laptop; never use 'játékos' as an adjective (it means "
    "'playful'). handheld -> kézi konzol, upscaling -> felskálázás, performance -> teljesítmény, "
    "frame rate -> képkockasebesség, frame generation -> képkocka-generálás, machine learning -> gépi tanulás, "
    "feature -> funkció, update -> frissítés. Keep product names, brand names, people's names and well-known "
    "acronyms (e.g. Windows, PlayStation, FSR, 4K) as they are.\n"
    "- Use correct Hungarian grammar and suffixes (agreement, -ban/-ben, -on/-en/-ön, definite vs. indefinite "
    "conjugation). Attach suffixes to names and acronyms with a hyphen when needed: 'az FSR-rel', 'a PS5-ön'.\n"
    "- Keep the tone: an enthusiastic presenter stays enthusiastic, a casual chat stays casual, but never "
    "exaggerate or add content.\n"
)


def _system_prompt(src_lang, formality, notes):
    src = LANG_NAMES.get(src_lang, src_lang or "English")
    s = (f"You are an experienced Hungarian dubbing translator (szinkronfordító). You translate {src} video "
         "dialogue into natural, idiomatic, spoken Hungarian that a Hungarian voice actor will perform as a "
         "lip-free dub.\n"
         "Rules:\n"
         "- Keep the meaning, tone, emotion and register. Slang and swearing are fine when the original has them.\n"
         "- Every line has a time budget (max_chars). The Hungarian line must be speakable in the "
         "same time as the original: stay within max_chars; if needed, condense and drop filler words, "
         "but never drop essential information.\n"
         "- Numbers, units and versions may stay as digits (e.g. 4.1, 60 fps, 1440p, 2026); a separate step "
         "spells them out for the voice. Use Hungarian number formatting otherwise (decimal comma: 2,5).\n"
         "- Each line is translated separately (do not merge or split lines), but use the "
         "surrounding lines for context.\n"
         "- The text comes from speech recognition: punctuation and sentence boundaries may be missing or "
         "wrong (e.g. 'come on we're late' = 'Come on. We're late.'), and product names may be misheard. "
         "Infer the intended meaning from context.\n"
         "- No quotes, brackets, notes, emojis or explanations in the output.\n"
         + HU_STYLE)
    if formality == "informal":
        s += "- People address each other informally (tegezés).\n"
    elif formality == "formal":
        s += "- Use the formal form of address (magázás).\n"
    else:
        s += "- Choose tegezés or magázás from context (friends, family, YouTubers talking to the viewer: tegezés).\n"
    if notes:
        s += "- Additional instructions from the user: " + notes.strip() + "\n"
    s += 'Answer with JSON only: {"translations": [{"id": <id>, "hu": "<Hungarian line>"}, ...]}'
    return s




def max_chars(line):
    """Karakterkeret: a magyar ugyanattól a beszélőtől kb. 0,9-szer annyi karakter ugyanannyi idő alatt,
    mint az angol (a hangmodell az eredeti beszélő tempóját követi)."""
    by_text = len(line["en"]) * 0.9
    by_time = (line["end"] - line["start"]) * HU_MIN_CHARS_PER_SEC
    return max(8, int(round(max(by_text, by_time))))


def bad_script(text):
    """Van-e benne nem latin betű (pl. kínai, cirill): a kisebb modellek néha belekevernek egy-egy ilyet."""
    import unicodedata

    return any(c.isalpha() and "LATIN" not in unicodedata.name(c, "") for c in text)


def _chat(model, system, user, timeout=600, temperature=0.2):
    # "json" mód és nem kötött séma: a sémás (nyelvtannal kötött) mintavétel több furcsa szót hozott
    body = {"model": model, "stream": False, "think": False, "format": "json", "keep_alive": "10m",
            "options": {"temperature": temperature, "num_ctx": 16384},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = requests.post(OLLAMA_URL + "/api/chat", json=body, timeout=timeout)
    if r.status_code == 400 and "think" in r.text:      # nem gondolkodó modell: "think" nélkül
        body.pop("think")
        r = requests.post(OLLAMA_URL + "/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    m = re.search(r"\{.*\}", content, flags=re.S)
    return json.loads(m.group(0) if m else content)


def translate_ollama(lines, model, src_lang="en", formality="auto", notes="", batch=25, log=print,
                     progress=None):
    """Minden sorba "hu" kerül. A sorokat csoportosan küldi, az előző csoport végével mint környezettel."""
    system = _system_prompt(src_lang, formality, notes)
    todo = [ln for ln in lines if not ln.get("hu")]
    done_ct = 0
    for b in range(0, len(todo), batch):
        chunk = todo[b:b + batch]
        first = chunk[0]["id"]
        before = [ln for ln in lines if ln["id"] < first][-6:]
        after = [ln for ln in lines if ln["id"] > chunk[-1]["id"]][:3]
        user = ""
        if before:
            user += "Previous lines (context, already translated):\n" + "\n".join(
                f'- {ln["en"]}  =>  {ln.get("hu", "")}' for ln in before) + "\n\n"
        user += "Translate these lines:\n" + json.dumps(
            [{"id": ln["id"], "speaker": ln.get("speaker", 0) + 1, "max_chars": max_chars(ln), "text": ln["en"]}
             for ln in chunk], ensure_ascii=False)
        if after:
            user += "\n\nFollowing lines (context only, do not translate):\n" + "\n".join(
                f'- {ln["en"]}' for ln in after)
        got = {}
        for attempt in range(3):
            try:
                data = _chat(model, system, user)
                got = {int(t["id"]): str(t["hu"]).strip() for t in data.get("translations", []) if t.get("hu")}
                if all(ln["id"] in got for ln in chunk):
                    break
                log(f"  a fordítás hiányos ({len(got)}/{len(chunk)} sor), újrapróbálom...")
            except Exception as e:
                log(f"  fordítási hiba: {e}; újrapróbálom...")
        for ln in chunk:
            if ln["id"] in got and not bad_script(got[ln["id"]]):
                ln["hu"] = clean_hu(got[ln["id"]])
        missing = [ln for ln in chunk if not ln.get("hu")]
        for ln in missing:   # hiányzó vagy idegen betűs sor: soronként újra
            ctx = [x for x in lines if x["id"] < ln["id"]][-3:]
            one = ("Context (previous lines):\n" + "\n".join(f'- {x["en"]}' for x in ctx) + "\n\n" if ctx else "")
            one += "Translate these lines:\n" + json.dumps(
                [{"id": ln["id"], "max_chars": max_chars(ln), "text": ln["en"]}], ensure_ascii=False)
            last = ""
            for attempt in range(3):
                try:
                    data = _chat(model, system, one, temperature=0.2 + 0.25 * attempt)
                    last = clean_hu(data["translations"][0]["hu"])
                    if last and not bad_script(last):
                        break
                except Exception as e:
                    log(f"  a(z) {ln['id'] + 1}. sor fordítása: {e}")
            if last:
                if bad_script(last):    # végső eset: az idegen betűs szó kimarad
                    import unicodedata
                    last = clean_hu("".join(c for c in last if not c.isalpha() or "LATIN" in unicodedata.name(c, "")))
                ln["hu"] = last
            else:
                log(f"  a(z) {ln['id'] + 1}. sort nem sikerült lefordítani")
        done_ct += len(chunk)
        if progress:
            progress(done_ct / max(1, len(todo)))
        log(f"  lefordítva: {done_ct}/{len(todo)} sor")
    return lines


# Gépileg felismerhető tükörfordítási minták: az átnéző kör soronként megkapja, mi a baj az adott sorral
# (a modellek így sokkal megbízhatóbban javítanak, mint egy általános "nézd át" kérésre).
_HUNGLISH = [
    # "-val való", "-ra való", "-ban való" ... (a rag előtt kötőjel is lehet: Sony-val való)
    (r"\w\s+való\s+\w", "'... való' construction (Hunglish, e.g. 'a Sonyval való együttműködés') - rephrase "
                        "with a verb or 'közös' (e.g. 'a Sonyval közös munkánk', 'hogy az Xboxra érkezik')"),
    (r"\bszámára\b", "'számára' (often Hunglish) - use a dative suffix (-nak/-nek) or a verb instead"),
    (r"(?:^|[.!?]\s+)Ez egy\b", "starts with 'Ez egy' (anglicism for 'It's a') - rebuild the sentence"),
    (r"\bgam(?:ing|er)\w*", "English loanword 'gaming/gamer' - use játék / játékos / a compound like játékhardver"),
    (r"\bjátékos\s+(?:eszköz|hardver|platform|laptop|technológi|élmény|gép|kártya)\w*",
     "'játékos' used as an adjective (means 'playful') - use a compound: játékeszköz, játékhardver"),
    (r"\báltal\b", "'által' (passive, Hunglish) - use an active sentence"),
    (r"\bizgatott\w*", "'izgatott' for 'excited' is an anglicism - e.g. 'örömmel', 'alig várjuk'"),
    (r"\bfelkerül\w*|\bkerül\w* sor\w*", "bureaucratic 'kerül' - use a simple active verb"),
]


def hunglish_problems(line):
    hu = line.get("hu", "")
    probs = [msg for pat, msg in _HUNGLISH if re.search(pat, hu, flags=re.I)]
    budget = max_chars(line)
    if len(hu) > budget * 1.3:       # kisebb túllépést a hangkészítés elnyel (gyorsítás, szünetek)
        probs.append(f"too long: {len(hu)} characters, the time budget is {budget} - say it more concisely")
    return probs


def review_ollama(lines, model, src_lang="en", formality="auto", notes="", batch=40, log=print, progress=None):
    """Második kör: a kész magyar szöveg átnézése az eredetivel együtt (félrefordítás, tükörfordítás,
    furcsa mondat), és csak a hibás sorok javítása."""
    src = LANG_NAMES.get(src_lang, src_lang or "English")
    system = (f"You are a senior Hungarian dubbing editor (szinkronrendező). You get {src} dialogue lines with "
              "a draft Hungarian dubbing translation. Check every line carefully:\n"
              "1. Does the Hungarian say exactly what the original means in this context? Watch for idioms "
              "translated literally, misread sentence boundaries (the transcript may lack punctuation), wrong "
              "subject/object, wrong tense, and lines that make no sense.\n"
              "2. Would a native Hungarian really say it like this? Rewrite every line that sounds translated: "
              "Hunglish constructions ('Ez egy...', '-val való', '-ként', 'számára', passive voice), English "
              "loanwords where a Hungarian word exists (gaming, gamer), English word order, unnecessary pronouns, "
              "long noun chains.\n"
              "3. Is the Hungarian grammar perfect (suffixes, agreement, definite/indefinite conjugation, "
              "spelling)? Fix every mistake.\n"
              "4. Does it stay within max_chars?\n"
              + _system_prompt(src_lang, formality, notes).split("Rules:\n", 1)[1].rsplit("Answer with JSON", 1)[0]
              + 'Return JSON only, listing ONLY the lines you changed: {"translations": [{"id": <id>, "hu": '
              '"<corrected Hungarian line>"}]}. If everything is correct, return {"translations": []}.')
    fixed = 0
    for b in range(0, len(lines), batch):
        chunk = [ln for ln in lines[b:b + batch] if ln.get("hu")]
        if not chunk:
            continue
        items = []
        for ln in chunk:
            it = {"id": ln["id"], "max_chars": max_chars(ln), "original": ln["en"], "hungarian": ln["hu"]}
            probs = hunglish_problems(ln)
            if probs:
                it["must_fix"] = probs
            items.append(it)
        user = ("Review these dubbing lines (in order). Lines with 'must_fix' have known problems that you MUST "
                "fix; check all the other lines too.\n" + json.dumps(items, ensure_ascii=False))
        try:
            data = _chat(model, system, user)
        except Exception as e:
            log(f"  az átnézés nem sikerült: {e}")
            continue
        by_id = {ln["id"]: ln for ln in chunk}
        for t in data.get("translations", []):
            try:
                ln = by_id.get(int(t["id"]))
            except (KeyError, TypeError, ValueError):
                continue
            new = clean_hu(str(t.get("hu", "")))
            if ln and new and new != ln["hu"] and not ln.get("edited") and not bad_script(new):
                log(f"  javítva: „{ln['hu']}” -> „{new}”")
                ln["hu_draft"], ln["hu"] = ln["hu"], new
                fixed += 1
        if progress:
            progress(min(1.0, (b + batch) / len(lines)))
    log(f"  átnézve: {len(lines)} sor, {fixed} javítás")
    return lines


def fix_problems_ollama(lines, model, src_lang="en", formality="auto", notes="", rounds=2, log=print,
                        progress=None):
    """Célzott javítás: csak a gépileg felismert hibás sorok (tükörfordítás, túl hosszú), kis adagokban,
    soronként megnevezve a hibát. Ami két kör után is hibás, az úgy marad (a hangkészítés még igazít rajta)."""
    system = _system_prompt(src_lang, formality, notes) + (
        '\nYou now fix specific lines. Answer with JSON only: {"translations": [{"id": <id>, "hu": "<fixed line>"}]}')
    fixed = 0
    for rnd in range(rounds):
        bad = [ln for ln in lines if ln.get("hu") and not ln.get("edited") and hunglish_problems(ln)]
        if not bad:
            break
        for b in range(0, len(bad), 10):
            chunk = bad[b:b + 10]
            items = []
            for ln in chunk:
                prev = next((x for x in lines if x["id"] == ln["id"] - 1), None)
                items.append({"id": ln["id"], "max_chars": max_chars(ln), "original": ln["en"],
                              "current_hungarian": ln["hu"], "must_fix": hunglish_problems(ln),
                              "previous_line": prev["en"] if prev else ""})
            user = ("Rewrite each line so that every problem in 'must_fix' disappears, while keeping the full "
                    "meaning of the original and natural spoken Hungarian.\n" + json.dumps(items, ensure_ascii=False))
            try:
                data = _chat(model, system, user, temperature=0.3 + 0.2 * rnd)
            except Exception as e:
                log(f"  a javítás nem sikerült: {e}")
                continue
            by_id = {ln["id"]: ln for ln in chunk}
            for t in data.get("translations", []):
                try:
                    ln = by_id.get(int(t["id"]))
                except (KeyError, TypeError, ValueError):
                    continue
                new = clean_hu(str(t.get("hu", "")))
                if not ln or not new or new == ln["hu"] or bad_script(new):
                    continue
                old_n, new_n = len(hunglish_problems(ln)), len(hunglish_problems(dict(ln, hu=new)))
                if new_n <= old_n and len(new) >= 0.4 * len(ln["hu"]):   # csak ha tényleg jobb lett
                    log(f"  javítva: „{ln['hu']}” -> „{new}”")
                    ln.setdefault("hu_draft", ln["hu"])
                    ln["hu"] = new
                    fixed += 1
        if progress:
            progress((rnd + 1) / rounds)
    left = [ln["id"] + 1 for ln in lines if ln.get("hu") and hunglish_problems(ln)]
    log(f"  célzott javítás: {fixed} sor" + (f"; maradt (főleg hosszú) sor: {left[:15]}" if left else ""))
    return lines


TERM_SYSTEM = (
    "You help a Hungarian text-to-speech voice that can only read Hungarian spelling. You get names, product "
    "names, acronyms and foreign words that appear in a Hungarian dub script (with the line they come from). For "
    "each term write 'say': how a Hungarian voice actor pronounces it, written in Hungarian spelling.\n"
    "- Acronyms spelled letter by letter: Hungarian letter names joined with hyphens: AMD -> á-em-dé, "
    "FSR -> ef-es-er, CPU -> cé-pé-ú, APU -> á-pé-ú, CTO -> cé-té-ó, PC -> pé-cé, XT -> iksz-té, RX -> er-iksz. "
    "Acronyms said as one word are written as Hungarians say them: ASUS -> ászusz, NASA -> nászá, ROG -> rog.\n"
    "- English and other foreign words and names: Hungarian phonetic spelling of the usual Hungarian "
    "pronunciation: Xbox -> ikszboksz, PlayStation -> plejsztésön, Microsoft -> májkroszoft, YouTube -> jútyúb, "
    "Jack -> dzsek, Steam -> sztím, DirectX -> direkt-iksz, Windows -> vindóz, Upscaling -> apszkéling, "
    "Pro -> pró, Series -> szíriz, Diamond -> dájmond, Ryzen -> rájzen, Halo -> héló, Sony -> szoni.\n"
    "- If the term carries a Hungarian suffix (e.g. 'Xboxra', 'Redstone-t'), keep the suffix in the spoken form "
    "(ikszbokszra, redsztónt).\n"
    "- If a term is an ordinary Hungarian word or is already read correctly with Hungarian spelling, return it "
    "unchanged.\n"
    'Return JSON only: {"terms": [{"term": "<term>", "say": "<Hungarian spelling>"}]}'
)

_EN_PATTERNS = re.compile(r"[wxqWXQ]|ck|th|sh|ea|ee|oo|ou|ph|(?<![gGlLnNtT])y", re.I)
_WORD = re.compile(r"[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű][\w]*(?:-[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű]+)?")


def find_terms(text):
    """A sorban lévő nevek, rövidítések, idegen szavak (ezeket kell magyarosan kimondani).
    A magyar szavakhoz nem nyúlunk: csak a mondat belsejében nagybetűs, a csupa nagybetűs és az angolos
    betűkapcsolatú (pl. ck, th, w, x) nagybetűs szavak számítanak."""
    terms = []
    for m in _WORD.finditer(text):
        base = re.sub(r"\d.*$", "", m.group(0).split("-")[0])        # Z13 -> Z (a számot a program mondja ki)
        before = text[: m.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?…:„\""
        if len(base) >= 2 and (
                re.search(r"[A-ZÁÉÍÓÖŐÚÜŰ].*[A-ZÁÉÍÓÖŐÚÜŰ]", base)            # AMD, PlayStation, DirectX
                or (base[0].isupper() and not sentence_start)                 # név a mondat belsejében
                or (base[0].isupper() and _EN_PATTERNS.search(base))):        # Jack, Xbox a mondat elején
            terms.append(base)
    return terms


def apply_terms(text, mapping):
    """A kifejezések cseréje a kiejtésükre (a magyar ragot megtartva: AMD-vel -> á-em-dével)."""
    if not mapping:
        return text
    keys = sorted(mapping, key=len, reverse=True)
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in keys) + r")(-?)([a-záéíóöőúüű]*)")

    def rep(m):
        return mapping[m.group(1)] + m.group(3)
    return pat.sub(rep, text)


def pronounce_ollama(lines, model, log=print, progress=None):
    """A felolvasandó változat ("say"): a nevek, rövidítések, idegen szavak magyar kiejtés szerint leírva.
    Az AI csak a kifejezések kiejtését adja meg, a cserét a program végzi: így a magyar szavak nem változhatnak,
    és ugyanaz a név mindenhol ugyanúgy hangzik. A számokat később a felolvasó lépés írja ki."""
    for ln in lines:                       # a régi (pl. rövidítés előtti) kiejtés érvénytelen
        if not ln.get("say_edited"):
            ln.pop("say", None)
    todo = [ln for ln in lines if ln.get("hu") and not ln.get("say_edited")]
    found = {}
    for ln in todo:
        for t in find_terms(ln["hu"]):
            found.setdefault(t, ln["hu"])
    if not found:
        return {}
    mapping = {}
    items = list(found.items())
    for b in range(0, len(items), 60):
        chunk = items[b:b + 60]
        user = "Terms:\n" + json.dumps([{"term": t, "line": ctx} for t, ctx in chunk], ensure_ascii=False)
        try:
            data = _chat(model, TERM_SYSTEM, user, temperature=0.1)
        except Exception as e:
            log(f"  a kiejtés előkészítése nem sikerült: {e}")
            continue
        for t in data.get("terms", []):
            term, say = str(t.get("term", "")).strip(), clean_hu(str(t.get("say", ""))).strip(" .,!?")
            if (term in found and say and say != term and not bad_script(say) and not re.search(r"\d", say)
                    and len(say) <= 4 * len(term) + 8):
                mapping[term] = say.lower() if term.isupper() else say
        if progress:
            progress(min(1.0, (b + 60) / len(items)))
    changed = 0
    for ln in todo:
        say = apply_terms(ln["hu"], mapping)
        if say != ln["hu"]:
            ln["say"] = say
            changed += 1
    log(f"  kiejtés: {len(mapping)} név/rövidítés magyarosan, {changed} sorban")
    return mapping


def shorten_ollama(line, target_chars, model, src_lang="en", formality="auto", notes=""):
    """Rövidebb magyar változat egy túl hosszú sorhoz."""
    system = _system_prompt(src_lang, formality, notes)
    user = ("This Hungarian dubbing line is too long to fit its time slot. Rephrase it to about "
            f"{target_chars} characters (not much shorter). Keep the full meaning of the original and a natural "
            "spoken Hungarian style: shorten by rephrasing and dropping filler words, not by dropping content "
            "or changing what is said.\n"
            + json.dumps([{"id": line["id"], "max_chars": target_chars, "text": line["en"],
                           "current_hungarian": line["hu"]}], ensure_ascii=False))
    data = _chat(model, system, user, timeout=180)
    return clean_hu(data["translations"][0]["hu"])


def clean_hu(t):
    t = t.strip().strip('"„”“').strip()
    t = re.sub(r"\s*\([^)]*\)", "", t)          # zárójeles megjegyzések
    return re.sub(r"\s+", " ", t).strip()


# -----------------------------------------------------------------------------
# DeepL
# -----------------------------------------------------------------------------

def load_deepl_key(settings):
    k = (settings.get("deepl_key") or "").strip()
    if k:
        return k
    try:
        with open(RKSUBBY_DEEPL_KEY, encoding="utf-8") as f:   # az rksubby már elmentett kulcsa
            return f.read().strip()
    except OSError:
        return ""


def translate_deepl(lines, key, src_lang="en", formality="auto", log=print, progress=None):
    host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    headers = {"Authorization": f"DeepL-Auth-Key {key}"}
    todo = [ln for ln in lines if not ln.get("hu")]
    for b in range(0, len(todo), 40):
        chunk = todo[b:b + 40]
        ctx = " ".join(ln["en"] for ln in lines if ln["id"] < chunk[0]["id"])[-1500:]
        body = {"text": [ln["en"] for ln in chunk], "target_lang": "HU", "split_sentences": "nonewlines"}
        if src_lang:
            body["source_lang"] = src_lang.upper()
        if formality in ("formal", "informal"):
            body["formality"] = "prefer_more" if formality == "formal" else "prefer_less"
        if ctx:
            body["context"] = ctx
        r = requests.post(host + "/v2/translate", json=body, headers=headers, timeout=60)
        if r.status_code == 403:
            raise RuntimeError("a DeepL elutasította a kulcsot (403)")
        if r.status_code == 456:
            raise RuntimeError("elfogyott a havi DeepL-keret (456)")
        r.raise_for_status()
        for ln, t in zip(chunk, r.json()["translations"]):
            ln["hu"] = clean_hu(t["text"])
        if progress:
            progress(min(1.0, (b + len(chunk)) / len(todo)))
    log(f"  DeepL: {len(todo)} sor lefordítva")
    return lines
