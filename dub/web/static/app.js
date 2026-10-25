// rkdub felület: a helyi API (dub/webapp.py) hívása, állapot követése, szövegszerkesztő.
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icons = () => lucide.createIcons();

let logTotal = 0;
let lastStatus = "idle";
let lines = [];              // a szerkesztő sorai (eredeti értékekkel a változás figyeléséhez)
let listenTrack = "hu";
let previewTrack = "hu";
let player = new Audio();
let playingNo = null;
let previewKey = "";

// ---------------------------------------------------------------- segédek
async function api(path, opts = {}) {
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch { data = null; }
    if (!r.ok) throw new Error((data && data.detail) || `${r.status} ${r.statusText}`);
    return data;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

function toast(msg, kind = "info") {
    const colors = { info: "border-brand-500/40 text-gray-200", ok: "border-emerald-500/50 text-emerald-300", err: "border-red-500/50 text-red-300" };
    const icon = { info: "info", ok: "check-circle-2", err: "alert-triangle" }[kind];
    const el = document.createElement("div");
    el.className = `bg-dark-card border ${colors[kind]} rounded-xl px-4 py-3 text-xs shadow-2xl flex gap-2 items-start transition-opacity duration-500`;
    el.innerHTML = `<i data-lucide="${icon}" class="w-4 h-4 shrink-0 mt-0.5"></i><span>${esc(msg)}</span>`;
    $("toasts").appendChild(el);
    icons();
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 600); }, kind === "err" ? 9000 : 5000);
}

function fmtSize(b) {
    if (b > 1 << 30) return (b / (1 << 30)).toFixed(1) + " GB";
    if (b > 1 << 20) return (b / (1 << 20)).toFixed(1) + " MB";
    return Math.max(1, Math.round(b / 1024)) + " KB";
}

// ---------------------------------------------------------------- beállítások
const TOGGLES = [
    ["keep_background", "Háttérzene és zajok megtartása"],
    ["keep_nonspeech", "Nevetés, kiáltás, sóhaj megtartása (az eredetiből)"],
    ["keep_original_track", "Eredeti hang 2. hangsávként a videóban"],
    ["subtitles", "Magyar felirat beágyazása a videóba"],
    ["stop_after_translate", "Megállás a fordítás után (átnézés, export/import a hang előtt)"],
];

function buildToggles() {
    $("toggles").innerHTML = TOGGLES.map(([id, label]) => `
        <label class="flex items-center justify-between gap-3 cursor-pointer">
            <span class="text-sm text-gray-300">${label}</span>
            <span class="relative inline-flex items-center shrink-0">
                <input type="checkbox" id="${id}" class="opt sr-only peer">
                <span class="w-10 h-[22px] bg-black/40 border border-dark-border rounded-full peer peer-checked:after:translate-x-[18px] after:content-[''] after:absolute after:top-[3px] after:left-[3px] after:bg-gray-300 peer-checked:after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-brand-500 peer-checked:border-brand-400 peer-checked:shadow-[0_0_10px_rgba(255,107,0,.6)] transition-colors"></span>
            </span>
        </label>`).join("");
}

// csúszka: narancs kitöltés a hüvelykujjig
function fillRange(el) {
    const min = parseFloat(el.min || 0), max = parseFloat(el.max || 100);
    el.style.setProperty("--p", ((parseFloat(el.value) - min) / (max - min || 1)) * 100 + "%");
}

function buildTicks() {
    $("spk-ticks").innerHTML = Array.from({ length: 11 }, (_, i) =>
        `<span data-n="${i}" style="left:${i * 10}%">${i ? i : "AUTO"}</span>`).join("");
}

// gyakori megjegyzések a fordítónak (a sparkles gomb alatt)
const NOTE_SNIPPETS = [
    "Laza, tegeződő YouTuber-stílus.",
    "A nevek, márka- és terméknevek maradjanak eredetiben.",
    "Ahol van bevett magyar szakszó, azt használd.",
    "A káromkodást tompítsd.",
    "Gyerekeknek szól: egyszerű, rövid mondatok.",
    "Hivatalos, tárgyilagos hangnem (pl. bemutató, hírek).",
];

function buildSnippets() {
    $("notes-snips").innerHTML = NOTE_SNIPPETS.map((s, i) =>
        `<button data-snip="${i}" class="w-full text-left text-sm text-gray-200 px-2.5 py-1.5 rounded-lg hover:bg-brand-500/15 hover:text-white transition-colors">${esc(s)}</button>`).join("");
    $("notes-snips").querySelectorAll("[data-snip]").forEach((b) => b.addEventListener("click", () => {
        const n = $("notes"), cur = n.value.trim();
        const s = NOTE_SNIPPETS[+b.dataset.snip];
        if (!cur.includes(s)) n.value = cur ? cur.replace(/[.!]?$/, ".") + " " + s : s;
        growNotes();
        $("notes-menu").classList.add("hidden");
        saveSettings();
    }));
}

function growNotes() { const n = $("notes"); n.style.height = "auto"; n.style.height = n.scrollHeight + 2 + "px"; }

function fillSelect(id, items, value) {
    $(id).innerHTML = items.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
    if (value !== undefined && value !== null) $(id).value = value;
}

function options() {
    const o = {};
    for (const id of ["translator", "ollama_model", "deepl_key", "formality", "ref_mode", "asr_model", "notes"]) o[id] = $(id).value;
    o.num_speakers = parseInt($("num_speakers").value || "0", 10);
    o.speaker_genders = o.num_speakers ? genders.slice(0, o.num_speakers) : [];
    o.tempo = parseFloat($("tempo").value);
    for (const [id] of TOGGLES) o[id] = $(id).checked;
    return o;
}

// ---------------------------------------------------------------- szereplők száma és neme
// A nemeket darabszámként lehet megadni (pl. 2 nő, 3 férfi); a háttérnek listaként megy: ["f","f","m","m","m"].
let genders = [];

function genderCounts() {
    return { f: genders.filter((g) => g === "f").length, m: genders.filter((g) => g === "m").length };
}

function setGenders(nf, nm) {
    genders = Array(nf).fill("f").concat(Array(nm).fill("m"));
}

function renderGenders() {
    const n = parseInt($("num_speakers").value || "0", 10);
    $("spk-label").textContent = n ? `${n} szereplő` : "Automatikus";
    fillRange($("num_speakers"));
    document.querySelectorAll("#spk-ticks span").forEach((s) => s.classList.toggle("on", +s.dataset.n === n));
    const box = $("genders");
    if (!n) {
        box.innerHTML = `<p class="col-span-full text-xs text-gray-500">A program maga dönti el, hány szereplő van. Ha tudod a számukat (és hogy hány nő és férfi), állítsd be: pontosabb lesz, és minden szereplő végig ugyanazon a hangon szól.</p>`;
        return;
    }
    let { f, m } = genderCounts();
    if (f + m > n) { m = Math.max(0, n - f); f = Math.min(f, n); setGenders(f, m); }   // a csúszkát lejjebb húzták
    const rest = n - f - m;
    box.innerHTML = `
        <label class="flex items-center justify-between gap-2 bg-black/30 border border-dark-border rounded-lg px-3 py-1.5 col-span-1 sm:col-span-2">
            <span class="text-sm text-pink-300">Nő</span>
            <input id="g-f" type="number" min="0" max="10" value="${f}" class="w-16 bg-dark-base border border-dark-border rounded-md px-2 py-1 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-500">
        </label>
        <label class="flex items-center justify-between gap-2 bg-black/30 border border-dark-border rounded-lg px-3 py-1.5 col-span-1 sm:col-span-2">
            <span class="text-sm text-sky-300">Férfi</span>
            <input id="g-m" type="number" min="0" max="10" value="${m}" class="w-16 bg-dark-base border border-dark-border rounded-md px-2 py-1 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-500">
        </label>
        <p class="col-span-full text-xs text-gray-500">${rest ? `${rest} szereplő nemét a program dönti el.` : "Mindegyik szereplő neme megadva."}</p>`;
    const onChange = () => {
        const nf = Math.max(0, Math.min(10, parseInt($("g-f").value || "0", 10)));
        const nm = Math.max(0, Math.min(10 - nf, parseInt($("g-m").value || "0", 10)));
        setGenders(nf, nm);
        if (nf + nm > parseInt($("num_speakers").value, 10)) $("num_speakers").value = String(nf + nm);   // a csúszka követi
        renderGenders();
        saveSettings();
    };
    $("g-f").addEventListener("change", onChange);
    $("g-m").addEventListener("change", onChange);
}

function updateDependent() {
    const deepl = $("translator").value === "deepl";
    $("model-wrap").classList.toggle("hidden", deepl);
    $("deepl-wrap").classList.toggle("hidden", !deepl);
    const t = parseFloat($("tempo").value);
    $("tempo-label").textContent = t.toFixed(2) + "x" + (Math.abs(t - 1) < 0.01 ? " (természetes)" : t > 1 ? " (gyorsabb)" : " (lassabb)");
    fillRange($("tempo"));
}

let saveTimer = null;
function saveSettings() {
    updateDependent();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => post("/api/settings", options()).catch(() => {}), 500);
}

async function init() {
    buildToggles();
    buildTicks();
    buildSnippets();
    const d = await api("/api/init");
    const s = d.settings;
    $("version").textContent = "v" + d.version;
    fillSelect("ref_mode", d.choices.ref_mode, s.ref_mode);
    fillSelect("asr_model", d.choices.asr_model, s.asr_model);
    fillSelect("translator", d.choices.translator, s.translator);
    fillSelect("formality", d.choices.formality, s.formality);
    const models = d.models.length ? d.models.map((m) => [m, m]) : [["", "(az Ollama nem fut, vagy nincs modell)"]];
    fillSelect("ollama_model", models, s.ollama_model || "");
    $("deepl_key").value = s.deepl_key || "";
    $("num_speakers").value = String(s.num_speakers || 0);
    genders = Array.isArray(s.speaker_genders) ? s.speaker_genders.slice() : [];
    renderGenders();
    $("num_speakers").addEventListener("input", () => { renderGenders(); saveSettings(); });
    $("tempo").value = s.tempo || 1.0;
    $("notes").value = s.notes || "";
    growNotes();
    for (const [id] of TOGGLES) $(id).checked = !!s[id];
    updateDependent();
    document.querySelectorAll(".opt").forEach((el) => el.addEventListener(el.tagName === "TEXTAREA" || el.type === "range" ? "input" : "change", saveSettings));
    showGpu(d.gpu);
    try { if (localStorage.getItem("rkdub.settingsOpen") === "0") toggleSettings(); } catch { }
}

function toggleSettings() {
    const hidden = $("settings-content").classList.toggle("hidden");
    $("settings-chevron").classList.toggle("rotate-180", hidden);
    try { localStorage.setItem("rkdub.settingsOpen", hidden ? "0" : "1"); } catch { }
}

// ---------------------------------------------------------------- GPU
const gpuHist = [];

function drawSpark() {
    const W = 86, H = 20, n = 40;
    const pts = gpuHist.slice(-n);
    if (pts.length < 2) return;
    const step = W / (n - 1), x0 = W - (pts.length - 1) * step;
    const xy = pts.map((v, i) => `${(x0 + i * step).toFixed(1)},${(H - 1 - (v / 100) * (H - 3)).toFixed(1)}`);
    $("spark-line").setAttribute("d", "M" + xy.join(" L"));
    $("spark-area").setAttribute("d", `M${x0.toFixed(1)},${H} L${xy.join(" L")} L${W},${H} Z`);
}

function showGpu(g) {
    if (!g || !g.ok) { $("gpu-name").textContent = (g && g.name) || "GPU"; return; }
    $("gpu-name").textContent = g.name.replace(/^NVIDIA GeForce /i, "NVIDIA ");
    $("gpu-util").textContent = g.util + "%";
    if (!gpuHist.length) for (let i = 0; i < 39; i++) gpuHist.push(g.util);   // induláskor is vonal legyen
    gpuHist.push(g.util);
    if (gpuHist.length > 60) gpuHist.shift();
    drawSpark();
    $("gpu-mem").textContent = `${g.mem_used.toFixed(1)} / ${g.mem_total.toFixed(1)} GB`;
    $("vram-bar").style.width = Math.min(100, (g.mem_used / (g.mem_total || 1)) * 100) + "%";
    const t = g.temp;
    const color = t >= 80 ? "text-red-400" : t >= 70 ? "text-amber-400" : "text-emerald-400";
    $("gpu-temp").textContent = `${t} °C`;
    $("gpu-temp").className = `text-sm font-mono font-semibold ${color}`;
    const ic = $("gpu-temp-icon");
    if (ic) ic.setAttribute("class", `lucide lucide-thermometer w-7 h-7 ${color}`);
}
async function pollGpu() {
    try { showGpu(await api("/api/gpu")); } catch { }
    setTimeout(pollGpu, 2000);
}

// ---------------------------------------------------------------- állapot, napló
const LEVEL = {
    info: ["text-blue-400", "INFO"], process: ["text-amber-400", "FOLYAMAT"], success: ["text-emerald-400", "KÉSZ"],
    warn: ["text-yellow-300", "FIGYELEM"], error: ["text-red-400", "HIBA"], detail: ["text-gray-500", "·"],
};

function appendLogs(items) {
    const box = $("log");
    if (logTotal === 0 && items.length) box.innerHTML = "";
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    for (const it of items) {
        const [cls, tag] = LEVEL[it.level] || LEVEL.process;
        const div = document.createElement("div");
        div.className = "text-gray-500";
        const msgCls = it.level === "error" ? "text-red-300" : it.level === "detail" ? "text-gray-500" : it.level === "success" ? "text-gray-300" : "text-gray-400";
        div.innerHTML = `[${esc(it.t)}] <span class="${cls}">${tag}</span> <span class="${msgCls}">${esc(it.msg)}</span>`;
        box.appendChild(div);
    }
    if (atBottom) box.scrollTop = box.scrollHeight;
}

const STATUS = {
    idle: ["bg-emerald-500", "bg-emerald-400", "Kész / tétlen", false],
    running: ["bg-brand-500", "bg-brand-500", "Dolgozik", true],
    done: ["bg-emerald-500", "bg-emerald-400", "Kész", false],
    review: ["bg-amber-400", "bg-amber-400", "Átnézésre vár", true],
    stopped: ["bg-gray-400", "bg-gray-400", "Megszakítva", false],
    error: ["bg-red-500", "bg-red-400", "Hiba", false],
};

async function pollState() {
    let st = null;
    try {
        st = await api("/api/state?since=" + logTotal);
    } catch {
        $("status-text").textContent = "A szerver nem válaszol";
        setTimeout(pollState, 2000);
        return;
    }
    if (st.log_total < logTotal) { logTotal = 0; $("log").innerHTML = ""; }
    appendLogs(st.logs);
    logTotal = st.log_total;

    const [dot, ping, text, anim] = STATUS[st.status] || STATUS.idle;
    $("status-dot").className = `relative inline-flex rounded-full h-3 w-3 ${dot}`;
    $("status-ping").className = `${anim ? "animate-ping" : "hidden"} absolute inline-flex h-full w-full rounded-full ${ping} opacity-75`;
    $("status-text").textContent = st.status === "running" ? `Dolgozik: ${st.desc}` : text;
    $("job-name").textContent = st.job || "";

    const running = st.status === "running";
    $("run").disabled = running;
    $("regen").disabled = running;
    if (running || st.status !== "idle") {
        $("progress-card").classList.remove("hidden");
        const pct = Math.round((st.progress || 0) * 100);
        $("progress-bar").style.width = pct + "%";
        $("progress-pct").textContent = pct + "%";
        $("progress-time").textContent = st.elapsed;
        $("progress-desc").textContent = running ? st.desc : text;
        const msg = $("progress-msg");
        if (!running && (st.message || st.error)) {
            msg.classList.remove("hidden");
            msg.className = "text-sm mt-2.5 " + (st.error ? "text-red-300" : st.status === "review" ? "text-amber-300" : "text-emerald-300");
            msg.textContent = st.error || st.message;
        } else {
            msg.classList.add("hidden");
        }
    }

    if (lastStatus === "running" && !running) {
        if (st.status === "done") toast(st.message || "Kész!", "ok");
        else if (st.status === "review") toast(st.message, "info");
        else if (st.error) toast(st.error, "err");
        await refreshAll();
    } else if (lastStatus === "idle" && st.has_lines && !lines.length && !running) {
        await refreshAll();           // oldal-újratöltés után: a legutóbbi munka visszatöltése
    }
    lastStatus = st.status;
    setTimeout(pollState, running ? 700 : 1500);
}

async function refreshAll() {
    await Promise.all([loadLines(), loadFiles()]);
}

// ---------------------------------------------------------------- bemenet
function setPath(p) { $("path").value = p || ""; }

function upload(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    $("upload-progress").classList.remove("hidden");
    xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        $("upload-bar").style.width = pct + "%";
        $("upload-text").textContent = `${file.name} · ${pct}% (${fmtSize(e.loaded)} / ${fmtSize(e.total)})`;
    };
    xhr.onload = () => {
        let d = {};
        try { d = JSON.parse(xhr.responseText); } catch { }
        if (xhr.status === 200) {
            setPath(d.path);
            $("upload-text").textContent = `${file.name} · feltöltve (${fmtSize(d.size)})`;
            toast("Feltöltve: " + file.name, "ok");
        } else {
            $("upload-progress").classList.add("hidden");
            toast(d.detail || "A feltöltés nem sikerült", "err");
        }
    };
    xhr.onerror = () => toast("A feltöltés nem sikerült", "err");
    xhr.send(fd);
}

// ---------------------------------------------------------------- futtatás
async function run() {
    const path = $("path").value.trim().replace(/^"|"$/g, "");
    if (!path) { toast("Válassz ki egy videót vagy hangfájlt (húzd be, tallózd ki, vagy írd be az útját).", "err"); return; }
    try {
        logTotal = 0;
        $("log").innerHTML = "";
        lines = [];
        renderRows();
        await post("/api/run", { path, options: options() });
        lastStatus = "running";
    } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------- szerkesztő
async function loadLines() {
    const d = await api("/api/lines");
    lines = d.lines.map((l) => ({ ...l, hu0: l.hu, say0: l.say, speaker0: l.speaker }));
    renderRows();
}

function autoGrow(el) { el.style.height = "auto"; el.style.height = el.scrollHeight + 2 + "px"; }

function renderRows() {
    const tb = $("rows");
    if (!lines.length) {
        tb.innerHTML = `<tr><td colspan="6" class="p-6 text-center text-gray-500">Még nincs szöveg. Indíts el egy szinkront (a „Megállás a fordítás után” beállítással a hang előtt átnézheted).</td></tr>`;
        updateDirty();
        return;
    }
    tb.innerHTML = lines.map((l, i) => `
        <tr data-i="${i}" class="hover:bg-dark-hover/60 transition-colors group ${l.note ? "bg-amber-500/5" : ""}">
            <td class="p-3 text-center font-mono ${l.note ? "text-amber-400" : "text-gray-500"} align-top">
                <div class="font-semibold">${String(l.no).padStart(2, "0")}</div>
                <select data-f="speaker" title="Beszélő (átírható: ki mondja ezt a sort)" class="spk mt-1 bg-dark-card border border-dark-border rounded-md text-[10px] text-gray-400 px-1 py-0.5 focus:outline-none focus:border-brand-500">
                    ${speakerOptions(l.speaker)}
                </select>
                ${l.note ? `<i data-lucide="alert-triangle" class="w-3.5 h-3.5 mx-auto mt-1 text-amber-400" title="${esc(l.note)}"></i>` : ""}
            </td>
            <td class="p-3 font-mono text-gray-400 align-top">${esc(l.time)}</td>
            <td class="p-3 text-gray-300 align-top">${esc(l.en)}${l.note ? `<div class="text-[10px] text-amber-400/80 mt-1">${esc(l.note)}</div>` : ""}</td>
            <td class="p-3 align-top">
                <textarea data-f="hu" rows="1" class="cell w-full bg-dark-card border ${l.edited ? "border-brand-500/40" : "border-dark-border"} rounded-lg px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-brand-500">${esc(l.hu)}</textarea>
            </td>
            <td class="p-3 align-top">
                <textarea data-f="say" rows="1" placeholder="Ugyanaz, mint a fordítás" class="cell w-full bg-dark-card border border-dark-border rounded-lg px-2.5 py-1.5 text-xs text-amber-200/80 font-mono focus:outline-none focus:border-amber-500/50">${esc(l.say)}</textarea>
            </td>
            <td class="p-3 text-center align-top">
                <button data-play="${l.no}" class="play p-1.5 bg-brand-500/10 text-brand-500 hover:bg-brand-500 hover:text-white rounded-lg transition-all" title="Lejátszás">
                    <i data-lucide="volume-2" class="w-4 h-4"></i>
                </button>
            </td>
        </tr>`).join("");
    tb.querySelectorAll("textarea.cell").forEach((ta) => {
        autoGrow(ta);
        ta.addEventListener("input", () => {
            autoGrow(ta);
            const l = lines[+ta.closest("tr").dataset.i];
            l[ta.dataset.f] = ta.value;
            ta.classList.toggle("dirty", ta.value.trim() !== (ta.dataset.f === "hu" ? l.hu0 : l.say0).trim());
            updateDirty();
        });
    });
    tb.querySelectorAll("select.spk").forEach((sel) => sel.addEventListener("change", () => {
        const l = lines[+sel.closest("tr").dataset.i];
        l.speaker = parseInt(sel.value, 10);
        sel.classList.toggle("dirty", l.speaker !== l.speaker0);
        updateDirty();
    }));
    tb.querySelectorAll("button.play").forEach((b) => b.addEventListener("click", () => playLine(+b.dataset.play, b)));
    icons();
    updateDirty();
    requestAnimationFrame(growAll);          // a végleges szélesség kialakulása után
}

function growAll() { document.querySelectorAll("textarea.cell").forEach(autoGrow); }

function speakerOptions(cur) {
    const n = Math.min(8, Math.max(2, ...lines.map((x) => x.speaker0 || 1)) + 1);   // +1: új szereplő is választható
    let s = "";
    for (let i = 1; i <= n; i++) s += `<option value="${i}" ${i === cur ? "selected" : ""}>B${i}</option>`;
    return s;
}

function dirtyLines() {
    return lines.filter((l) => l.hu.trim() !== (l.hu0 || "").trim() || l.say.trim() !== (l.say0 || "").trim()
        || l.speaker !== l.speaker0);
}
function updateDirty() {
    const n = dirtyLines().length;
    $("dirty-count").classList.toggle("hidden", !n);
    $("dirty-count").textContent = n ? `${n} módosított sor` : "";
}

function setListen(track) {
    listenTrack = track;
    const on = "listen px-3 py-1.5 bg-brand-500/20 text-brand-400 border border-brand-500/40 rounded-lg text-sm font-medium flex items-center gap-1.5";
    const off = "listen px-3 py-1.5 btn-ghost rounded-lg text-sm font-medium flex items-center gap-1.5";
    $("listen-hu").className = track === "hu" ? on : off;
    $("listen-orig").className = track === "orig" ? on : off;
}

function playLine(no, btn) {
    document.querySelectorAll("tr.row-playing").forEach((r) => r.classList.remove("row-playing"));
    document.querySelectorAll("button.play").forEach((b) => b.classList.remove("bg-brand-500", "text-white"));
    if (playingNo === no && !player.paused) { player.pause(); playingNo = null; return; }
    player.pause();
    player = new Audio(`/api/clip?no=${no}&track=${listenTrack}&t=${Date.now()}`);
    playingNo = no;
    btn.closest("tr").classList.add("row-playing");
    btn.classList.add("bg-brand-500", "text-white");
    player.onended = () => { btn.classList.remove("bg-brand-500", "text-white"); btn.closest("tr").classList.remove("row-playing"); playingNo = null; };
    player.play().catch(async () => {
        btn.classList.remove("bg-brand-500", "text-white");
        try { const r = await fetch(player.src); const d = await r.json(); toast(d.detail || "Nem lejátszható", "err"); }
        catch { toast("Nem lejátszható", "err"); }
    });
}

async function regenerate() {
    const edits = dirtyLines().map((l) => ({ no: l.no, hu: l.hu, say: l.say, speaker: l.speaker }));
    if (!lines.length) { toast("Előbb futtass le egy szinkront (vagy legalább a fordítást).", "err"); return; }
    try {
        const d = await post("/api/regenerate", { edits, options: options() });
        lastStatus = "running";
        toast(`Hang készítése elindult (${d.changed} módosított sor).`, "info");
    } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------- export / import
function openModal(id) { $(id).classList.remove("hidden"); }
function closeModals() { document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden")); }

async function doExport() {
    try {
        const d = await api("/api/export");
        $("export-text").value = d.text;
        $("export-path").textContent = d.path;
        openModal("export-modal");
        loadFiles();
    } catch (e) { toast(e.message, "err"); }
}

async function doImport() {
    const f = $("import-file").files[0];
    try {
        let st;
        if (f) {
            const fd = new FormData();
            fd.append("file", f);
            st = await api("/api/import-file", { method: "POST", body: fd });
        } else {
            st = await post("/api/import", { text: $("import-text").value });
        }
        closeModals();
        $("import-text").value = "";
        $("import-file").value = "";
        $("import-file-name").textContent = "";
        let msg = `Importálva (${st.source}): ${st.changed} sor szövege módosult`;
        if (st.say) msg += `, ${st.say} kiejtés`;
        if (st.missing) msg += `, ${st.missing} sor hiányzott belőle (azok változatlanok)`;
        toast(msg + ". Most: Hang készítése.", "ok");
        await loadLines();
    } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------- fájlok, előnézet
const KIND = {
    video: ["file-video", "bg-brand-500/10 text-brand-500"], audio: ["music", "bg-blue-500/10 text-blue-400"],
    srt: ["subtitles", "bg-purple-500/10 text-purple-400"], txt: ["file-text", "bg-amber-500/10 text-amber-400"],
};

async function loadFiles() {
    let d;
    try { d = await api("/api/files"); } catch { return; }
    const box = $("files");
    if (!d.files.length) {
        box.innerHTML = `<p class="text-sm text-gray-300 py-2">Még nincs elkészült fájl.</p>`;
    } else {
        box.innerHTML = d.files.map((f) => {
            const [ic, cls] = KIND[f.kind] || KIND.txt;
            const playable = f.kind === "video" || f.kind === "audio";
            return `
            <div class="flex items-center justify-between bg-black/30 p-3 rounded-lg border border-dark-border hover:border-brand-500/40 transition-colors gap-3">
                <div class="flex items-center gap-3 min-w-0">
                    <div class="p-2 ${cls} rounded-lg shrink-0"><i data-lucide="${ic}" class="w-4 h-4"></i></div>
                    <div class="min-w-0">
                        <p class="text-xs font-medium text-gray-200 truncate" title="${esc(f.path)}">${esc(f.name)}</p>
                        <p class="text-[10px] text-gray-500 font-mono">${esc(f.label)} · ${fmtSize(f.size)}</p>
                    </div>
                </div>
                <div class="flex gap-1.5 shrink-0">
                    ${playable ? `<button data-preview="${esc(f.path)}" data-kind="${f.kind}" class="px-2.5 py-1.5 bg-dark-hover hover:bg-dark-border text-xs rounded-lg text-gray-300 transition-colors flex items-center gap-1"><i data-lucide="play" class="w-3 h-3"></i> Lejátszás</button>` : ""}
                    <a href="/api/media?download=1&path=${encodeURIComponent(f.path)}" class="px-2.5 py-1.5 ${f.kind === "video" ? "bg-brand-500 hover:bg-brand-600 text-white font-medium" : "bg-dark-hover hover:bg-dark-border text-gray-300"} text-xs rounded-lg transition-colors flex items-center gap-1"><i data-lucide="download" class="w-3 h-3"></i> Letöltés</a>
                </div>
            </div>`;
        }).join("");
        box.querySelectorAll("[data-preview]").forEach((b) => b.addEventListener("click", () => showPreview(b.dataset.preview, b.dataset.kind === "video", Date.now())));
    }
    icons();
    if (d.preview) {
        const key = d.preview.path + "|" + d.preview.mtime;
        if (key !== previewKey) showPreview(d.preview.path, d.preview.video, d.preview.mtime);
    }
}

function showPreview(path, isVideo, stamp) {
    previewKey = path + "|" + stamp;
    const src = `/api/media?path=${encodeURIComponent(path)}&v=${encodeURIComponent(stamp)}`;
    $("preview-empty").classList.add("hidden");
    $("orig-audio").pause();
    $("orig-audio").removeAttribute("src");
    $("video").pause();
    $("audio").pause();
    if (isVideo) {
        $("audio-only").classList.add("hidden");
        $("audio-only").classList.remove("flex");
        $("video").classList.remove("hidden");
        $("video").src = src + "#t=0.5";          // indításkor egy képkocka látszik, ne fekete legyen
    } else {
        $("video").classList.add("hidden");
        $("audio-only").classList.remove("hidden");
        $("audio-only").classList.add("flex");
        $("audio").src = src;
    }
    setPreviewTrack("hu");
    updateBar();
}

function mainMedia() { return $("video").classList.contains("hidden") ? $("audio") : $("video"); }

let userMuted = false;

function applyMute() {
    const m = mainMedia(), o = $("orig-audio");
    if (previewTrack === "orig") { m.muted = true; o.muted = userMuted; }
    else m.muted = userMuted;
    $("pl-mute").innerHTML = `<i data-lucide="${userMuted || m.volume === 0 ? "volume-x" : "volume-2"}" class="w-5 h-5"></i>`;
    icons();
}

function setPreviewTrack(track) {
    previewTrack = track;
    const on = "seg-on px-4 py-1.5 rounded-md transition-all";
    const off = "seg-off px-4 py-1.5 rounded-md transition-all";
    $("track-hu").className = track === "hu" ? on : off;
    $("track-orig").className = track === "orig" ? on : off;
    const m = mainMedia();
    const o = $("orig-audio");
    if (track === "orig") {
        if (!o.getAttribute("src")) o.src = "/api/orig-audio?t=" + Date.now();
        o.currentTime = m.currentTime || 0;
        o.volume = m.volume;
        o.playbackRate = m.playbackRate;
        if (!m.paused) o.play().catch(() => toast("Az eredeti hang még készül, próbáld újra pár másodperc múlva.", "info"));
    } else {
        o.pause();
    }
    applyMute();
}

// ---------------------------------------------------------------- saját vezérlősáv
let seeking = false;
const fmtT = (s) => { s = isFinite(s) ? Math.max(0, s) : 0; return Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0"); };
const hasMedia = () => !!mainMedia().getAttribute("src");

function updateBar() {
    const m = mainMedia(), d = m.duration || 0;
    $("pl-time").textContent = `${fmtT(m.currentTime)} / ${fmtT(d)}`;
    if (!seeking) { $("pl-seek").value = d ? Math.round((m.currentTime / d) * 1000) : 0; fillRange($("pl-seek")); }
}

function setPlayIcon() {
    const playing = !mainMedia().paused;
    $("pl-play").innerHTML = `<i data-lucide="${playing ? "pause" : "play"}" class="w-5 h-5 fill-current"></i>`;
    icons();
}

function togglePlay() {
    if (!hasMedia()) return;
    const m = mainMedia();
    if (m.paused) m.play().catch(() => { }); else m.pause();
}

function bindPlayer() {
    for (const s of [0.75, 1, 1.25, 1.5]) {
        const b = document.createElement("button");
        b.className = "speed w-full text-left text-sm px-2 py-1 rounded-md hover:bg-brand-500/15 " + (s === 1 ? "text-brand-400 font-semibold" : "text-gray-200");
        b.textContent = s.toString().replace(".", ",") + "×";
        b.addEventListener("click", () => {
            mainMedia().playbackRate = s;
            $("orig-audio").playbackRate = s;
            document.querySelectorAll("#pl-menu .speed").forEach((x) => x.className = x.className.replace(/text-brand-400 font-semibold|text-gray-200/, x === b ? "text-brand-400 font-semibold" : "text-gray-200"));
            $("pl-menu").classList.add("hidden");
        });
        $("pl-menu").appendChild(b);
    }
    $("pl-play").addEventListener("click", togglePlay);
    $("video").addEventListener("click", togglePlay);
    $("pl-seek").addEventListener("input", () => {
        seeking = true;
        fillRange($("pl-seek"));
        const m = mainMedia();
        if (m.duration) { m.currentTime = ($("pl-seek").value / 1000) * m.duration; $("pl-time").textContent = `${fmtT(m.currentTime)} / ${fmtT(m.duration)}`; }
    });
    $("pl-seek").addEventListener("change", () => { seeking = false; });
    $("pl-mute").addEventListener("click", () => { userMuted = !userMuted; applyMute(); });
    $("pl-vol").addEventListener("input", () => {
        const v = parseFloat($("pl-vol").value);
        for (const m of [$("video"), $("audio"), $("orig-audio")]) m.volume = v;
        if (v > 0 && userMuted) userMuted = false;
        fillRange($("pl-vol"));
        applyMute();
    });
    fillRange($("pl-vol"));
    fillRange($("pl-seek"));
    $("pl-gear").addEventListener("click", (e) => { e.stopPropagation(); $("pl-menu").classList.toggle("hidden"); });
    $("pl-full").addEventListener("click", () => {
        if (document.fullscreenElement) document.exitFullscreen();
        else $("stage").requestFullscreen().catch(() => { });
    });
    for (const m of [$("video"), $("audio")]) {
        ["timeupdate", "loadedmetadata", "durationchange", "seeked"].forEach((ev) => m.addEventListener(ev, updateBar));
        ["play", "pause", "ended", "emptied"].forEach((ev) => m.addEventListener(ev, setPlayIcon));
    }
}

function syncOrig() {
    if (previewTrack !== "orig") return;
    const m = mainMedia(), o = $("orig-audio");
    if (o.playbackRate !== m.playbackRate) o.playbackRate = m.playbackRate;
    if (Math.abs(o.currentTime - m.currentTime) > 0.25) o.currentTime = m.currentTime;
    if (m.paused && !o.paused) o.pause();
    if (!m.paused && o.paused) o.play().catch(() => { });
}

// ---------------------------------------------------------------- események
function bind() {
    $("toggle-settings").addEventListener("click", toggleSettings);
    bindPlayer();
    $("notes").addEventListener("input", growNotes);
    $("notes-magic").addEventListener("click", (e) => { e.stopPropagation(); $("notes-menu").classList.toggle("hidden"); });
    document.addEventListener("click", (e) => {
        if (!e.target.closest("#notes-menu")) $("notes-menu").classList.add("hidden");
        if (!e.target.closest("#pl-menu")) $("pl-menu").classList.add("hidden");
    });
    const dz = $("drop-zone");
    dz.addEventListener("click", () => $("file-input").click());
    $("file-input").addEventListener("change", (e) => upload(e.target.files[0]));
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("border-brand-500", "bg-brand-500/10"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("border-brand-500", "bg-brand-500/10"); }));
    dz.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));
    $("browse").addEventListener("click", async () => {
        try { const d = await post("/api/browse"); if (d.path) setPath(d.path); } catch (e) { toast(e.message, "err"); }
    });
    $("run").addEventListener("click", run);
    $("stop").addEventListener("click", () => post("/api/stop").then(() => toast("Megszakítás kérve: a folyamatban lévő lépés végén áll meg.", "info")).catch((e) => toast(e.message, "err")));
    $("clear-log").addEventListener("click", () => { $("log").innerHTML = ""; });
    $("listen-hu").addEventListener("click", () => setListen("hu"));
    $("listen-orig").addEventListener("click", () => setListen("orig"));
    $("regen").addEventListener("click", regenerate);
    $("export").addEventListener("click", doExport);
    $("import").addEventListener("click", () => openModal("import-modal"));
    $("import-go").addEventListener("click", doImport);
    $("import-file").addEventListener("change", (e) => { $("import-file-name").textContent = e.target.files[0] ? e.target.files[0].name : ""; });
    $("export-copy").addEventListener("click", async () => {
        try { await navigator.clipboard.writeText($("export-text").value); toast("Kimásolva a vágólapra.", "ok"); }
        catch { $("export-text").select(); document.execCommand("copy"); toast("Kimásolva a vágólapra.", "ok"); }
    });
    document.querySelectorAll(".close-modal").forEach((b) => b.addEventListener("click", closeModals));
    document.querySelectorAll(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) closeModals(); }));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModals(); });
    $("track-hu").addEventListener("click", () => setPreviewTrack("hu"));
    $("track-orig").addEventListener("click", () => setPreviewTrack("orig"));
    for (const m of [$("video"), $("audio")]) {
        ["play", "pause", "seeked", "ratechange"].forEach((ev) => m.addEventListener(ev, syncOrig));
        m.addEventListener("volumechange", () => { $("orig-audio").volume = m.volume; });
    }
    $("video").addEventListener("loadedmetadata", () => {
        const v = $("video");
        if (v.videoWidth) { $("video-badge").textContent = `${v.videoWidth}×${v.videoHeight}`; $("video-badge").classList.remove("hidden"); }
    });
    setInterval(syncOrig, 1000);
    let rt = null;
    window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(growAll, 150); });
    if (document.fonts) document.fonts.ready.then(growAll);
    $("open-out").addEventListener("click", () => post("/api/open-folder", { which: "out" }));
    $("clear-work").addEventListener("click", async () => {
        try {
            const s = await api("/api/work-size");
            if (!s.jobs) { toast("Nincs törölnivaló munkamappa.", "info"); return; }
            if (!confirm(`Törlöd a köztes fájlokat? ${s.jobs} munkamappa, ${fmtSize(s.bytes)}.\nA kész fájlok (kesz mappa) megmaradnak, de a régi munkák szövegét utána nem lehet tovább szerkeszteni.`)) return;
            const d = await post("/api/clear-work");
            toast(`Törölve, ${fmtSize(d.freed)} felszabadult.`, "ok");
            lines = []; renderRows(); loadFiles();
        } catch (e) { toast(e.message, "err"); }
    });
}

bind();
icons();
init().catch((e) => toast("Nem sikerült betölteni a beállításokat: " + e.message, "err"));
pollState();
pollGpu();
