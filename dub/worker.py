# -*- coding: utf-8 -*-
"""Egy szinkron futtatása külön folyamatban: a végén minden videómemória felszabadul (a következő
futásnál a fordító AI-nak is van helye), és egy esetleges összeomlás nem viszi magával a felületet."""

import multiprocessing
import queue
import traceback

_CTX = multiprocessing.get_context("spawn")


def _run(src, options, job_dir, q, cancel, online=False):
    import os

    from .util import is_offline_error, short_err, use_offline_models

    if online:
        os.environ["RKDUB_ONLINE"] = "1"
    offline = use_offline_models()
    from .pipeline import Cancelled, DubJob

    try:
        job = DubJob(src, options, log=lambda m: q.put(("log", m)),
                     progress=lambda f, d="": q.put(("p", f, d)), cancel=cancel, job_dir=job_dir)
        q.put(("done", job.run()))
    except Cancelled:
        q.put(("err", "Megszakítva. Ha újraindítod, onnan folytatja, ahol abbahagyta."))
    except Exception as e:
        if offline and is_offline_error(e):
            q.put(("retry_online", short_err(e)))   # hiányzó modell: újra, letöltéssel
            return
        traceback.print_exc()
        q.put(("log", "HIBA: " + short_err(e)))
        q.put(("err", short_err(e)))


class JobProcess:
    """Elindítja a munkát, és eseményeket ad vissza: ("log", szöveg), ("p", arány, leírás),
    ("done", eredmény) vagy ("err", hibaüzenet)."""

    def __init__(self, src, options, job_dir):
        self.args = (src, options, job_dir)
        self.q = _CTX.Queue()
        self.cancel = _CTX.Event()
        self.finished = False
        self._start(online=False)

    def _start(self, online):
        self.proc = _CTX.Process(target=_run, args=(*self.args, self.q, self.cancel, online), daemon=True)
        self.proc.start()

    def stop(self):
        self.cancel.set()

    def events(self, timeout=0.5):
        """Az eddig érkezett események; ha a folyamat jelzés nélkül állt le, hibát ad."""
        out = []
        try:
            out.append(self.q.get(timeout=timeout))
            while True:
                out.append(self.q.get_nowait())
        except queue.Empty:
            pass
        if any(e[0] == "retry_online" for e in out):
            self.proc.join(5)
            self._start(online=True)
            out = [e for e in out if e[0] != "retry_online"]
            out.append(("log", "Hiányzó modell: letöltöm (internet kell hozzá), és folytatom..."))
            return out
        if any(e[0] in ("done", "err") for e in out):
            self.finished = True
        elif not self.proc.is_alive() and self.q.empty():
            self.finished = True
            out.append(("err", f"a feldolgozó folyamat váratlanul leállt (kilépési kód: {self.proc.exitcode})"))
        return out
