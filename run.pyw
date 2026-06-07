"""
Lanceur local RAG.
Double-cliquez sur run.pyw pour démarrer.
"""

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).parent
os.chdir(BASE)

APP_URL = "http://127.0.0.1:5000"
OLLAMA_API = "http://127.0.0.1:11434/"
OLLAMA_INSTALLER_NAME = "OllamaSetup.exe"
OLLAMA_DOWNLOAD_URLS = [
    "https://ollama.com/download/OllamaSetup.exe",
    "https://ollama.ai/download/OllamaSetup.exe",
]

def _port_open(port):
    """Retourne True si le port local est ouvert."""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0

def _ollama_up():
    """Retourne True si l'API HTTP d'Ollama repond."""
    try:
        urllib.request.urlopen(OLLAMA_API, timeout=2)
        return True
    except Exception:
        return False

def _find_ollama():
    """Trouve l'executable Ollama dans le PATH ou les chemins courants."""
    which = shutil.which("ollama")
    if which:
        return which

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "C:/x")) / "Programs/Ollama/ollama.exe",
        Path(os.environ.get("USERPROFILE", "C:/x")) / "AppData/Local/Programs/Ollama/ollama.exe",
        Path("C:/Program Files/Ollama/ollama.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""

def _pkg_ok(module_name):
    """Retourne True si un module Python est importable."""
    return importlib.util.find_spec(module_name) is not None

def _download_with_progress(url, destination, on_progress):
    """Telecharge un fichier et remonte la progression via callback."""
    req = urllib.request.Request(url, headers={"User-Agent": "RAG-Local-Launcher/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk_size = 1024 * 128
        with open(destination, "wb") as out:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    on_progress(min(100, int(downloaded * 100 / total)))

def _install_ollama(log, set_sub_progress):
    """Installe Ollama en mode silencieux sur Windows."""
    installer = BASE / OLLAMA_INSTALLER_NAME

    log("  Téléchargement de l'installateur Ollama...", "warn")
    set_sub_progress(2)

    for idx, url in enumerate(OLLAMA_DOWNLOAD_URLS, start=1):
        try:
            _download_with_progress(
                url,
                installer,
                lambda p: set_sub_progress(5 + int(p * 0.70)),
            )
            break
        except Exception as exc:
            if installer.exists():
                installer.unlink(missing_ok=True)
            if idx == len(OLLAMA_DOWNLOAD_URLS):
                return False, "Impossible de télécharger l'installateur Ollama"

    log("  Installation d'Ollama...", "warn")
    set_sub_progress(80)
    try:
        result = subprocess.run(
            [str(installer), "/VERYSILENT", "/NORESTART"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            if detail:
                log("  " + detail.splitlines()[-1], "err")
            return False, f"Installation échouée (code {result.returncode})"
        time.sleep(2)
        set_sub_progress(100)
        return True, "Ollama installé avec succès"
    except subprocess.TimeoutExpired:
        return False, "L'installation d'Ollama a expiré"
    except Exception as exc:
        return False, f"Erreur d'installation d'Ollama : {exc}"
    finally:
        installer.unlink(missing_ok=True)

def _check_deps(log, set_sub_progress):
    """Installe les dependances Python manquantes."""
    packages = {
        "flask": "flask",
        "flask_cors": "flask-cors",
        "langchain": "langchain",
        "langchain_ollama": "langchain-ollama",
        "langchain_chroma": "langchain-chroma",
        "langchain_community": "langchain-community",
        "langchain_text_splitters": "langchain-text-splitters",
        "chromadb": "chromadb",
        "pypdf": "pypdf",
        "reportlab": "reportlab",
    }

    missing = [pip_name for mod_name, pip_name in packages.items() if not _pkg_ok(mod_name)]
    if not missing:
        set_sub_progress(100)
        return True, "Toutes les dépendances requises sont installées"

    req_file = BASE / "requirements.txt"
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if req_file.exists():
        cmd += ["-r", str(req_file)]
    else:
        cmd += missing

    log(f"  Installation de {len(missing)} paquet(s) manquant(s)...", "warn")
    set_sub_progress(5)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    progress = 10
    lines_seen = 0
    tail = []

    if proc.stdout:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            tail.append(line)
            tail = tail[-8:]

            low = line.lower()
            if "collecting" in low or "requirement already satisfied" in low:
                lines_seen += 1
                progress = min(70, 10 + lines_seen * 4)
                set_sub_progress(progress)
            elif "downloading" in low or "building wheel" in low:
                progress = min(85, progress + 3)
                set_sub_progress(progress)
            elif "installing collected packages" in low:
                set_sub_progress(92)
            elif "successfully installed" in low:
                set_sub_progress(100)

    code = proc.wait()
    if code != 0:
        for line in tail[-5:]:
            log("  " + line, "err")
        return False, "L'installation via pip a échoué"

    set_sub_progress(100)
    return True, f"{len(missing)} paquet(s) installé(s)"

def _check_ollama(log, overwrite, quitting, set_sub_progress):
    """Verifie qu'Ollama est installe et que son API est accessible."""
    if _ollama_up():
        set_sub_progress(100)
        return True, "Ollama est déjà démarré"

    exe = _find_ollama()
    if not exe:
        log("  Ollama introuvable. Installation en cours...", "warn")
        ok, msg = _install_ollama(log, lambda p: set_sub_progress(int(p * 0.70)))
        log(("  OK  " if ok else "  X  ") + msg, "ok" if ok else "err")
        if not ok:
            return False, msg
        exe = _find_ollama()
        if not exe:
            return False, "Exécutable Ollama introuvable après installation"
    else:
        set_sub_progress(45)

    log("  Démarrage de ollama serve...", "warn")
    try:
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return False, f"Impossible de lancer Ollama : {exc}"

    log("  Attente de l'API Ollama...", "warn")
    for second in range(1, 46):
        if quitting():
            return False, "Cancelled"
        if _ollama_up():
            set_sub_progress(100)
            return True, f"Ollama prêt (environ {second}s)"
        overwrite(f"  Attente de l'API Ollama... {second}/45s", "warn")
        set_sub_progress(min(98, 50 + int(second * 1.08)))
        time.sleep(1)

    return False, "Ollama n'a pas répondu en 45 secondes"

def _start_flask(on_error):
    """Demarre le serveur Flask dans un thread en arriere-plan."""
    def _runner():
        """Execute Flask et remonte les erreurs du thread."""
        try:
            from app import app as flask_app

            flask_app.run(
                host="127.0.0.1",
                port=5000,
                debug=False,
                threaded=True,
                use_reloader=False,
            )
        except Exception as exc:
            on_error(f"  X  Erreur Flask : {exc}")

    threading.Thread(target=_runner, daemon=True).start()

def _run_gui():
    """Lance l'interface desktop avec progression et logs."""
    import tkinter as tk
    from tkinter import ttk

    class Launcher:
        BG = "#0d0d0f"
        BG2 = "#141418"
        ACC = "#7c6af7"
        GRN = "#5ad4a0"
        YLW = "#c9a227"
        RED = "#f25f5c"
        TXT = "#e8e8f0"
        DIM = "#888899"

        def __init__(self):
            """Initialise la fenetre du launcher."""
            self.root = tk.Tk()
            self.root.title("RAG Local")
            self.root.geometry("520x390")
            self.root.resizable(False, False)
            self.root.configure(bg=self.BG)
            self.root.protocol("WM_DELETE_WINDOW", self.quit)

            self._quitting = False
            self._progress_value = 0.0

            self._build()

        def _build(self):
            """Construit les widgets de l'interface."""
            header = tk.Frame(self.root, bg=self.BG2)
            header.pack(fill="x")
            tk.Label(
                header,
                text="RAG",
                bg=self.BG2,
                fg=self.ACC,
                font=("Segoe UI", 14, "bold"),
            ).pack(side="left", padx=(14, 8), pady=10)
            tk.Label(
                header,
                text="Lanceur local",
                bg=self.BG2,
                fg=self.TXT,
                font=("Segoe UI", 12),
            ).pack(side="left", pady=10)

            self._status = tk.Label(
                self.root,
                text="Préparation de l'installation...",
                bg=self.BG,
                fg=self.DIM,
                font=("Segoe UI", 9),
                anchor="w",
            )
            self._status.pack(fill="x", padx=14, pady=(10, 4))

            style = ttk.Style()
            style.theme_use("clam")
            style.configure(
                "RAG.Horizontal.TProgressbar",
                troughcolor=self.BG2,
                bordercolor=self.BG2,
                background=self.ACC,
                lightcolor=self.ACC,
                darkcolor=self.ACC,
            )

            pb_wrap = tk.Frame(self.root, bg=self.BG)
            pb_wrap.pack(fill="x", padx=14)

            self._progress = ttk.Progressbar(
                pb_wrap,
                mode="determinate",
                maximum=100,
                length=470,
                style="RAG.Horizontal.TProgressbar",
            )
            self._progress.pack(side="left", fill="x", expand=True)
            self._percent = tk.Label(
                pb_wrap,
                text="0%",
                bg=self.BG,
                fg=self.DIM,
                font=("Segoe UI", 9),
                width=5,
                anchor="e",
            )
            self._percent.pack(side="left", padx=(8, 0))

            self._txt = tk.Text(
                self.root,
                bg=self.BG,
                fg=self.DIM,
                font=("Consolas", 9),
                relief="flat",
                bd=0,
                state="disabled",
                wrap="word",
                height=14,
            )
            self._txt.pack(fill="both", expand=True, padx=14, pady=(8, 8))
            for tag, color, cfg in [
                ("ok", self.GRN, {}),
                ("warn", self.YLW, {}),
                ("err", self.RED, {}),
                ("head", self.TXT, {"font": ("Consolas", 9, "bold")}),
            ]:
                self._txt.tag_configure(tag, foreground=color, **cfg)

            controls = tk.Frame(self.root, bg=self.BG2, pady=8)
            controls.pack(fill="x")
            self._start_btn = tk.Button(
                controls,
                text="Démarrer l'application",
                bg=self.ACC,
                fg="white",
                relief="flat",
                font=("Segoe UI", 9, "bold"),
                padx=14,
                pady=5,
                state="disabled",
                command=lambda: webbrowser.open(APP_URL),
            )
            self._start_btn.pack(side="left", padx=(12, 6))
            tk.Button(
                controls,
                text="Arrêter",
                bg=self.BG,
                fg=self.DIM,
                relief="flat",
                font=("Segoe UI", 9),
                padx=12,
                pady=5,
                command=self.quit,
            ).pack(side="left")
            tk.Label(
                controls,
                text=APP_URL,
                bg=self.BG2,
                fg=self.DIM,
                font=("Segoe UI", 8),
            ).pack(side="right", padx=12)

        def _safe_ui(self, fn):
            """Planifie une mise a jour UI de facon thread-safe."""
            self.root.after(0, fn)

        def _write(self, msg, tag="", replace_last=False):
            """Ecrit une ligne dans la console visuelle."""
            def _do():
                """Applique l'ecriture dans le widget texte principal."""
                self._txt.configure(state="normal")
                if replace_last and float(self._txt.index("end-1c")) > 1.0:
                    self._txt.delete("end-2l", "end-1c")
                self._txt.insert("end", msg + "\n", tag)
                self._txt.see("end")
                self._txt.configure(state="disabled")

            self._safe_ui(_do)

        def log(self, msg, tag=""):
            """Ajoute une ligne de log."""
            self._write(msg, tag)

        def overwrite_last(self, msg, tag=""):
            """Remplace la derniere ligne de log (progression)."""
            self._write(msg, tag, replace_last=True)

        def set_progress(self, value, status_text=None):
            """Met a jour la barre de progression et le statut."""
            value = max(self._progress_value, min(100.0, float(value)))
            self._progress_value = value

            def _do():
                """Rafraichit visuellement la progression dans l'UI."""
                self._progress["value"] = value
                self._percent.configure(text=f"{int(value)}%")
                if status_text:
                    self._status.configure(text=status_text)

            self._safe_ui(_do)

        def sub_progress(self, start, end):
            """Retourne un mappeur de progression locale vers globale."""
            span = max(0.0, end - start)

            def _update(pct):
                """Convertit un pourcentage local en progression globale."""
                pct = max(0.0, min(100.0, float(pct)))
                self.set_progress(start + (pct / 100.0) * span)

            return _update

        def set_status(self, text, color=None):
            """Met a jour le texte de statut de l'interface."""
            def _do():
                """Applique le texte/couleur de statut dans la fenetre."""
                self._status.configure(text=text)
                if color:
                    self._status.configure(fg=color)

            self._safe_ui(_do)

        def mark_ready(self):
            """Active le bouton de demarrage quand tout est pret."""
            self.set_progress(100, "Installation terminée. Cliquez sur Démarrer l'application.")

            def _do():
                """Debloque le bouton de lancement et retire le focus force."""
                self._start_btn.configure(state="normal")
                self.root.attributes("-topmost", False)

            self._safe_ui(_do)

        def quit(self):
            """Arrete proprement le launcher."""
            self._quitting = True
            self.root.destroy()

        def _setup(self):
            """Orchestre la sequence complete d'initialisation."""
            if _port_open(5000):
                self.log("OK  Le serveur de l'application est déjà démarré", "ok")
                self.set_status("Application déjà active. Cliquez sur Démarrer l'application.", self.GRN)
                self.set_progress(100)
                self.mark_ready()
                return

            self.log("[ 1/3 ] Vérification des dépendances Python...", "head")
            dep_progress = self.sub_progress(0, 55)
            dep_progress(0)
            ok, msg = _check_deps(self.log, dep_progress)
            self.log(("  OK  " if ok else "  X  ") + msg, "ok" if ok else "err")
            if not ok:
                self.set_status("L'installation des dépendances a échoué.", self.RED)
                return
            if self._quitting:
                return

            self.log("[ 2/3 ] Vérification d'Ollama...", "head")
            ollama_progress = self.sub_progress(55, 85)
            ollama_progress(0)
            ok, msg = _check_ollama(self.log, self.overwrite_last, lambda: self._quitting, ollama_progress)
            self.log(("  OK  " if ok else "  X  ") + msg, "ok" if ok else "err")
            if not ok:
                self.set_status("Ollama n'est pas prêt. Corrigez-le puis réessayez.", self.RED)
                return
            if self._quitting:
                return

            self.log("[ 3/3 ] Démarrage du serveur API...", "head")
            flask_progress = self.sub_progress(85, 100)
            flask_progress(10)
            _start_flask(lambda e: self.log(e, "err"))

            up = False
            for i in range(1, 61):
                if self._quitting:
                    return
                if _port_open(5000):
                    up = True
                    break
                flask_progress(min(95, 10 + i * 1.4))
                self.set_status(f"Attente du serveur de l'application... {i}/60", self.DIM)
                time.sleep(0.5)

            if not up:
                self.log("  X  Le serveur n'a pas démarré. Consultez les journaux ci-dessus.", "err")
                self.set_status("Le serveur n'a pas démarré.", self.RED)
                return

            self.log("  OK  Serveur prêt.", "ok")
            self.set_status("Installation terminée. Cliquez sur Démarrer l'application.", self.GRN)
            self.mark_ready()

        def run(self):
            """Demarre la sequence setup puis la boucle Tkinter."""
            threading.Thread(target=self._setup, daemon=True).start()
            self.root.mainloop()

    Launcher().run()

def _run_headless():
    """Lance la sequence setup sans interface graphique."""
    def log(msg, *_):
        """Affiche un log en mode console."""
        print(msg)

    print("RAG Local - démarrage en mode sans interface graphique")

    ok, msg = _check_deps(log, lambda p: None)
    print(("OK  " if ok else "X  ") + msg)
    if not ok:
        sys.exit(1)

    ok, msg = _check_ollama(log, log, lambda: False, lambda p: None)
    print(("OK  " if ok else "X  ") + msg)
    if not ok:
        sys.exit(1)

    _start_flask(print)
    for _ in range(40):
        if _port_open(5000):
            break
        time.sleep(0.5)

    if _port_open(5000):
        webbrowser.open(APP_URL)
        print(f"Application disponible à {APP_URL} - laissez ce processus ouvert")
        while True:
            time.sleep(5)

    print("Le serveur n'a pas démarré")
    sys.exit(1)

try:
    _run_gui()
except ImportError:
    _run_headless()
