# -*- coding: utf-8 -*-
"""Grafikus felület (Gradio, a böngészőben vagy Edge-alkalmazásablakban)."""

import os
import shutil
import subprocess
import threading
import time
import webbrowser

import gradio as gr

from . import media
from . import script
from . import translate as tr
from .pipeline import DEFAULT_OPTIONS, QC_FLAG, DubJob
from .util import NO_WINDOW, OUT_DIR, ROOT, WORK_DIR, fmt_time, load_settings, read_json, save_settings
from .worker import JobProcess

TRANSLATORS = {"Helyi AI (Ollama)": "ollama", "DeepL": "deepl"}
FORMALITY = {"Automatikus": "auto", "Tegező": "informal", "Magázó": "formal"}
REF_MODES = {"Magyarosított (akcentus nélkül, ajánlott)": "hu", "Soronként (az eredeti hangsúllyal)": "line",
             "Beszélőnként (angol mintából)": "speaker"}
SPEAKERS = ["Automatikus", "1", "2", "3", "4", "5", "6"]
TABLE_HEADERS = ["#", "Idő", "Beszélő", "Eredeti", "Magyar (felirat)", "Kiejtés (ha eltér)", "Ellenőrzés"]


def _inv(d, v, default):
    return next((k for k, x in d.items() if x == v), default)


class App:
    def __init__(self):
        self.proc = None            # a futó munka (külön folyamat)
        self.job = None             # a legutóbbi munka mappája (a táblázathoz, meghallgatáshoz)
        self.opts = None
        self.exported = ""          # a legutóbb exportált szöveg (ehhez képest látszik, ha beillesztettek valamit)
        self.settings = dict(DEFAULT_OPTIONS, **load_settings())

    # --- beállítások ------------------------------------------------------------
    def options_from(self, translator, model, deepl_key, formality, speakers, ref_mode, notes,
                     keep_bg, keep_ns, keep_orig, subs, stop_after):
        o = {"translator": TRANSLATORS.get(translator, "ollama"), "ollama_model": model or "",
             "deepl_key": (deepl_key or "").strip(), "formality": FORMALITY.get(formality, "auto"),
             "num_speakers": 0 if speakers == "Automatikus" else int(speakers),
             "ref_mode": REF_MODES.get(ref_mode, "hu"), "notes": notes or "",
             "keep_background": bool(keep_bg), "keep_nonspeech": bool(keep_ns),
             "keep_original_track": bool(keep_orig), "subtitles": bool(subs),
             "stop_after_translate": bool(stop_after)}
        self.settings.update(o)
        if o["deepl_key"] == tr.load_deepl_key({}):     # az rksubby kulcsa: nem kell még egy példány belőle
            self.settings.pop("deepl_key", None)
        save_settings(self.settings)
        return o

    # --- futtatás ---------------------------------------------------------------
    def run(self, upload, path, *opts, progress=gr.Progress()):
        src = (path or "").strip().strip('"') or (upload if isinstance(upload, str) else getattr(upload, "name", ""))
        if not src or not os.path.isfile(src):
            raise gr.Error("Válassz ki egy videót vagy hangfájlt (húzd be, vagy add meg az elérési útját).")
        if not media.is_media(src):
            raise gr.Error("Ezt a fájltípust nem ismerem. Videó (mp4, mkv, mov, webm...) vagy hang (wav, mp3, m4a...) kell.")
        o = self.options_from(*opts)
        self.job = DubJob(src, o)          # csak a munkamappa; a munka külön folyamatban fut
        self.opts = o
        yield from self._drive(src, o, progress, "Indul...")

    def _drive(self, src, o, progress, first_desc, done_msg=None):
        """Elindítja a munkát külön folyamatban, és közben frissíti a felületet."""
        if self.proc and not self.proc.finished:
            raise gr.Error("Már fut egy szinkron. Várd meg, vagy szakítsd meg.")
        logs, state = [], {"p": 0.0, "desc": first_desc, "err": None, "res": None}
        self.proc = JobProcess(src, o, self.job.dir)
        t0 = time.time()
        while not self.proc.finished:
            for ev in self.proc.events(0.5):
                if ev[0] == "log":
                    logs.append(f"{time.strftime('%H:%M:%S')}  {ev[1]}")
                    print(ev[1], flush=True)
                elif ev[0] == "p":
                    state["p"], state["desc"] = ev[1], ev[2] or state["desc"]
                elif ev[0] == "done":
                    state["res"] = ev[1]
                elif ev[0] == "err":
                    state["err"] = ev[1]
            progress(state["p"], desc=state["desc"])
            yield (self._status(state["desc"], state["p"], t0), "\n".join(logs[-400:]),
                   gr.update(), gr.update(), gr.update(), gr.update())
        if state["err"] or not state["res"]:
            yield (f"### ⚠️ {state['err'] or 'ismeretlen hiba'}", "\n".join(logs[-400:]), gr.update(),
                   gr.update(), gr.update(), gr.update(value=self.table(), visible=bool(self.table())))
            return
        out = self._finished(state["res"], logs, t0)
        yield ((done_msg.format(t=fmt_time(time.time() - t0)),) + out[1:]) if done_msg else out

    def _status(self, desc, p, t0):
        return f"### ⏳ {desc}  \n{p * 100:.0f}% · eltelt {fmt_time(time.time() - t0)}"

    def _finished(self, res, logs, t0):
        if res.get("stopped"):                 # a fordítás után megállt: átnézés, export/import jön
            msg = (f"### 📝 A fordítás kész ({res.get('lines', 0)} sor, {fmt_time(time.time() - t0)})  \n"
                   "Nézd át lent a táblázatban, vagy **exportáld** javításra (pl. Claude-nak), "
                   "aztán nyomd meg a **🔁 Hang készítése** gombot.")
            return (msg, "\n".join(logs[-400:]), gr.update(), gr.update(), gr.update(),
                    gr.update(value=self.table(), visible=True))
        out = res.get("output", "")
        files = [f for f in (out, res.get("srt")) if f and os.path.exists(f)]
        is_video = res.get("has_video")
        msg = (f"### ✅ Kész! ({fmt_time(time.time() - t0)})  \n`{out}`  \n"
               f"{res.get('dubbed', 0)}/{res.get('lines', 0)} sor szinkronizálva.")
        return (msg, "\n".join(logs[-400:]),
                gr.update(value=out if is_video else None, visible=bool(is_video)),
                gr.update(value=None if is_video else out, visible=not is_video),
                gr.update(value=files),
                gr.update(value=self.table(), visible=True))

    def stop(self):
        if not self.proc or self.proc.finished:
            return gr.update()
        self.proc.stop()
        return "### ⏹ Megszakítás... (a folyamatban lévő lépés végén áll meg)"

    # --- szövegszerkesztő ---------------------------------------------------------
    def table(self):
        if not self.job:
            return []
        rows = []
        for ln in self.job.lines():
            note = ""
            if ln.get("qc_cer", 0) > QC_FLAG:
                note = f"⚠ eltér ({ln.get('qc_heard', '')})"
            elif ln.get("fits") is False:
                note = f"⏱ hosszú ({ln.get('tts_dur', 0):.1f}/{ln.get('avail', 0):.1f} mp)"
            elif ln.get("edited"):
                note = "✎ javítva"
            rows.append([ln["id"] + 1, fmt_time(ln["start"]), ln.get("speaker", 0) + 1, ln["en"],
                         ln.get("hu", ""), ln.get("say", ""), note])
        return rows

    def preview(self, evt: gr.SelectData):
        if not self.job:
            return None, None
        row = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        lines = self.job.lines()
        if row is None or row >= len(lines):
            return None, None
        ln = lines[row]
        import numpy as np

        y, sr = media.read_audio(self.job.p("audio.wav"))
        a, b = int(max(0, ln["start"] - 0.2) * sr), int((ln["end"] + 0.3) * sr)
        orig = (sr, (np.clip(y[a:b], -1, 1) * 32767).astype(np.int16))
        f = ln.get("tts_file")
        hu = self.job.p(f) if f and os.path.exists(self.job.p(f)) else None
        return orig, hu

    def regenerate(self, rows, progress=gr.Progress()):
        if not self.job or not self.opts:
            raise gr.Error("Előbb futtass le egy szinkront.")
        lines = self.job.lines()
        data = rows.values.tolist() if hasattr(rows, "values") else rows
        changed = 0
        for r in data:
            try:
                i = int(r[0]) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(lines):
                new = str(r[4] or "").strip()
                say = str(r[5] or "").strip()
                if say != (lines[i].get("say") or ""):          # a kiejtést írták át
                    if say:
                        lines[i]["say"], lines[i]["say_edited"] = say, True
                    else:
                        lines[i].pop("say", None)
                        lines[i].pop("say_edited", None)
                    changed += 1
                if new and new != lines[i].get("hu"):           # a szöveget írták át
                    lines[i]["hu"], lines[i]["edited"] = new, True
                    if not lines[i].get("say_edited"):          # a régi kiejtés már nem érvényes
                        lines[i].pop("say", None)
                    changed += 1
        self.job.save_lines(lines)
        o = dict(self.opts, stop_after_translate=False)     # innen már a hang is elkészül
        yield from self._drive(self.job.src, o, progress, f"{changed} módosított sor, hang készítése...",
                               done_msg=f"### ✅ Kész: {changed} módosított sor ({{t}})")

    # --- export / import ----------------------------------------------------------
    def export_script(self):
        if not self.job or not os.path.exists(self.job.p("lines.json")):
            raise gr.Error("Előbb futtass le egy szinkront (vagy legalább a fordítást).")
        lang = (read_json(self.job.p("words.json")) or {}).get("lang", "en")
        path, text = script.export_script(self.job, lang)
        self.exported = text
        return (text, path, gr.update(open=True),
                f"### ⬇ Exportálva: `{path}`  \nMásold ki a szöveget (vagy add oda a fájlt) pl. a Claude-nak ezzel: "
                "*„Javítsd ki a magyar szinkronszöveget a fájl elején lévő utasítások szerint, és add vissza a "
                "teljes fájlt.”* A javítottat illeszd vissza ide, és nyomd meg az **Importálás** gombot.")

    def import_script(self, pasted, uploaded):
        if not self.job:
            raise gr.Error("Előbb futtass le egy szinkront (vagy legalább a fordítást).")
        src_name, text = "", ""
        if uploaded:                                         # 1. feltöltött fájl
            with open(uploaded, encoding="utf-8-sig") as f:
                text, src_name = f.read(), os.path.basename(uploaded)
        elif (pasted or "").strip() and pasted.strip() != self.exported.strip():   # 2. beillesztett szöveg
            text, src_name = pasted, "a beillesztett szöveg"
        else:                                                # 3. a kesz mappában lévő (pl. kívülről javított) fájl
            path = script.export_path(self.job)
            if not os.path.exists(path):
                raise gr.Error("Nincs mit importálni: illeszd be a javított szöveget, vagy tölts fel egy fájlt.")
            with open(path, encoding="utf-8-sig") as f:
                text, src_name = f.read(), os.path.basename(path)
        try:
            st = script.apply_script(self.job, text)
        except ValueError as e:
            raise gr.Error(f"Ezt nem tudtam beolvasni: {e}")
        extra = []
        if st["say"]:
            extra.append(f"{st['say']} kiejtés")
        if st["missing"]:
            extra.append(f"{st['missing']} sor hiányzott belőle (azok változatlanok)")
        if st["unknown"]:
            extra.append(f"ismeretlen sorszám: {st['unknown'][:10]}")
        msg = (f"### ⬆ Importálva ({src_name}): {st['changed']} sor szövege módosult"
               + (", " + ", ".join(extra) if extra else "") + ".  \nNyomd meg a **🔁 Hang készítése** gombot.")
        return msg, gr.update(value=self.table(), visible=True), None


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)   # noqa: S606


def browser_open(url):
    """Edge-alkalmazásablakban nyílik (mint egy önálló program), különben a böngészőben."""
    for exe in (shutil.which("msedge"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")):
        if exe and os.path.exists(exe):
            try:
                prof = os.path.join(os.path.expanduser("~"), ".rkdub", "edge")
                subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}", "--window-size=1400,950",
                                  "--no-first-run", "--no-default-browser-check"], creationflags=NO_WINDOW)
                return
            except OSError:
                pass
    webbrowser.open(url)


CSS = """
.gradio-container {max-width: 1400px !important; margin: auto;}
#title h1 {margin-bottom: 0;}
#run-btn {min-height: 56px; font-size: 1.15em;}
#log textarea {font-family: Consolas, monospace; font-size: 12px;}
footer {display: none !important;}
"""


def build():
    app = App()
    s = app.settings
    models = tr.ollama_models()
    model = s.get("ollama_model") if s.get("ollama_model") in models else tr.pick_default_model(models)
    deepl_key = tr.load_deepl_key(s)

    with gr.Blocks(title="rkdub – magyar szinkron") as demo:
        gr.Markdown("# 🎙️ rkdub – magyar szinkron\nAngol (vagy más nyelvű) videó vagy hangfájl → magyar szinkron "
                    "az eredeti beszélők hangján. Minden a saját gépeden fut, a videókártyádon.", elem_id="title")
        with gr.Row(equal_height=False):
            with gr.Column(scale=5):
                upload = gr.File(label="Videó vagy hangfájl (húzd ide)", type="filepath",
                                 file_types=sorted(media.VIDEO_EXT | media.AUDIO_EXT), height=170)
                path = gr.Textbox(label="...vagy a fájl elérési útja (nagy fájlnál gyorsabb, nem kell feltölteni)",
                                  placeholder=r"pl. D:\Videók\film.mp4", max_lines=1)
                with gr.Accordion("Beállítások", open=False):
                    with gr.Row():
                        translator = gr.Radio(list(TRANSLATORS), label="Fordító",
                                              value=_inv(TRANSLATORS, s.get("translator"), "Helyi AI (Ollama)"))
                        formality = gr.Radio(list(FORMALITY), label="Megszólítás",
                                             value=_inv(FORMALITY, s.get("formality"), "Automatikus"))
                    with gr.Row():
                        ollama_model = gr.Dropdown(models or [""], value=model or None, label="Helyi AI-modell (Ollama)",
                                                   allow_custom_value=True,
                                                   visible=s.get("translator", "ollama") == "ollama")
                        deepl = gr.Textbox(value=deepl_key, label="DeepL-kulcs", type="password",
                                           visible=s.get("translator") == "deepl")
                    with gr.Row():
                        speakers = gr.Dropdown(SPEAKERS, label="Beszélők száma",
                                               value=str(s.get("num_speakers")) if s.get("num_speakers") else "Automatikus")
                        ref_mode = gr.Radio(list(REF_MODES), label="Hangminta",
                                            value=_inv(REF_MODES, s.get("ref_mode"), list(REF_MODES)[0]))
                    notes = gr.Textbox(value=s.get("notes", ""), lines=2, label="Megjegyzés a fordítónak (nem kötelező)",
                                       placeholder="pl. Ez egy főzős videó. A „Gordon” név maradjon. A csatorna neve: Konyhakaland.")
                    with gr.Row():
                        keep_bg = gr.Checkbox(s.get("keep_background", True), label="Háttérzene és zajok megtartása")
                        keep_ns = gr.Checkbox(s.get("keep_nonspeech", True), label="Nevetés, kiáltás, sóhaj megtartása")
                    with gr.Row():
                        keep_orig = gr.Checkbox(s.get("keep_original_track", True), label="Eredeti hang 2. hangsávként")
                        subs = gr.Checkbox(s.get("subtitles", False), label="Magyar felirat beágyazása a videóba")
                    stop_after = gr.Checkbox(s.get("stop_after_translate", False),
                                             label="Megállás a fordítás után (átnézés, export/import a hang előtt)")
                with gr.Row():
                    run_btn = gr.Button("▶ Szinkronizálás", variant="primary", elem_id="run-btn", scale=4)
                    stop_btn = gr.Button("⏹ Megszakítás", scale=1)
                status = gr.Markdown("")
                log = gr.Textbox(label="Napló", lines=10, max_lines=10, autoscroll=True, elem_id="log",
                                 interactive=False)
            with gr.Column(scale=6):
                video = gr.Video(label="Eredmény", interactive=False, height=420)
                audio = gr.Audio(label="Eredmény", interactive=False, visible=False, type="filepath")
                files = gr.File(label="Letöltés", file_count="multiple", interactive=False, height=110)
                with gr.Row():
                    gr.Button("📂 Kész fájlok mappája", size="sm").click(lambda: open_folder(OUT_DIR))
                    gr.Button("🗂 Munkamappák (újrakezdéshez törölhető)", size="sm").click(lambda: open_folder(WORK_DIR))

        with gr.Accordion("📝 Szöveg ellenőrzése és javítása", open=True):
            gr.Markdown("Kattints egy sorra a meghallgatásához. A **Magyar** (felirat) és a **Kiejtés** oszlop "
                        "átírható; utána nyomd meg a **🔁 Hang készítése** gombot: csak a módosított sorok készülnek "
                        "el újra. A *Kiejtés* azt mutatja, hogyan mondja ki a hang a neveket, rövidítéseket "
                        "(pl. „Xbox” → „ikszboksz”, „AMD” → „á-em-dé”); ha üres, a felirat szerint olvas.")
            with gr.Row():
                orig_clip = gr.Audio(label="Eredeti", interactive=False)
                hu_clip = gr.Audio(label="Magyar", interactive=False, type="filepath")
            table = gr.Dataframe(headers=TABLE_HEADERS,
                                 datatype=["number", "str", "number", "str", "str", "str", "str"],
                                 interactive=True, wrap=True, static_columns=[0, 1, 2, 3, 6], max_height=520,
                                 column_widths=["4%", "7%", "6%", "26%", "26%", "21%", "10%"], visible=False)
            with gr.Row():
                regen = gr.Button("🔁 Hang készítése / újragenerálás (a módosított sorokkal)", variant="primary",
                                  scale=3)
                export_btn = gr.Button("⬇ Exportálás javításra", scale=1)
                import_btn = gr.Button("⬆ Javított importálása", scale=1)
            with gr.Accordion("Export / import: a fordítás javíttatása (pl. Claude-dal)", open=False) as xio:
                gr.Markdown(
                    "1. **⬇ Exportálás**: a teljes szöveg (angol eredeti, magyar, időkeret) ide és a `kesz` mappába "
                    "kerül egy `..._forditas.txt` fájlba, az elején egy rövid utasítással egy AI-nak.\n"
                    "2. Másold ki a szöveget (a doboz jobb felső sarkában van a másolás gomb), vagy add oda a fájlt "
                    "pl. a Claude-nak, és kérd meg, hogy javítsa ki.\n"
                    "3. A javított szöveget **illeszd be ide** (vagy töltsd fel a fájlt), és nyomd meg a "
                    "**⬆ Javított importálása** gombot. Ha a `kesz` mappában lévő fájlt javíttatod (pl. a Claude "
                    "Code-dal), az importálás magától onnan olvas.\n"
                    "4. **🔁 Hang készítése**: csak a megváltozott sorok készülnek el újra.")
                script_box = gr.Textbox(label="Szinkronszöveg (másolható; ide illeszd be a javítottat)", lines=14,
                                        max_lines=40, buttons=["copy"])
                with gr.Row():
                    script_upload = gr.File(label="...vagy a javított fájl feltöltése (.txt)", type="filepath",
                                            file_types=[".txt", ".md"], height=110)
                    script_file = gr.File(label="Exportált fájl", interactive=False, height=110)

        opts = [translator, ollama_model, deepl, formality, speakers, ref_mode, notes, keep_bg, keep_ns,
                keep_orig, subs, stop_after]
        translator.change(lambda t: (gr.update(visible=t == "Helyi AI (Ollama)"), gr.update(visible=t == "DeepL")),
                          translator, [ollama_model, deepl])
        upload.change(lambda f: gr.update(value="") if f else gr.update(), upload, path)
        outs = [status, log, video, audio, files, table]
        # a folyamatjelző a lejátszó helyén látszik, a napló közben olvasható marad
        run_btn.click(app.run, [upload, path, *opts], outs, show_progress_on=[video])
        stop_btn.click(app.stop, None, status)
        table.select(app.preview, None, [orig_clip, hu_clip])
        regen.click(app.regenerate, table, outs, show_progress_on=[video])
        export_btn.click(app.export_script, None, [script_box, script_file, xio, status])
        import_btn.click(app.import_script, [script_box, script_upload], [status, table, script_upload])

        def refresh_models(cur):     # oldalbetöltéskor: az azóta letöltött Ollama-modellek is látszanak
            ms = tr.ollama_models()
            return gr.update(choices=ms or [""], value=cur if cur in ms else (tr.pick_default_model(ms) or None))

        demo.load(refresh_models, ollama_model, ollama_model)
    return demo


def _already_running(port):
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch(port=7870, open_browser=True):
    url = f"http://127.0.0.1:{port}/"
    if _already_running(port):        # már fut (pl. kétszer kattintottak az ikonra): csak az ablakot nyitjuk meg
        print(f"Az rkdub már fut: {url}", flush=True)
        if open_browser:
            browser_open(url)
        return
    demo = build()
    if open_browser:
        threading.Timer(2.5, browser_open, args=(url,)).start()
    print(f"rkdub fut: {url}  (bezárás: ez az ablak)", flush=True)
    demo.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1", server_port=port, inbrowser=False, show_error=True,
        allowed_paths=[OUT_DIR, WORK_DIR, ROOT], css=CSS, theme=gr.themes.Soft(primary_hue="orange"),
        favicon_path=os.path.join(ROOT, "rkdub.ico") if os.path.exists(os.path.join(ROOT, "rkdub.ico")) else None)
