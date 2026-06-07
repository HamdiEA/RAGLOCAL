let currentProject = null;
let currentMode = "qa";
let _settings = {};

const qs = id => document.getElementById(id);
const qsa = sel => Array.from(document.querySelectorAll(sel));

function on(id, evt, fn) {
    const el = qs(id);
    if (el) el.addEventListener(evt, fn);
    else console.warn("Missing element #" + id);
}

function esc(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function fmtSize(b) {
    if (b < 1024) return b + " o";
    if (b < 1048576) return (b / 1024).toFixed(1) + " Ko";
    return (b / 1048576).toFixed(1) + " Mo";
}

function autoResize(id) {
    const el = qs(id);
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
}

let _tt;

function toast(msg) {
    let el = qs("_toast");
    if (!el) {
        el = document.createElement("div");
        el.id = "_toast";
        Object.assign(el.style, {
            position: "fixed",
            bottom: "24px",
            left: "50%",
            transform: "translateX(-50%)",
            background: "#ffffff",
            border: "1px solid #d7dbe7",
            color: "#0f1a2e",
            padding: "10px 20px",
            borderRadius: "6px",
            fontSize: "13px",
            zIndex: "9999",
            transition: "opacity 0.3s",
            pointerEvents: "none"
        });
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = "1";
    clearTimeout(_tt);
    _tt = setTimeout(() => { el.style.opacity = "0"; }, 3500);
}

async function apiFetch(url, opts = {}) {
    const r = await fetch(url, opts);
    if (!r.ok) {
        let msg = "HTTP " + r.status;
        try {
            const j = await r.json();
            if (Array.isArray(j.errors) && j.errors.length) msg = j.errors.join(" | ");
            else msg = j.error || j.message || msg;
        } catch {}
        throw new Error(msg);
    }
    return r.json();
}

function enc(s) { return encodeURIComponent(s); }

async function pingOllama() {
    const dot = qs("ollamaStatus");
    try {
        const r = await fetch("http://127.0.0.1:11434/", { signal: AbortSignal.timeout(2000) });
        if (dot) dot.classList.toggle("online", r.ok);
    } catch { if (dot) dot.classList.remove("online"); }
    setTimeout(pingOllama, 15000);
}

function switchTab(name) {
    if (name === "files" && !currentProject) {
        toast("Sélectionne d'abord un projet.");
        return;
    }
    qsa(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    const idMap = { chat: "tabChat", files: "tabFiles" };
    qsa(".tab-content").forEach(c => {
        const isActive = c.id === idMap[name];
        c.classList.toggle("active", isActive);
        c.style.display = isActive ? "flex" : "none";
    });
    if (name === "files") loadFiles();
}

function openModal() {
    qs("modalOverlay").classList.remove("hidden");
    setTimeout(() => qs("inputProjectName").focus(), 50);
}

function closeModal() {
    qs("modalOverlay").classList.add("hidden");
    qs("inputProjectName").value = "";
    qs("inputProjectDesc").value = "";
}

async function loadProjects() {
    try {
        const data = await apiFetch("/api/projects");
        renderSidebar(data);
        if (!currentProject && data.length) await openProject(data[0].name);
    } catch (e) { console.error(e); }
}

function renderSidebar(projects) {
    const ul = qs("projectList");
    ul.innerHTML = "";
    if (!projects.length) {
        ul.innerHTML = '<li style="padding:10px 12px;color:var(--text3);font-size:12px;">Aucun projet pour le moment</li>';
        return;
    }
    projects.forEach(p => {
        const li = document.createElement("li");
        li.className = "project-item" + (p.name === currentProject ? " active" : "");
        li.dataset.name = p.name;
        li.innerHTML = `
      <span class="project-item-name">${esc(p.name)}</span>
      <span class="project-item-meta">
        <span>${p.file_count} fichier${p.file_count !== 1 ? "s" : ""}</span>
        ${p.indexed ? '<span class="indexed-badge">indexe</span>' : ""}
      </span>`;
        li.addEventListener("click", () => openProject(p.name));
        ul.appendChild(li);
    });
}

async function openProject(name) {
    currentProject = name;
    qsa(".project-item").forEach(li => li.classList.toggle("active", li.dataset.name === name));

    const projects = await apiFetch("/api/projects");
    const meta = projects.find(p => p.name === name) || {};

    qs("currentProjectName").textContent = name;
    qs("currentProjectDesc").textContent = meta.description || "";
    qs("emptyState").classList.add("hidden");
    qs("projectView").classList.remove("hidden");

    qs("chatMessages").innerHTML = '<div class="chat-welcome"><p>Pose une question sur tes documents ou génère un rapport.</p></div>';
    await loadHistory();

    setIndexBanner(!meta.indexed);
    await loadFiles();
    switchTab(meta.indexed ? "chat" : "files");
}

async function loadHistory() {
    if (!currentProject) return;
    try {
        const items = await apiFetch("/api/projects/" + enc(currentProject) + "/history");
        renderHistory(items);
    } catch (e) { console.error(e); }
}

function renderHistory(items) {
    const box = qs("chatMessages");
    box.innerHTML = "";
    if (!items || !items.length) {
        box.innerHTML = '<div class="chat-welcome"><p>Pose une question sur tes documents ou génère un rapport.</p></div>';
        return;
    }
    items.forEach(m => addMsg(m.role, m.text, m.sources || []));
}

function setIndexBanner(show) {
    qs("indexBanner").classList.toggle("hidden", !show);
}

async function createProject() {
    const name = qs("inputProjectName").value.trim();
    const desc = qs("inputProjectDesc").value.trim();
    if (!name) { toast("Saisis un nom."); return; }
    try {
        await apiFetch("/api/projects", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, description: desc }),
        });
        closeModal();
        await loadProjects();
        await openProject(name);
    } catch (e) { toast("Erreur : " + e.message); }
}

async function deleteProject() {
    if (!currentProject || !confirm(`Supprimer "${currentProject}" ?`)) return;
    await apiFetch("/api/projects/" + enc(currentProject), { method: "DELETE" });
    currentProject = null;
    qs("projectView").classList.add("hidden");
    qs("emptyState").classList.remove("hidden");
    await loadProjects();
}

function openFilePickerForProject() {
    if (!currentProject) {
        toast("Sélectionne d'abord un projet.");
        return;
    }
    switchTab("files");
    const fileInput = qs("fileInput");
    if (fileInput) fileInput.click();
}

async function loadFiles() {
    if (!currentProject) {
        renderFiles([], "Aucun projet sélectionné.");
        return;
    }
    try {
        const files = await apiFetch("/api/projects/" + enc(currentProject) + "/files");
        renderFiles(files);
    } catch (e) {
        console.error(e);
        toast("Erreur: " + e.message);
        renderFiles([], "Erreur de chargement des fichiers.");
    }
}

function renderFiles(files, emptyMessage) {
    const box = qs("fileList");
    if (!box) return;
    box.innerHTML = "";
    const list = Array.isArray(files) ? files : (files && files.files ? files.files : []);
    const header = document.createElement("div");
    header.style.color = "var(--text3)";
    header.style.fontSize = "12px";
    header.style.marginBottom = "6px";
    header.textContent = "Projet: " + (currentProject || "(aucun)") + " · " + (list.length || 0) + " fichier(s)";
    box.appendChild(header);
    if (!list.length) {
        const msg = emptyMessage || "Aucun fichier - dépose des fichiers ci-dessus.";
        const p = document.createElement("p");
        p.style.color = "var(--text3)";
        p.style.fontSize = "13px";
        p.textContent = msg;
        box.appendChild(p);
        return;
    }
    list.forEach(f => {
        const extKey = f.ext.replace(".", "");
        const row = document.createElement("div");
        row.className = "file-item";
        row.innerHTML = `
      <span class="file-ext ${extKey}">${extKey}</span>
      <span class="file-name">${esc(f.name)}</span>
      <span class="file-size">${fmtSize(f.size)}</span>
      <button class="btn-del-file" title="Supprimer">X</button>`;
        row.querySelector(".btn-del-file").addEventListener("click", () => deleteFile(f.name));
        box.appendChild(row);
    });
}

async function uploadFiles(fileList) {
    if (!currentProject) { toast("Sélectionne d'abord un projet."); return; }
    const valid = Array.from(fileList).filter(f => /\.(pdf|txt|md)$/i.test(f.name));
    if (!valid.length) { toast("Formats acceptés : PDF, TXT, MD"); return; }

    toast("Téléversement de " + valid.length + " fichier(s)...");
    const fd = new FormData();
    valid.forEach(f => fd.append("files", f));

    try {
        const data = await apiFetch("/api/projects/" + enc(currentProject) + "/files", { method: "POST", body: fd });
        await loadFiles();
        await loadProjects();
        setIndexBanner(true);
        const savedCount = data.saved && data.saved.length != null ? data.saved.length : 0;
        toast(savedCount + " fichier(s) ajouté(s)");
    } catch (e) { toast("Erreur : " + e.message); }
}

async function deleteFile(name) {
    if (!confirm('Supprimer "' + name + '" ?')) return;
    await apiFetch("/api/projects/" + enc(currentProject) + "/files/" + enc(name), { method: "DELETE" });
    await loadFiles();
    await loadProjects();
    setIndexBanner(true);
}

function indexProject() {
    if (!currentProject) return;
    const consoleEl = qs("indexConsole");
    const logs = qs("consoleLogs");
    consoleEl.classList.remove("hidden");
    logs.innerHTML = "";
    setIndexBanner(false);

    const es = new EventSource("/api/projects/" + enc(currentProject) + "/index");
    es.onmessage = e => {
        let p;
        try { p = JSON.parse(e.data); } catch { return; }
        const line = document.createElement("div");
        line.className = "console-log-line " + p.type;
        line.textContent = p.msg;
        logs.appendChild(line);
        logs.scrollTop = logs.scrollHeight;
        if (p.type === "done") {
            es.close();
            loadProjects();
            setTimeout(() => consoleEl.classList.add("hidden"), 5000);
        }
        if (p.type === "error") {
            es.close();
            setIndexBanner(true);
        }
    };
    es.onerror = () => {
        es.close();
        const line = document.createElement("div");
        line.className = "console-log-line error";
        line.textContent = "Connexion perdue.";
        logs.appendChild(line);
    };
}

async function loadSettings() {
    try {
        _settings = await apiFetch("/api/settings");
        const el = qs("footerModelName");
        if (el) el.textContent = _settings.llm_model || "";
    } catch (e) {
        console.error("Failed to load settings:", e);
    }
}

async function fetchAndPopulateModels() {
    const statusEl = qs("modelsStatus");
    if (statusEl) {
        statusEl.textContent = "Chargement…";
        statusEl.className = "models-status";
    }
    try {
        const data = await apiFetch("/api/models");
        const models = data.models || [];
        ["llmModelsList", "embedModelsList"].forEach(id => {
            const dl = qs(id);
            if (!dl) return;
            dl.innerHTML = "";
            models.forEach(m => {
                const o = document.createElement("option");
                o.value = m;
                dl.appendChild(o);
            });
        });
        if (statusEl) {
            statusEl.textContent = models.length ?
                models.length + " modèle(s) disponible(s) — saisissez pour filtrer ou entrer un nom libre" :
                "Ollama injoignable — saisissez le nom du modèle manuellement";
            statusEl.className = "models-status " + (models.length ? "ok" : "warn");
        }
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = "Impossible de joindre Ollama";
            statusEl.className = "models-status warn";
        }
    }
}

async function openSettingsModal() {
    await loadSettings();
    if (qs("settingLlmModel")) qs("settingLlmModel").value = _settings.llm_model || "";
    if (qs("settingEmbedModel")) qs("settingEmbedModel").value = _settings.embedding_model || "";
    if (qs("settingChunkSize")) qs("settingChunkSize").value = _settings.chunk_size || 1200;
    if (qs("settingChunkOverlap")) qs("settingChunkOverlap").value = _settings.chunk_overlap || 250;
    if (qs("settingRetrievalK")) qs("settingRetrievalK").value = _settings.retrieval_k || 8;
    qs("settingsOverlay").classList.remove("hidden");
    fetchAndPopulateModels();
}

function closeSettingsModal() {
    qs("settingsOverlay").classList.add("hidden");
}

async function saveSettings() {
    const llm = (qs("settingLlmModel").value || "").trim();
    const embed = (qs("settingEmbedModel").value || "").trim();
    if (!llm) { toast("Le nom du modèle LLM est requis."); return; }
    if (!embed) { toast("Le nom du modèle d'embedding est requis."); return; }

    const payload = {
        llm_model: llm,
        embedding_model: embed,
        chunk_size: parseInt(qs("settingChunkSize").value) || 1200,
        chunk_overlap: parseInt(qs("settingChunkOverlap").value) || 250,
        retrieval_k: parseInt(qs("settingRetrievalK").value) || 8,
    };
    try {
        const result = await apiFetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        _settings = result.settings || {..._settings, ...payload };
        const el = qs("footerModelName");
        if (el) el.textContent = _settings.llm_model || "";
        closeSettingsModal();
        toast("Paramètres enregistrés.");
    } catch (e) {
        toast("Erreur lors de l'enregistrement des paramètres : " + e.message);
    }
}

const PDF_KEYWORDS = /\bpdf\b|rapport\s*pdf|exporter?\s*pdf|genere?\s*(un\s*)?pdf|télécharger?\s*(en\s*)?pdf/i;

async function downloadPDF(content, projectName) {
    const btn = document.activeElement;
    const origText = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
        btn.textContent = "Génération…";
    }

    try {
        const currentProjectEl = document.getElementById("currentProjectName");
        const title = ((currentProjectEl && currentProjectEl.textContent) || projectName) + " — Rapport";
        const resp = await fetch("/api/projects/" + enc(projectName) + "/export-pdf", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content, title }),
        });
        if (!resp.ok) {
            const j = await resp.json().catch(() => ({}));
            throw new Error(j.error || "HTTP " + resp.status);
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        const cd = resp.headers.get("content-disposition") || "";
        const m = cd.match(/filename="?([^"]+)"?/);
        a.download = m ? m[1] : projectName + "_rapport.pdf";
        a.click();
        URL.revokeObjectURL(url);
        toast("PDF téléchargé ✓");
    } catch (e) {
        toast("Erreur PDF : " + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = origText;
        }
    }
}

function makePdfButton(answerText) {
    const wrap = document.createElement("div");
    wrap.className = "pdf-btn-wrap";
    const btn = document.createElement("button");
    btn.className = "btn-pdf";
    btn.innerHTML = "&#x1F4C4; Télécharger en PDF";
    btn.addEventListener("click", () => downloadPDF(answerText, currentProject));
    wrap.appendChild(btn);
    return wrap;
}

async function sendMessage() {
    const input = qs("chatInput");
    const q = input.value.trim();
    if (!q || !currentProject) return;
    input.value = "";
    autoResize("chatInput");
    addMsg("user", q);
    const tid = addTyping();
    qs("btnSend").disabled = true;

    const wantsPdf = PDF_KEYWORDS.test(q);
    const effectiveMode = wantsPdf ? "report" : currentMode;
    if (wantsPdf && currentMode !== "report") {
        qsa(".mode-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === "report"));
        currentMode = "report";
    }

    const llmQuestion = wantsPdf ?
        q.replace(/\bpdf\b/gi, 'rapport')
        .replace(/\s{2,}/g, ' ').trim() :
        q;

    try {
        const data = await apiFetch("/api/projects/" + enc(currentProject) + "/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: llmQuestion, mode: effectiveMode }),
        });
        removeTyping(tid);
        addMsg("assistant", data.error ? data.error : data.answer, data.sources || [], wantsPdf || effectiveMode === "report");
    } catch (e) {
        removeTyping(tid);
        addMsg("assistant", e.message, [], false);
    }
    qs("btnSend").disabled = false;
    input.focus();
}

function addMsg(role, text, sources = [], showPdfBtn = false) {
    const box = qs("chatMessages");
    const welcome = box.querySelector(".chat-welcome");
    if (welcome) welcome.remove();
    const div = document.createElement("div");
    div.className = "msg " + role;
    const srcs = sources.length ?
        '<div class="msg-sources">' + sources.map(s => '<span class="source-tag">' + esc(s) + '</span>').join("") + "</div>" :
        "";
    div.innerHTML = `
    <div class="msg-avatar">${role === "user" ? "U" : "AI"}</div>
    <div class="msg-body">
      <div class="msg-bubble">${role === "assistant" ? md2html(text) : "<p>" + esc(text) + "</p>"}</div>
      ${srcs}
    </div>`;
    box.appendChild(div);
    if (role === "assistant" && showPdfBtn && text && text.length > 40) {
        const msgBody = div.querySelector(".msg-body");
        if (msgBody) msgBody.appendChild(makePdfButton(text));
    }
    box.scrollTop = box.scrollHeight;
}

function addTyping() {
    const id = "t" + Date.now();
    const box = qs("chatMessages");
    const div = document.createElement("div");
    div.id = id;
    div.className = "msg assistant";
    div.innerHTML = `<div class="msg-avatar">AI</div><div class="msg-body"><div class="msg-bubble">
    <div class="typing-indicator"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div>
  </div></div>`;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return id;
}

function removeTyping(id) {
    const el = qs(id);
    if (el) el.remove();
}

function md2html(md) {
    let s = esc(md);
    s = s.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, c) => "<pre><code>" + c.trim() + "</code></pre>");
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/^### (.+)$/gm, "<h3>$1</h3>");
    s = s.replace(/^## (.+)$/gm, "<h2>$1</h2>");
    s = s.replace(/^# (.+)$/gm, "<h1>$1</h1>");
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*(.+?)\*/g, "<em>$1</em>");
    s = s.replace(/^---+$/gm, "<hr>");
    s = s.replace(/^[-*] (.+)$/gm, "<li>$1</li>");
    s = s.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
    s = s.replace(/(<li>[\s\S]*?<\/li>)/g, m => "<ul>" + m + "</ul>");
    s = s.split(/\n{2,}/).map(b => {
        b = b.trim();
        if (!b || /^<(h[1-3]|ul|ol|pre|hr|li)/.test(b)) return b;
        return "<p>" + b.replace(/\n/g, "<br>") + "</p>";
    }).join("\n");
    return s;
}

document.addEventListener("DOMContentLoaded", () => {
    loadProjects();
    loadSettings();
    pingOllama();

    on("btnNewProject", "click", openModal);
    on("btnNewProjectEmpty", "click", openModal);
    on("btnCancelProject", "click", closeModal);
    on("btnConfirmProject", "click", createProject);
    on("inputProjectName", "keydown", e => { if (e.key === "Enter") createProject(); });
    qs("modalOverlay").addEventListener("click", e => {
        if (e.target.id === "modalOverlay") closeModal();
    });

    on("btnSettings", "click", openSettingsModal);
    on("btnCancelSettings", "click", closeSettingsModal);
    on("btnSaveSettings", "click", saveSettings);
    on("btnRefreshModels", "click", fetchAndPopulateModels);
    qs("settingsOverlay").addEventListener("click", e => {
        if (e.target.id === "settingsOverlay") closeSettingsModal();
    });

    on("btnDeleteProject", "click", deleteProject);
    on("btnAddDocuments", "click", openFilePickerForProject);

    qsa(".tab").forEach(btn => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

    qsa(".mode-btn").forEach(btn => btn.addEventListener("click", () => {
        qsa(".mode-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        currentMode = btn.dataset.mode;
    }));

    on("btnSend", "click", sendMessage);
    on("chatInput", "keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    on("chatInput", "input", () => autoResize("chatInput"));

    on("btnIndex", "click", () => indexProject());
    on("btnIndexFromFiles", "click", () => {
        switchTab("chat");
        indexProject();
    });
    on("btnCloseConsole", "click", () => qs("indexConsole").classList.add("hidden"));

    const fileInput = qs("fileInput");
    const dropZone = qs("dropZone");

    on("btnBrowse", "click", e => {
        e.stopPropagation();
        fileInput.click();
    });

    dropZone.addEventListener("click", e => {
        if (e.target.id !== "btnBrowse") fileInput.click();
    });
    fileInput.addEventListener("change", () => {
        if (fileInput.files.length) uploadFiles(fileInput.files);
        fileInput.value = "";
    });
    dropZone.addEventListener("dragover", e => {
        e.preventDefault();
        dropZone.classList.add("drag-over");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
    dropZone.addEventListener("drop", e => {
        e.preventDefault();
        dropZone.classList.remove("drag-over");
        if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
    });
});