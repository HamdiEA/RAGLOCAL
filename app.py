"""Flask backend pour l'application RAG locale."""

import io
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

ALLOWED_EXTS = {".pdf", ".txt", ".md"}

APP_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "RAGLocal"
SETTINGS_FILE = APP_DATA_ROOT / "rag_local_settings.json"
LEGACY_SETTINGS_FILE = Path(__file__).parent / "rag_local_settings.json"

_DEFAULTS = {
    "base_dir": "",
    "llm_model": "gemma4:e4b",
    "embedding_model": "nomic-embed-text",
    "chunk_size": 1200,
    "chunk_overlap": 250,
    "retrieval_k": 8,
    "max_history": 200,
}

PROJECT_NAME_RE = re.compile(r"^[\w .-]{1,80}$", re.UNICODE)
FORBIDDEN_PATH_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

def _default_base_dir():
    """Retourne le dossier de stockage local par defaut de l'application."""
    return APP_DATA_ROOT / "projects"

def _to_abs_path(raw_path):
    """Normalise un chemin en chemin absolu."""
    candidate = Path(str(raw_path)).expanduser() if raw_path else _default_base_dir()
    if not candidate.is_absolute():
        candidate = (Path(__file__).parent / candidate).resolve()
    return candidate

def _is_writable_dir(folder):
    """Verifie qu'un dossier est accessible en ecriture."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".rag_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False

def _is_within(child, parent):
    """Verifie qu'un chemin enfant reste dans un dossier parent."""
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False

def _validated_project_name(name):
    """Valide un nom de projet pour eviter les noms interdits/sensibles."""
    cleaned = str(name or "").strip()
    if not cleaned:
        return False, "Nom requis."
    if cleaned.endswith((" ", ".")):
        return False, "Le nom ne doit pas finir par un espace ou un point."
    if any(ch in FORBIDDEN_PATH_CHARS for ch in cleaned):
        return False, "Le nom contient des caracteres interdits."
    if not PROJECT_NAME_RE.fullmatch(cleaned):
        return False, "Le nom contient des caracteres non autorises."
    stem = Path(cleaned).stem.upper()
    if stem in WINDOWS_RESERVED_NAMES:
        return False, "Nom reserve par Windows."
    return True, cleaned

def _safe_project_root(name):
    """Construit le chemin racine d'un projet valide."""
    ok, value = _validated_project_name(name)
    if not ok:
        return None, value
    base = _base().resolve(strict=False)
    root = (base / value).resolve(strict=False)
    if not _is_within(root, base):
        return None, "Nom de projet invalide."
    return root, value

def _safe_doc_path(project_name, filename):
    """Construit un chemin de fichier document securise dans un projet."""
    root, err_or_name = _safe_project_root(project_name)
    if root is None:
        return None, err_or_name
    clean_name = Path(str(filename or "")).name.strip()
    if not clean_name:
        return None, "Nom de fichier invalide."
    if clean_name in {".", ".."}:
        return None, "Nom de fichier invalide."
    full_path = (root / "documents" / clean_name).resolve(strict=False)
    if not _is_within(full_path, root / "documents"):
        return None, "Nom de fichier invalide."
    return full_path, err_or_name

def _load_settings():
    """Charge les parametres utilisateurs et applique des valeurs sures."""
    saved = {}
    for settings_file in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        if not settings_file.exists():
            continue
        try:
            loaded = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                saved = loaded
                break
        except Exception:
            continue
    merged = {**_DEFAULTS, **saved}
    base_candidate = _to_abs_path(merged.get("base_dir"))
    if not _is_writable_dir(base_candidate):
        base_candidate = _default_base_dir()
        _is_writable_dir(base_candidate)
    merged["base_dir"] = str(base_candidate)
    return merged

_settings = _load_settings()

def _save_settings():
    """Enregistre les parametres utilisateurs sur disque."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(_settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )

_save_settings()

def cfg(key):
    """Lit une valeur de configuration avec fallback sur les valeurs par defaut."""
    return _settings.get(key, _DEFAULTS.get(key))

def _coerce_int(value, default, min_value, max_value):
    """Convertit une valeur en entier borne."""
    try:
        casted = int(value)
    except Exception:
        casted = default
    return max(min_value, min(max_value, casted))

def _base():
    """Retourne le dossier racine des projets."""
    return Path(cfg("base_dir"))

def project_dir(name):
    """Retourne le dossier d'un projet en validant le nom."""
    root, err = _safe_project_root(name)
    if root is None:
        raise ValueError(err)
    return root

def docs_dir(name):
    """Retourne le dossier des documents d'un projet."""
    return project_dir(name) / "documents"

def db_dir(name):
    """Retourne le dossier de base vectorielle d'un projet."""
    return project_dir(name) / "chroma_db"

def meta_path(name):
    """Retourne le chemin du fichier de metadonnees d'un projet."""
    return project_dir(name) / "meta.json"

def history_path(name):
    """Retourne le chemin de l'historique de chat d'un projet."""
    return project_dir(name) / "chat_history.json"

def sse(event, message):
    """Formate un message Server-Sent Events (SSE) avec un type et un contenu."""
    return f"data: {json.dumps({'type': event, 'msg': message})}\n\n"

def read_json(path, default):
    """Lit un fichier JSON et retourne son contenu, ou une valeur par dÃ©faut si absent/invalide."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def write_json(path, data):
    """Ã‰crit un dictionnaire dans un fichier JSON avec indentation et encodage UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _get_available_models():
    """Retourne la liste des modÃ¨les disponibles localement dans Ollama."""
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        return {m["name"] for m in data.get("models", [])}
    except Exception:
        return set()

def _model_exists(model_name):
    """VÃ©rifie si un modÃ¨le existe localement."""
    available = _get_available_models()
    model_name = (model_name or "").strip()
    if not model_name:
        return False
    if model_name in available:
        return True
    return any(m.split(":", 1)[0] == model_name for m in available)

def _pull_model(model_name):
    """TÃ©lÃ©charge et installe un modÃ¨le via Ollama pull."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/pull",
            data=json.dumps({"name": model_name}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3600) as resp:
            # Consommer la rÃ©ponse streaming
            while True:
                chunk = resp.read(1024)
                if not chunk:
                    break
        return True, "Modèle téléchargé avec succès"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, f"Le modèle '{model_name}' est introuvable dans le registre Ollama"
        return False, f"Erreur HTTP {e.code} : {e.reason}"
    except Exception as e:
        return False, f"Erreur lors du téléchargement du modèle : {str(e)}"

def read_meta(name):
    """Lit et retourne les mÃ©tadonnÃ©es d'un projet (nom, description, date de crÃ©ation)."""
    return read_json(meta_path(name), {"name": name})

def read_history(name):
    """Lit et retourne l'historique complet des messages d'un projet."""
    return read_json(history_path(name), [])

def append_history(name, role, text, sources=None):
    """Ajoute un message Ã  l'historique du projet, en limitant Ã  MAX_HISTORY entrÃ©es."""
    items = read_history(name)
    items.append({
        "role": role,
        "text": text,
        "sources": sources or [],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    limit = cfg("max_history")
    if len(items) > limit:
        items = items[-limit:]
    write_json(history_path(name), items)

def list_doc_files(name):
    """Liste tous les fichiers documents (PDF, TXT, MD) d'un projet avec leur nom, extension et taille."""
    folder = docs_dir(name)
    if not folder.exists():
        return []
    files = []
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTS:
            files.append({
                "name": f.name,
                "ext": f.suffix.lower(),
                "size": f.stat().st_size,
            })
    return files

def invalidate_index(name):
    """Supprime le dossier de la base vectorielle pour forcer une rÃ©indexation."""
    db = db_dir(name)
    if db.exists():
        shutil.rmtree(db)

def run_llm(template, temperature, **kwargs):
    """Construit un prompt Ã  partir d'un template et invoque le modÃ¨le LLM local via Ollama."""
    from langchain_core.prompts import PromptTemplate
    from langchain_ollama import ChatOllama

    llm_model = cfg("llm_model")
    
    # Téléchargement automatique du modèle si nécessaire
    if not _model_exists(llm_model):
        print(f"Modèle {llm_model} introuvable. Téléchargement en cours...")
        ok, msg = _pull_model(llm_model)
        if not ok:
            raise Exception(f"Échec de l'installation du modèle '{llm_model}' : {msg}")

    prompt = PromptTemplate.from_template(template)
    llm = ChatOllama(model=llm_model, temperature=temperature)
    chain = prompt | llm
    return chain.invoke(kwargs).content

def general_template():
    """Retourne le template de prompt pour les questions gÃ©nÃ©rales (hors documents)."""
    return """Tu es un assistant IA local. Sois clair, utile et precis.
Tu peux donner des astuces, conseils et estimations basees sur tes connaissances generales.
Si la question demande des elements factuels non confirmables ici, indique-le.

QUESTION : {question}

REPONSE :"""

def report_template():
    """Retourne le template de prompt pour la gÃ©nÃ©ration de rapports structurÃ©s Ã  partir des documents."""
    return """Tu es un assistant expert en analyse documentaire.
Produis un rapport structure, clair et utile a partir du contexte fourni.
Si l'utilisateur demande du LaTeX, rends le rapport en LaTeX.
Tu peux ajouter des astuces, recommandations ou estimations si elles aident la decision,
mais indique quand cela depasse les documents.

FORMAT (Markdown par defaut) :
# Titre du rapport
## Resume executif
## Sections thematiques
## Points cles
## Recommandations et estimations (si pertinent)
## Conclusion

REGLES :
- Analyse en profondeur le contexte.
- Cite les documents quand c'est possible.
- Si tu utilises des connaissances generales, marque-le explicitement.
- Ne pas inventer de faits attribues aux documents.

CONTEXTE :
{context}

DEMANDE : {question}

RAPPORT :"""

def qa_template():
    """Retourne le template de prompt pour les questions-rÃ©ponses prÃ©cises sur les documents."""
    return """Tu es un assistant IA expert en analyse documentaire.
Analyse en profondeur les documents et reponds avec precision et clarte.
Tu peux donner des astuces, conseils ou estimations utiles si c'est pertinent,
mais indique clairement quand cela ne vient pas des documents.

REGLES :
- Priorite aux informations du contexte.
- Si une info vient des documents, appuie-toi sur le contexte.
- Si tu ajoutes une estimation ou une astuce generale, precise-le.
- Si l'information manque, dis-le sans phrase standard imposee.

CONTEXTE :
{context}

QUESTION : {question}

REPONSE :"""

@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Retourne les parametres actuels de l'application."""
    return jsonify({k: cfg(k) for k in _DEFAULTS})

@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Met a jour les parametres et installe les modeles manquants si besoin."""
    global _settings
    data = request.get_json(silent=True) or {}
    editable = {"llm_model", "embedding_model", "chunk_size", "chunk_overlap", "retrieval_k", "max_history"}
    llm_value = str(data.get("llm_model", cfg("llm_model"))).strip()
    embed_value = str(data.get("embedding_model", cfg("embedding_model"))).strip()
    if not llm_value:
        return jsonify({"ok": False, "error": "Le nom du modèle LLM est requis."}), 400
    if not embed_value:
        return jsonify({"ok": False, "error": "Le nom du modèle d'embedding est requis."}), 400
    data["llm_model"] = llm_value
    data["embedding_model"] = embed_value
    
    # Vérification et téléchargement automatique des modèles si nécessaire
    pull_errors = []
    if "llm_model" in data and data["llm_model"] != cfg("llm_model"):
        if not _model_exists(data["llm_model"]):
            ok, msg = _pull_model(data["llm_model"])
            if not ok:
                pull_errors.append(f"Modèle LLM : {msg}")
    
    if "embedding_model" in data and data["embedding_model"] != cfg("embedding_model"):
        if not _model_exists(data["embedding_model"]):
            ok, msg = _pull_model(data["embedding_model"])
            if not ok:
                pull_errors.append(f"Modèle d'embedding : {msg}")
    
    # If there are pull errors, return them
    if pull_errors:
        return jsonify({
            "ok": False,
            "error": " ; ".join(pull_errors),
            "errors": pull_errors,
            "settings": {k: cfg(k) for k in _DEFAULTS}
        }), 400
    
    for k, v in data.items():
        if k in editable:
            _settings[k] = v
    _settings["chunk_size"] = _coerce_int(_settings.get("chunk_size"), 1200, 100, 10000)
    _settings["chunk_overlap"] = _coerce_int(_settings.get("chunk_overlap"), 250, 0, 5000)
    _settings["retrieval_k"] = _coerce_int(_settings.get("retrieval_k"), 8, 1, 30)
    _settings["max_history"] = _coerce_int(_settings.get("max_history"), 200, 10, 1000)
    _save_settings()
    return jsonify({"ok": True, "settings": {k: cfg(k) for k in _DEFAULTS}})

@app.route("/api/models", methods=["GET"])
def get_models():
    """Liste les modeles disponibles detectes dans Ollama."""
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        models = sorted(m["name"] for m in data.get("models", []))
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"models": [], "error": str(e)})

@app.route("/")
def index():
    """Sert la page HTML principale de l'application."""
    return render_template("index.html")

@app.route("/api/projects", methods=["GET"])
def list_projects():
    """Retourne la liste des projets locaux et leur etat d'indexation."""
    bd = _base().resolve(strict=False)
    bd.mkdir(parents=True, exist_ok=True)
    projects = []
    for d in sorted(bd.iterdir()):
        if not d.is_dir():
            continue
        if not _validated_project_name(d.name)[0]:
            continue
        try:
            meta = read_meta(d.name)
            files = list_doc_files(d.name)
            indexed = (db_dir(d.name) / "chroma.sqlite3").exists()
        except Exception:
            continue
        projects.append({
            "name": d.name,
            "description": meta.get("description", ""),
            "created_at": meta.get("created_at", ""),
            "file_count": len(files),
            "indexed": indexed,
        })
    return jsonify(projects)

@app.route("/api/projects", methods=["POST"])
def create_project():
    """CrÃ©e un nouveau projet avec son dossier, sous-dossiers et fichiers de mÃ©tadonnÃ©es/historique vides."""
    data = request.get_json(silent=True) or {}
    ok, name_or_err = _validated_project_name(data.get("name", ""))
    if not ok:
        return jsonify({"error": name_or_err}), 400
    name = name_or_err
    desc = data.get("description", "").strip()

    if project_dir(name).exists():
        return jsonify({"error": "Projet deja existant."}), 409

    try:
        docs_dir(name).mkdir(parents=True, exist_ok=True)
        write_json(meta_path(name), {
            "name": name,
            "description": desc[:300],
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        write_json(history_path(name), [])
    except Exception as e:
        return jsonify({"error": f"Erreur creation projet: {e}"}), 500

    return jsonify({"ok": True, "name": name}), 201

@app.route("/api/projects/<name>", methods=["DELETE"])
def delete_project(name):
    """Supprime un projet complet."""
    d, err = _safe_project_root(name)
    if d is None:
        return jsonify({"error": err}), 400
    if not d.exists():
        return jsonify({"error": "Introuvable."}), 404
    shutil.rmtree(d)
    return jsonify({"ok": True})

@app.route("/api/projects/<name>/history", methods=["GET"])
def get_history(name):
    """Retourne l'historique complet des messages d'un projet au format JSON."""
    d, err = _safe_project_root(name)
    if d is None:
        return jsonify({"error": err}), 400
    if not d.exists():
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify(read_history(name))

@app.route("/api/projects/<name>/files", methods=["GET"])
def list_files(name):
    """Retourne la liste des fichiers documents d'un projet avec leurs dÃ©tails."""
    d, err = _safe_project_root(name)
    if d is None:
        return jsonify({"error": err}), 400
    if not d.exists():
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify(list_doc_files(name))

@app.route("/api/projects/<name>/files", methods=["POST"])
def upload_files(name):
    """RÃ©ceptionne des fichiers uploadÃ©s, les sauvegarde dans le dossier documents, et invalide l'index si des fichiers ont Ã©tÃ© ajoutÃ©s."""
    root, err = _safe_project_root(name)
    if root is None:
        return jsonify({"error": err}), 400
    folder = root / "documents"
    if not folder.exists():
        if root.exists():
            folder.mkdir(parents=True, exist_ok=True)
        else:
            return jsonify({"error": f"Projet '{name}' introuvable."}), 404

    if "files" not in request.files:
        return jsonify({"error": "Aucun fichier recu."}), 400

    saved = []
    skipped = []
    for file in request.files.getlist("files"):
        if not file.filename:
            continue
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTS:
            skipped.append(file.filename)
            continue
        safe_name = Path(file.filename).name
        if not safe_name:
            skipped.append(file.filename)
            continue
        dest = (folder / safe_name).resolve(strict=False)
        if not _is_within(dest, folder):
            skipped.append(file.filename)
            continue
        file.save(dest)
        saved.append(safe_name)

    if saved:
        invalidate_index(name)

    return jsonify({"saved": saved, "skipped": skipped})

@app.route("/api/projects/<name>/files/<filename>", methods=["DELETE"])
def delete_file(name, filename):
    """Supprime un fichier document spÃ©cifique et invalide l'index vectoriel du projet."""
    f, err = _safe_doc_path(name, filename)
    if f is None:
        return jsonify({"error": err}), 400
    if not f.exists():
        return jsonify({"error": "Introuvable."}), 404
    f.unlink()
    invalidate_index(name)
    return jsonify({"ok": True})

@app.route("/api/projects/<name>/index", methods=["GET", "POST"])
def index_project(name):
    """Lance l'indexation complÃ¨te des documents d'un projet en streaming SSE (chargement, dÃ©coupage, vectorisation)."""
    root, err = _safe_project_root(name)
    if root is None:
        return Response(sse("error", err), mimetype="text/event-stream")
    if not root.exists():
        return Response(sse("error", "Projet introuvable."), mimetype="text/event-stream")

    def generate():
        """Genere les evenements SSE de progression d'indexation."""
        try:
            from langchain_community.document_loaders import PyPDFLoader, TextLoader
            from langchain_text_splitters import RecursiveCharacterTextSplitter
            from langchain_chroma import Chroma
            from langchain_ollama import OllamaEmbeddings

            folder = root / "documents"
            db = root / "chroma_db"
            folder.mkdir(parents=True, exist_ok=True)

            files = [f for f in folder.iterdir()
                     if f.is_file() and f.suffix.lower() in ALLOWED_EXTS]
            if not files:
                yield sse("error", "Aucun fichier dans ce projet.")
                return

            yield sse("log", f"{len(files)} fichier(s) detecte(s).")

            if db.exists():
                shutil.rmtree(db)
                yield sse("log", "Ancienne base supprimee.")

            all_docs = []
            for f in files:
                yield sse("log", f"Lecture: {f.name}")
                try:
                    loader = PyPDFLoader(str(f)) if f.suffix.lower() == ".pdf" else TextLoader(str(f), encoding="utf-8")
                    docs = loader.load()
                    for doc in docs:
                        doc.metadata["source"] = f.name
                    all_docs.extend(docs)
                except Exception as e:
                    yield sse("log", f"Ignore: {f.name} ({e})")

            yield sse("log", f"{len(all_docs)} page(s) extraites.")
            if not all_docs:
                yield sse("error", "Aucune page lisible trouvee dans les documents.")
                return

            chunk_size = cfg("chunk_size")
            chunk_overlap = cfg("chunk_overlap")
            embed_model = cfg("embedding_model")

            splitter = RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                separators=["\n\n", "\n", ".", "?", "!", " ", ""],
            )
            chunks = splitter.split_documents(all_docs)
            for i, c in enumerate(chunks):
                c.metadata["chunk_id"] = i
            if not chunks:
                yield sse("error", "Aucun morceau généré. Vérifie les documents ou les paramètres.")
                return

            yield sse("log", f"{len(chunks)} morceau(x) créés (taille={chunk_size}, recouvrement={chunk_overlap}).")
            yield sse("log", f"Vectorisation avec {embed_model}...")

            # Téléchargement automatique du modèle d'embedding si nécessaire
            if not _model_exists(embed_model):
                yield sse("log", f"Modèle d'embedding {embed_model} introuvable. Téléchargement en cours...")
                ok, msg = _pull_model(embed_model)
                if not ok:
                    yield sse("error", f"Le modèle '{embed_model}' est introuvable dans le registre Ollama")
                    return

            embeddings = OllamaEmbeddings(model=embed_model)
            Chroma.from_documents(
                documents=chunks,
                embedding=embeddings,
                persist_directory=str(db),
            )

            yield sse("done", f"Indexation terminée. {len(chunks)} morceau(x) stocké(s).")
        except Exception as e:
            yield sse("error", f"Erreur critique: {e}")

    resp = Response(stream_with_context(generate()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp

@app.route("/api/projects/<name>/chat", methods=["POST"])
def chat(name):
    """Traite une question utilisateur : recherche dans la base vectorielle si disponible, sinon passe en mode gÃ©nÃ©ral, et retourne la rÃ©ponse."""
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    mode = data.get("mode", "qa")
    mode = "report" if mode == "report" else "qa"
    root, err = _safe_project_root(name)
    if root is None:
        return jsonify({"error": err}), 400
    if not root.exists():
        return jsonify({"error": "Projet introuvable."}), 404

    if not question:
        return jsonify({"error": "Question vide."}), 400

    db = root / "chroma_db"

    try:
        from langchain_chroma import Chroma
        from langchain_ollama import OllamaEmbeddings

        if not db.exists():
            answer = run_llm(general_template(), 0.2, question=question)
            append_history(name, "user", question, [])
            append_history(name, "assistant", answer, [])
            return jsonify({"answer": answer, "sources": [], "chunks": 0})

        embed_model = cfg("embedding_model")
        
        # Téléchargement automatique du modèle d'embedding si nécessaire
        if not _model_exists(embed_model):
            print(f"Modèle d'embedding {embed_model} introuvable. Téléchargement en cours...")
            ok, msg = _pull_model(embed_model)
            if not ok:
                return jsonify({"error": f"Le modèle '{embed_model}' est introuvable dans le registre Ollama"}), 500

        k = cfg("retrieval_k")
        embeddings = OllamaEmbeddings(model=embed_model)
        vectorstore = Chroma(persist_directory=str(db), embedding_function=embeddings)
        retriever = vectorstore.as_retriever(search_type="mmr", search_kwargs={"k": k, "fetch_k": max(k * 4, 30)})

        docs = retriever.invoke(question)
        context = "\n\n".join(d.page_content for d in docs)
        sources = list({d.metadata.get("source", "?") for d in docs})

        if not context.strip():
            answer = run_llm(general_template(), 0.2, question=question)
            append_history(name, "user", question, [])
            append_history(name, "assistant", answer, [])
            return jsonify({"answer": answer, "sources": [], "chunks": 0})

        template = report_template() if mode == "report" else qa_template()
        answer = run_llm(template, 0.0, context=context, question=question)
        append_history(name, "user", question, [])
        append_history(name, "assistant", answer, sources)

        return jsonify({"answer": answer, "sources": sources, "chunks": len(docs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _latex_simple(expr):
    """Convertit une expression LaTeX simple en caractères Unicode (flèches, symboles, exposants, indices)."""
    expr = expr.strip()
    # Gérer \text{...}
    expr = re.sub(r'\\text\{([^}]*)\}', r'\1', expr)
    # Gérer \sqrt{...}
    expr = re.sub(r'\\sqrt\{([^}]*)\}', r'√(\1)', expr)
    # Gérer les exposants ^{...} ou ^X
    def _sup(match):
        """Convertit une sequence en caracteres exposants Unicode."""
        body = match.group(1)
        sup_map = {
            '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
            '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
            'n': 'ⁿ', 'i': 'ⁱ',
        }
        return ''.join(sup_map.get(c, c) for c in body)

    expr = re.sub(r'\^{([^}]+)}', _sup, expr)
    expr = re.sub(r'\^(\w)', _sup, expr)
    # Gérer les indices _{...} ou _X
    def _sub(match):
        """Convertit une sequence en caracteres indices Unicode."""
        body = match.group(1)
        sub_map = {
            '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄',
            '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
        }
        return ''.join(sub_map.get(c, c) for c in body)

    expr = re.sub(r'_\{([^}]+)\}', _sub, expr)
    expr = re.sub(r'_(\w)', _sub, expr)
    # Remplacer les commandes LaTeX connues par leur équivalent Unicode
    latex_map = {
        r"\rightarrow": "→", r"\leftarrow": "←",
        r"\Rightarrow": "⇒", r"\Leftarrow": "⇐",
        r"\Leftrightarrow": "⇔",
        r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
        r"\epsilon": "ε", r"\lambda": "λ", r"\mu": "μ",
        r"\pi": "π", r"\sigma": "σ", r"\omega": "ω",
        r"\infty": "∞", r"\pm": "±", r"\times": "×", r"\div": "÷",
        r"\neq": "≠", r"\leq": "≤", r"\geq": "≥", r"\approx": "≈",
        r"\equiv": "≡", r"\in": "∈", r"\notin": "∉",
        r"\subset": "⊂", r"\supset": "⊃", r"\cup": "∪", r"\cap": "∩",
        r"\emptyset": "∅", r"\forall": "∀", r"\exists": "∃",
        r"\nabla": "∇", r"\partial": "∂",
        r"\int": "∫", r"\sum": "∑", r"\prod": "∏",
        r"\sqrt": "√", r"\cdots": "⋯", r"\ldots": "…",
        r"\degree": "°", r"\deg": "°",
    }
    for cmd, char in latex_map.items():
        expr = expr.replace(cmd, char)
    # Supprimer les commandes LaTeX restantes et les accolades
    expr = re.sub(r'\\[a-zA-Z]+', '', expr)
    expr = expr.replace('{', '').replace('}', '')
    return expr

def _inline(text, styles=None):
    """Convertit le markdown en ligne (gras, italique, code, LaTeX) en XML ReportLab valide, sans balises mal imbriquÃ©es."""
    # D'abord : remplacer les blocs mathÃ©matiques LaTeX $...$
    text = re.sub(r'\$([^\$]+)\$', lambda m: _latex_simple(m.group(1)), text)
    # Ã‰chapper les caractÃ¨res XML spÃ©ciaux
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    result = []
    pos = 0
    bold_open = False
    italic_open = False

    while pos < len(text):
        # Span de code â€” prioritÃ© maximale, pas d'imbrication Ã  l'intÃ©rieur
        if text[pos] == '`':
            end = text.find('`', pos + 1)
            if end != -1:
                code_content = text[pos + 1:end]
                result.append(f'<font name="Courier">{code_content}</font>')
                pos = end + 1
                continue

        # Gras+italique combinÃ© ***
        if text[pos:pos + 3] == '***':
            if bold_open and italic_open:
                result.append('</i></b>')
                bold_open = False
                italic_open = False
            elif not bold_open and not italic_open:
                result.append('<b><i>')
                bold_open = True
                italic_open = True
            else:
                result.append('***')  # MalformÃ©, traiter comme texte littÃ©ral
            pos += 3
            continue

        # Gras **
        if text[pos:pos + 2] == '**':
            if bold_open:
                if italic_open:
                    # Ne peut pas fermer le gras tant que l'italique est ouvert â€” littÃ©ral
                    result.append('**')
                else:
                    result.append('</b>')
                    bold_open = False
            else:
                result.append('<b>')
                bold_open = True
            pos += 2
            continue

        # Italique * (astÃ©risque unique, ne faisant pas partie de **)
        if (text[pos] == '*'
                and (pos == 0 or text[pos - 1] != '*')
                and (pos + 1 >= len(text) or text[pos + 1] != '*')):
            if italic_open:
                result.append('</i>')
                italic_open = False
            else:
                result.append('<i>')
                italic_open = True
            pos += 1
            continue

        result.append(text[pos])
        pos += 1

    # Fermer les balises encore ouvertes dans le bon ordre (sÃ©curitÃ©)
    if italic_open:
        result.append('</i>')
    if bold_open:
        result.append('</b>')

    return ''.join(result)

def _make_code_block(code_text, lang=""):
    """GÃ©nÃ¨re un bloc de code stylisÃ© (fond sombre, police monospace, label de langage) sous forme de tableau ReportLab."""
    from reportlab.platypus import Paragraph, Table, TableStyle, Spacer
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle  # â† C'Ã©tait cette ligne qui manquait

    # Ã‰chapper le XML Ã  l'intÃ©rieur du code
    safe = code_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Remplacer les sauts de ligne par <br/> pour le Paragraph
    safe_br = safe.replace("\n", "<br/>")
    # Construire le label de langage Ã©ventuel
    if lang:
        label = f'<font name="Courier" size="8" color="#888888">{lang}</font>'
    else:
        label = ""
    inner = Paragraph(
        f"{label}<br/><font name='Courier' size='9'>{safe_br}</font>",
        ParagraphStyle("CodeBlock", fontName="Courier", fontSize=9,
                       textColor=colors.HexColor("#d4d4d8"),
                       leading=13, leftIndent=8, rightIndent=8,
                       spaceBefore=0, spaceAfter=0),
    )
    tbl = Table([[inner]], colWidths=[440])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1e1e2e")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("ROUNDEDCORNERS", [4]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#3a3a4a")),
    ]))
    return [Spacer(1, 6), tbl, Spacer(1, 6)]

def _parse_table(table_lines, styles):
    """Analyse des lignes de tableau Markdown et retourne un objet Table ReportLab stylisÃ© avec en-tÃªte et alternance de couleurs."""
    from reportlab.platypus import Paragraph, Table, TableStyle
    from reportlab.lib import colors

    # Le LLM met parfois des retours Ã  la ligne DANS une cellule,
    # ce qui produit une ligne ne commenÃ§ant pas par "|".
    # On la rattache alors Ã  la derniÃ¨re cellule de la ligne prÃ©cÃ©dente.
    merged_lines = []
    for line in table_lines:
        stripped = line.strip()
        if stripped.startswith("|") or not merged_lines:
            merged_lines.append(stripped)
        else:
            merged_lines[-1] += " " + stripped

    rows = []
    for line in merged_lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return None

    # Ignorer la ligne de sÃ©paration (ex : | :--- | :--- | )
    data_rows = []
    for idx, row in enumerate(rows):
        if idx == 1 and all(re.match(r'^:?-+:?$', c.strip()) for c in row):
            continue
        data_rows.append([Paragraph(_inline(c), styles["TableCell"]) for c in row])
    if not data_rows:
        return None

    ncols = len(data_rows[0])

    # Au lieu de largeurs Ã©gales qui Ã©crasent le texte,
    # on donne moins aux petites colonnes et plus aux grandes.
    avail_w = 595.27 - 4.4 * 28.35  # Largeur A4 moins les marges â‰ˆ 470pt
    if ncols == 2:
        col_widths = [avail_w * 0.3, avail_w * 0.7]
    elif ncols == 3:
        col_widths = [avail_w * 0.25, avail_w * 0.35, avail_w * 0.4]
    elif ncols == 4:
        col_widths = [avail_w * 0.2, avail_w * 0.27, avail_w * 0.28, avail_w * 0.25]
    elif ncols >= 5:
        w = avail_w / ncols
        col_widths = [w] * ncols
    else:
        col_widths = [avail_w] * ncols

    tbl = Table(data_rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#ffffff")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#3a3a4a")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#ffffff"), colors.HexColor("#f5f5fa")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl

def _md_to_story(content, styles):
    """Convertit une chaÃ®ne Markdown complÃ¨te en une liste d'Ã©lÃ©ments ReportLab (Paragraph, Table, Spacer, etc.) prÃªts Ã  Ãªtre construits en PDF."""
    from reportlab.platypus import (
        Paragraph, Spacer, HRFlowable, ListFlowable, ListItem,
    )
    from reportlab.lib import colors

    story = []
    lines = content.splitlines()
    i = 0
    bullet_buf = []
    numbered_buf = []

    def flush_bullets():
        """Vide le tampon de liste Ã  puces et l'ajoute au story sous forme de ListFlowable."""
        nonlocal bullet_buf
        if not bullet_buf:
            return
        items = [ListItem(Paragraph(_inline(t), styles["BulletBody"]),
                          leftIndent=12, bulletIndent=0) for t in bullet_buf]
        story.append(ListFlowable(items, bulletType="bullet",
                                  leftIndent=18, bulletFontSize=8, spaceBefore=2, spaceAfter=2))
        story.append(Spacer(1, 4))
        bullet_buf = []

    def flush_numbered():
        """Vide le tampon de liste numÃ©rotÃ©e et l'ajoute au story sous forme de ListFlowable avec chiffres."""
        nonlocal numbered_buf
        if not numbered_buf:
            return
        items = []
        for idx, t in enumerate(numbered_buf, 1):
            items.append(ListItem(
                Paragraph(f"<b>{idx}.</b>&nbsp;&nbsp;{_inline(t)}", styles["BulletBody"]),
                leftIndent=12, bulletIndent=0,
            ))
        story.append(ListFlowable(items, bulletType="bullet",
                                  leftIndent=18, bulletFontSize=8, spaceBefore=2, spaceAfter=2))
        story.append(Spacer(1, 4))
        numbered_buf = []

    def flush_all_lists():
        """Vide les deux tampons de listes (puces et numÃ©rotÃ©es)."""
        flush_bullets()
        flush_numbered()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Ligne vide
        if not stripped:
            flush_all_lists()
            story.append(Spacer(1, 6))
            i += 1
            continue

        # Bloc de code dÃ©limitÃ© (ouverture ```)
        if re.match(r'^```', stripped):
            flush_all_lists()
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not re.match(r'^```', lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            i += 1  # Sauter la ligne de fermeture ```
            story.extend(_make_code_block("\n".join(code_lines), lang))
            continue

        # DÃ©tection de tableau Markdown : ligne commenÃ§ant par | et la suivante aussi
        if stripped.startswith("|") and (i + 1 < len(lines) and lines[i + 1].strip().startswith("|")):
            flush_all_lists()
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            tbl = _parse_table(table_lines, styles)
            if tbl:
                story.append(Spacer(1, 6))
                story.append(tbl)
                story.append(Spacer(1, 6))
            continue

        # Ligne horizontale (--- ou plus)
        if re.match(r"^-{3,}$", stripped):
            flush_all_lists()
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=colors.HexColor("#cccccc"), spaceAfter=6))
            i += 1
            continue

        # Titre H1
        if stripped.startswith("# ") and not stripped.startswith("## "):
            flush_all_lists()
            story.append(Paragraph(_inline(stripped[2:]), styles["H1"]))
            story.append(Spacer(1, 8))
            i += 1
            continue

        # Titre H2
        if stripped.startswith("## ") and not stripped.startswith("### "):
            flush_all_lists()
            story.append(Spacer(1, 6))
            story.append(Paragraph(_inline(stripped[3:]), styles["H2"]))
            story.append(HRFlowable(width="100%", thickness=0.4,
                                    color=colors.HexColor("#e0e0e0"), spaceAfter=4))
            i += 1
            continue

        # Titre H3
        if stripped.startswith("### ") and not stripped.startswith("#### "):
            flush_all_lists()
            story.append(Paragraph(_inline(stripped[4:]), styles["H3"]))
            story.append(Spacer(1, 4))
            i += 1
            continue

        # Titre H4
        if stripped.startswith("#### "):
            flush_all_lists()
            story.append(Paragraph(_inline(stripped[5:]), styles["H4"]))
            story.append(Spacer(1, 3))
            i += 1
            continue

        # Ã‰lÃ©ment de liste Ã  puces (- ou *)
        if re.match(r"^[-*]\s", stripped):
            flush_numbered()
            bullet_buf.append(stripped[2:])
            i += 1
            continue

        # Ã‰lÃ©ment de liste numÃ©rotÃ©e (1. 2. etc.)
        if re.match(r"^\d+\.\s", stripped):
            flush_bullets()
            numbered_buf.append(re.sub(r"^\d+\.\s", "", stripped))
            i += 1
            continue

        # Paragraphe normal
        flush_all_lists()
        story.append(Paragraph(_inline(stripped), styles["Body"]))
        story.append(Spacer(1, 4))
        i += 1

    flush_all_lists()
    return story

@app.route("/api/projects/<name>/export-pdf", methods=["POST"])
def export_pdf(name):
    """GÃ©nÃ¨re un PDF stylisÃ© Ã  partir d'un contenu Markdown (avec page de couverture, en-tÃªte/pied de page, tableaux, blocs de code, symboles LaTeX)."""
    data = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    title = data.get("title", "Rapport")

    if not content:
        return jsonify({"error": "Contenu vide."}), 400

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, HRFlowable, ListFlowable, ListItem,
            Table, TableStyle,
        )

        W, H = A4
        buffer = io.BytesIO()

        def on_page(canvas, doc):
            """Dessine la barre d'en-tÃªte sombre avec le titre, et le pied de page avec numÃ©ro de page et date."""
            canvas.saveState()
            # Barre d'en-tÃªte
            canvas.setFillColor(colors.HexColor("#1a1a2e"))
            canvas.rect(0, H - 1.4 * cm, W, 1.4 * cm, fill=1, stroke=0)
            canvas.setFont("Helvetica-Bold", 9)
            canvas.setFillColor(colors.white)
            safe_title = title[:80]
            canvas.drawString(2 * cm, H - 0.95 * cm, safe_title)
            # Pied de page
            canvas.setFillColor(colors.HexColor("#888888"))
            canvas.setFont("Helvetica", 8)
            canvas.drawString(2 * cm, 0.8 * cm, f"Page {doc.page}")
            canvas.drawRightString(W - 2 * cm, 0.8 * cm, time.strftime("%d/%m/%Y"))
            canvas.restoreState()

        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=2.2 * cm, leftMargin=2.2 * cm,
            topMargin=2.4 * cm, bottomMargin=1.8 * cm,
            title=title,
        )

        accent = colors.HexColor("#4f46e5")  # indigo
        dark = colors.HexColor("#1a1a2e")
        text = colors.HexColor("#222233")

        custom = {
            "H1": ParagraphStyle("H1",
                fontName="Helvetica-Bold", fontSize=18,
                textColor=dark, leading=22, spaceAfter=4, alignment=TA_LEFT),
            "H2": ParagraphStyle("H2",
                fontName="Helvetica-Bold", fontSize=13,
                textColor=accent, leading=16, spaceBefore=8, spaceAfter=2),
            "H3": ParagraphStyle("H3",
                fontName="Helvetica-Bold", fontSize=11,
                textColor=text, leading=14, spaceBefore=6, spaceAfter=2),
            "H4": ParagraphStyle("H4",
                fontName="Helvetica-Bold", fontSize=10.5,
                textColor=colors.HexColor("#444466"), leading=13,
                spaceBefore=5, spaceAfter=2),
            "Body": ParagraphStyle("Body",
                fontName="Helvetica", fontSize=10,
                textColor=text, leading=15, alignment=TA_JUSTIFY),
            "BulletBody": ParagraphStyle("BulletBody",
                fontName="Helvetica", fontSize=10,
                textColor=text, leading=14),
            "TableCell": ParagraphStyle("TableCell",
                fontName="Helvetica", fontSize=9,
                textColor=text, leading=12),
        }

        story = []

        # BanniÃ¨re sombre avec le titre
        banner_data = [[Paragraph(
            f'<font color="white"><b>{title}</b></font>',
            ParagraphStyle("Banner", fontName="Helvetica-Bold", fontSize=20,
                           textColor=colors.white, leading=24, alignment=TA_CENTER)
        )]]
        banner = Table(banner_data, colWidths=[W - 4.4 * cm])
        banner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), dark),
            ("TOPPADDING", (0, 0), (-1, -1), 18),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
            ("LEFTPADDING", (0, 0), (-1, -1), 16),
            ("RIGHTPADDING", (0, 0), (-1, -1), 16),
            ("ROUNDEDCORNERS", [6]),
        ]))
        story.append(Spacer(1, 1.2 * cm))
        story.append(banner)
        story.append(Spacer(1, 0.4 * cm))

        # Ligne de mÃ©tadonnÃ©es (nom du projet + date de gÃ©nÃ©ration)
        meta_text = (
            f'<font color="#888888" size="9">'
            f'Projet&nbsp;: <b>{name}</b>&nbsp;&nbsp;|&nbsp;&nbsp;'
            f'Genere le : <b>{time.strftime("%d/%m/%Y a %H:%M")}</b>'
            f'</font>'
        )
        story.append(Paragraph(meta_text,
            ParagraphStyle("Meta", fontName="Helvetica", fontSize=9,
                           textColor=colors.HexColor("#888888"), alignment=TA_CENTER)))
        story.append(Spacer(1, 0.3 * cm))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=accent, spaceAfter=16))

        story.extend(_md_to_story(content, custom))

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        buffer.seek(0)

        safe_filename = re.sub(r"[^\w\-]", "_", name) + "_rapport.pdf"
        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=safe_filename,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    _base().mkdir(parents=True, exist_ok=True)
    print(f"RAG Local — LLM: {cfg('llm_model')} | http://127.0.0.1:5000")
    app.run(debug=False, port=5000, threaded=True)
