"""Voix du bot : synthèse vocale (OmniVoice ou Chatterbox), lancée dans un processus séparé (environnement conda dédié).

Module indépendant de Discord. Le texte est nettoyé, converti en WAV par tools/tts_<moteur>.py (exécuté avec le Python
de l'environnement du moteur) et mis en cache dans la séance : le même texte n'est synthétisé qu'une fois.
"""
import atexit
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .campagne import Campaign
from . import modeles
from .config import SET

ROOT = Path(__file__).resolve().parent.parent
ENGINES = {   # moteur : (script, fichier du Python de son environnement, installeur)
    "omnivoice": ("tts_omnivoice.py", ".voix_python_omnivoice", "install_omnivoice.bat"),
    "chatterbox": ("tts_chatterbox.py", ".voix_python", "install_voix.bat"),
}
SPEED_APPLIED = "VITESSE_APPLIQUEE"       # écrit par le worker quand le moteur a lui-même réglé la vitesse


def engine() -> str:
    return SET["voix.moteur"] if SET["voix.moteur"] in ENGINES else "omnivoice"
REFERENCE_NAMES = ("voix.wav", "voix.mp3", "voix.flac")


class VoixError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def python_exe() -> str:
    """Python de l'environnement du moteur : réglage `voix.python`, sinon le fichier écrit par l'installeur du moteur,
    sinon l'interpréteur courant."""
    configured = SET["voix.python"].strip()
    if configured:
        return configured
    try:
        saved = (ROOT / ENGINES[engine()][1]).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        saved = ""
    return saved or sys.executable


NO_VOICE = ("défaut", "defaut")         # nom réservé : pas d'échantillon, voix par défaut du moteur
VOICE_EXTS = (".wav", ".mp3", ".flac")


def voices_dir() -> Path:
    """Dossier commun des voix du bot (réglage `voix.dossier`) : un fichier `voix.<nom>.wav` (ou .mp3/.flac) par voix,
    avec en option `voix.<nom>.txt` (transcription de l'échantillon)."""
    return Path(SET["voix.dossier"])


def list_voices() -> list:
    """Noms des voix disponibles (triés), d'après les fichiers `voix.<nom>.<extension>` du dossier commun."""
    names = set()
    try:
        for f in voices_dir().iterdir():
            if f.is_file() and f.suffix.lower() in VOICE_EXTS and f.name.lower().startswith("voix.") and len(f.stem) > 5:
                names.add(f.stem[5:])
    except OSError:
        pass
    return sorted(names, key=str.lower)


def voice_path(name: str) -> Path:
    """Fichier d'une voix du dossier commun ; VoixError (avec la liste des voix) si elle n'existe pas."""
    clean = (name or "").strip()
    for n in list_voices():
        if n.lower() == clean.lower():
            for ext in VOICE_EXTS:
                if (voices_dir() / f"voix.{n}{ext}").is_file():
                    return voices_dir() / f"voix.{n}{ext}"
    available = ", ".join(list_voices()) or f"aucune (dépose des fichiers `voix.<nom>.wav` dans `{voices_dir()}`)"
    raise VoixError(f"Voix « {clean} » introuvable. Disponibles : {available}.")


def campaign_voice(campaign: Campaign | None) -> str:
    """Nom de la voix choisie pour la campagne (« » = aucune), enregistré dans campagne.json."""
    return str(campaign.meta.get("voix") or "") if campaign is not None else ""


def voice_reference(campaign: Campaign | None, voice: str | None = None) -> Path | None:
    """Échantillon de voix à imiter. Ordre : voix demandée explicitement (`voice`) > voix de la campagne (campagne.json) >
    ancien `voix.wav` dans le dossier de la campagne > réglage `voix.voix_reference` > voix par défaut du moteur (None).
    Le nom « défaut » désigne explicitement la voix par défaut du moteur (aucun échantillon)."""
    if voice:
        return None if voice.strip().lower() in NO_VOICE else voice_path(voice)
    chosen = campaign_voice(campaign)
    if chosen:
        if chosen.lower() in NO_VOICE:
            return None
        try:
            return voice_path(chosen)
        except VoixError:
            print(f"[bot] voix : la voix « {chosen} » de la campagne est introuvable, repli sur la voix suivante")
    if campaign is not None:
        for name in REFERENCE_NAMES:
            if (campaign.path / name).is_file():
                return campaign.path / name
    configured = SET["voix.voix_reference"].strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    return None


def reference_text(reference: Path | None) -> str:
    """Transcription de l'échantillon de voix (fichier texte du même nom, ex. voix.txt), vide si absente."""
    if reference is None:
        return ""
    try:
        return reference.with_suffix(".txt").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def clean_for_speech(text: str) -> str:
    """Retire la mise en forme Markdown et les marqueurs de secret, pour une lecture à voix haute."""
    text = re.sub(r"\[(?:secret:[^\]]*|mj|groupe|à confirmer)\]", "", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)                  # liens Markdown
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)             # titres
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)                  # puces
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def truncate_for_speech(text: str, limit: int) -> tuple:
    """-> (texte, coupé). Coupe à la dernière fin de phrase avant `limit` caractères (à défaut au dernier mot)."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)|\n", cut)]
    if ends and ends[-1] >= limit // 3:
        return cut[:ends[-1]].strip(), True
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).strip(), True


def settings_snapshot(reference: Path | None) -> dict:
    """Réglages qui influencent l'audio produit (sert à la clé du cache et à expliquer un audio préparé autrement)."""
    snap = {"moteur": engine(), "langue": SET["voix.langue"], "vitesse": SET["voix.vitesse"], "pause_s": SET["voix.pause_s"],
            "voix": "défaut"}
    if engine() == "omnivoice":
        snap["etapes"] = SET["voix.etapes"]
    else:
        snap["temperature"] = SET["voix.temperature"]
    if reference is not None:
        st = reference.stat()
        snap["voix"] = f"{reference.name}:{st.st_size}:{st.st_mtime_ns}:{reference_text(reference)}"
    return snap


def cache_key(spoken: str, reference: Path | None) -> str:
    raw = json.dumps({"texte": spoken, **settings_snapshot(reference)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_lock = threading.Lock()        # une seule synthèse à la fois (un seul moteur vocal chargé)


def is_busy() -> bool:
    """Vrai pendant qu'une synthèse vocale tourne (moteur vocal chargé)."""
    return _lock.locked()


def _paths(campaign: Campaign, session_id: str) -> tuple:
    d = campaign.path / "seances" / session_id
    return d / "precedemment.wav", d / "precedemment.voix.json", d / "precedemment.voix.txt"


def audio_state(campaign: Campaign, session_id: str, text: str, voice: str | None = None) -> dict:
    """État de l'audio d'une séance : {'etat', 'wav', 'detail'}.
    - 'ok'       : prêt et à jour ;
    - 'reglages' : le texte est le bon mais l'audio a été préparé avec d'autres réglages de voix (il reste lisible) ;
    - 'texte'    : le « précédemment » a changé depuis la préparation (l'audio dirait autre chose : à refaire) ;
    - 'absent'   : pas de fichier audio pour cette séance."""
    spoken = clean_for_speech(text)
    wav, meta, src = _paths(campaign, session_id)
    if not spoken or not wav.exists():
        return {"etat": "absent", "wav": None, "detail": ""}
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    reference = voice_reference(campaign, voice)
    if info.get("cle") == cache_key(spoken, reference):
        return {"etat": "ok", "wav": wav, "detail": ""}
    try:
        same_text = src.read_text(encoding="utf-8").strip() == spoken       # texte réellement lu lors de la préparation
    except OSError:
        same_text = False
    if not same_text:
        return {"etat": "texte", "wav": None, "detail": ""}
    before, now = info.get("reglages") or {}, settings_snapshot(reference)
    changed = [f"{k} : {before.get(k)} → {now.get(k)}" for k in now if k in before and before[k] != now[k]
               and k != "voix"] + (["voix de référence modifiée"] if before.get("voix", now["voix"]) != now["voix"] else [])
    return {"etat": "reglages", "wav": wav, "detail": ", ".join(changed) or "réglages de la voix modifiés depuis"}


def cached_audio(campaign: Campaign, session_id: str, text: str, voice: str | None = None) -> Path | None:
    """WAV déjà préparé ET à jour pour ce texte et ces réglages, sinon None. Ne synthétise jamais."""
    state = audio_state(campaign, session_id, text, voice)
    return state["wav"] if state["etat"] == "ok" else None


def playable_audio(campaign: Campaign, session_id: str, text: str) -> tuple:
    """-> (wav ou None, état). L'audio d'un texte inchangé reste lisible même si les réglages de voix ont changé depuis."""
    state = audio_state(campaign, session_id, text)
    return state["wav"], state


def sessions_with_audio(campaign: Campaign) -> list:
    """Identifiants des séances qui ont un fichier audio du « précédemment »."""
    return [d.name for d in sorted(campaign.path.glob("seances/*")) if (d / "precedemment.wav").exists()
            and not d.name.startswith("_")]


def _run(cmd: list, timeout: int, on_line=None):
    """Lance le worker. `on_line(texte)` reçoit chaque ligne écrite par le worker au fil de l'eau (suivi de progression)."""
    if on_line is None:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, env=modeles.child_env())
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=modeles.child_env())
    expired = threading.Event()

    def kill():
        expired.set()
        proc.kill()
    timer = threading.Timer(timeout, kill)
    err_chunks: list = []
    reader = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    timer.start()
    reader.start()
    out_lines = []
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            out_lines.append(line)
            try:
                on_line(line)
            except Exception:
                pass
        proc.wait()
        reader.join(5)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    return subprocess.CompletedProcess(cmd, proc.returncode, "\n".join(out_lines).encode("utf-8"),
                                       b"".join(err_chunks))


class _Server:
    """Processus de synthèse permanent (OmniVoice) : le modèle se charge une seule fois, au démarrage du bot, puis chaque
    synthèse n'est plus qu'une requête (une ligne JSON sur son entrée, des lignes de progression sur sa sortie).
    Utilisé seulement quand Whisper reste chargé (`liberer_whisper = non`) : sinon le but est justement de libérer la mémoire."""

    def __init__(self):
        self._lock = threading.RLock()                 # une requête à la fois
        self.proc = None
        self.state = "arrêté"                          # arrêté | chargement | prêt | échec
        self._ready = threading.Event()
        self._done = threading.Event()
        self._rc = None
        self._lines: list = []
        self._on_line = None
        self._err: list = []
        self._t0 = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _command(self) -> list:
        return [python_exe(), str(ROOT / "tools" / ENGINES[engine()][0]), "--serve", "--device", SET["voix.device"] or "auto"]

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            cmd = self._command()
            self._ready.clear()
            self._done.clear()
            self.state, self._err, self._t0 = "chargement", [], time.monotonic()
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             env=modeles.child_env())
            except OSError as e:
                self.proc, self.state = None, "échec"
                self._ready.set()
                raise VoixError(f"Impossible de lancer Python pour la voix ({e}). Vérifie `python` dans [voix] ou lance "
                                f"{ENGINES[engine()][2]}.") from e
            threading.Thread(target=self._read_out, args=(self.proc,), daemon=True).start()
            threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()

    def _read_err(self, proc) -> None:
        for raw in proc.stderr:
            self._err = (self._err + [raw.decode("utf-8", errors="replace").rstrip()])[-40:]

    def _read_out(self, proc) -> None:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line == "PRET":
                self.state = "prêt"
                print(f"[bot] voix : modèle prêt en mémoire ({time.monotonic() - self._t0:.0f} s)")
                self._ready.set()
                continue
            m = re.match(r"TERMINE (-?\d+)$", line)
            if m:
                self._rc = int(m.group(1))
                self._done.set()
                continue
            self._lines.append(line)
            handler = self._on_line
            if handler:
                try:
                    handler(line)
                except Exception:
                    pass
        proc.wait()
        if proc is self.proc:                           # le processus s'est arrêté (plantage, mémoire...)
            if self.state != "prêt":
                self.state = "échec"
            else:
                self.state = "arrêté"
            self._rc = None
            self._ready.set()
            self._done.set()

    def stop(self) -> None:
        with self._lock:
            proc, self.proc, self.state = self.proc, None, "arrêté"
            if proc is not None and proc.poll() is None:
                proc.kill()

    def request(self, argv: list, timeout: int, on_line=None):
        """Envoie une synthèse au processus permanent (qu'il démarre s'il le faut). -> CompletedProcess."""
        with self._lock:
            self.start()
            proc = self.proc
            deadline = time.monotonic() + timeout
            if not self._ready.is_set() and on_line:
                on_line("Chargement du modèle (pas encore prêt)...")
            if not self._ready.wait(max(1.0, deadline - time.monotonic())):
                self.stop()
                raise subprocess.TimeoutExpired(argv, timeout)
            if self.state != "prêt" or not self.running():
                return self._failed(argv)
            self._done.clear()
            self._rc, self._lines, self._on_line = None, [], on_line
            try:
                proc.stdin.write((json.dumps({"argv": argv}) + "\n").encode("ascii"))
                proc.stdin.flush()
            except OSError:
                return self._failed(argv)
            try:
                if not self._done.wait(max(1.0, deadline - time.monotonic())):
                    self.stop()
                    raise subprocess.TimeoutExpired(argv, timeout)
            finally:
                self._on_line = None
            if self._rc is None:                        # le processus est mort pendant la requête
                return self._failed(argv)
            errors = "\n".join(line for line in self._lines if line.startswith("ERREUR"))
            return subprocess.CompletedProcess(argv, self._rc, "\n".join(self._lines).encode("utf-8"),
                                               (errors or "\n".join(self._err[-5:])).encode("utf-8"))

    def _failed(self, argv):
        tail = "\n".join(self._err[-8:]) or "le processus de la voix s'est arrêté"
        return subprocess.CompletedProcess(argv, 1, b"", tail.encode("utf-8"))


SERVER = _Server()
atexit.register(SERVER.stop)


def use_server() -> bool:
    """Processus permanent : seulement avec OmniVoice et quand Whisper reste chargé (sinon on libère la mémoire à chaque fois)."""
    return engine() == "omnivoice" and not SET["voix.liberer_whisper"]


def preload() -> bool:
    """Au démarrage du bot : lance le chargement du modèle vocal (en arrière-plan, dans son processus). -> vrai si lancé."""
    if not (SET["voix.activer"] and use_server()):
        return False
    try:
        print("[bot] voix : chargement du modèle vocal au démarrage...")
        SERVER.start()
    except VoixError as e:
        print(f"[bot] voix : {e}")
        return False
    return True


def _server_run(cmd: list, timeout: int, on_line=None):
    """Même contrat que _run, mais auprès du processus permanent : cmd = [python, script, *arguments]."""
    return SERVER.request(cmd[2:], timeout, on_line)


RE_CHUNK = re.compile(r"^(MORCEAU|FAIT) (\d+)/(\d+)")


def _progress_from(progress):
    """Transforme les lignes du worker en appels progress(étape, faits, total)."""
    if progress is None:
        return None

    def on_line(line: str):
        m = RE_CHUNK.match(line)
        if m:
            kind, i, n = m.group(1), int(m.group(2)), int(m.group(3))
            progress("Synthèse", i if kind == "FAIT" else i - 1, n)
            if kind == "FAIT":
                print(f"[bot] voix : {line}")                 # durée obtenue / durée attendue de chaque morceau
        elif line.startswith("Chargement"):
            progress("Chargement du modèle", 0, 0)
        elif line.startswith(("ALERTE", "NOTE")):
            print(f"[bot] voix : {line}")
    return on_line


def ensure_audio(campaign: Campaign, session_id: str, text: str, force: bool = False, progress=None,
                 voice: str | None = None) -> Path:
    """Prépare le WAV du « précédemment » de cette séance (ou le renvoie s'il est déjà à jour).
    `force=True` régénère même si l'audio est à jour (la synthèse varie d'un essai à l'autre) ; l'ancien audio est gardé
    en `precedemment.precedent.wav` et remis en place si la nouvelle synthèse échoue.
    Appel bloquant (à lancer dans un thread) ; jamais pendant un enregistrement : c'est à l'appelant de le vérifier."""
    spoken = clean_for_speech(text)
    if not spoken:
        raise VoixError("Texte vide : rien à lire.")
    if not force:
        ready = cached_audio(campaign, session_id, text, voice)
        if ready is not None:
            return ready
    if not _lock.acquire(blocking=False):
        raise VoixError("Une synthèse vocale est déjà en cours.")
    try:
        wav, meta, _src = _paths(campaign, session_id)
        old_wav, old_meta = wav.with_suffix(".precedent.wav"), meta.with_suffix(".precedent.json")
        kept = force and wav.exists()
        if kept:
            shutil.copy2(wav, old_wav)
            if meta.exists():
                shutil.copy2(meta, old_meta)
        try:
            return _synthesize(campaign, session_id, spoken, progress, voice)
        except Exception:
            if kept:                                    # on ne perd pas l'audio précédent
                shutil.copy2(old_wav, wav)
                if old_meta.exists():
                    shutil.copy2(old_meta, meta)
            raise
    finally:
        _lock.release()


TEMP_PREFIX = "voicebot_dire_"     # audios « à la volée » : fichiers temporaires, supprimés après lecture


def ensure_free_audio(campaign: Campaign | None, text: str, progress=None, voice: str | None = None) -> Path:
    """Audio d'un texte quelconque (/dire, /regle dicter, /campagne dicter avec une voix) dans un dossier temporaire.
    Rien n'est conservé : l'appelant joue le WAV puis appelle `discard_free(wav)`. Même règles que ensure_audio :
    bloquant, jamais pendant un enregistrement (à l'appelant de le vérifier). Voix : `voice`, sinon celle de la campagne."""
    spoken = clean_for_speech(text)
    if not spoken:
        raise VoixError("Texte vide : rien à lire.")
    if not _lock.acquire(blocking=False):
        raise VoixError("Une synthèse vocale est déjà en cours.")
    folder = None
    try:
        reference = voice_reference(campaign, voice)
        folder = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        return _synthesize_to(folder / "dire.wav", folder / "dire.voix.json", folder / "dire.voix.txt", reference, spoken, progress)
    except BaseException:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        _lock.release()


def discard_free(wav: Path | None) -> None:
    """Supprime un audio temporaire produit par ensure_free_audio (sans effet sur tout autre fichier)."""
    if wav is not None and wav.parent.name.startswith(TEMP_PREFIX):
        shutil.rmtree(wav.parent, ignore_errors=True)


def purge_temp() -> int:
    """Supprime les audios temporaires restés après un arrêt brutal du bot. Renvoie le nombre de dossiers retirés."""
    removed = 0
    for d in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def _speed_up(wav: Path, factor: float) -> None:
    """Accélère (ou ralentit) le WAV avec ffmpeg (atempo : la hauteur de la voix ne change pas).
    Si ffmpeg échoue, on garde le WAV d'origine : mieux vaut une voix un peu lente que pas de voix."""
    if abs(factor - 1.0) < 0.01:
        return
    tmp = wav.with_suffix(".rapide.wav")
    try:
        done = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-filter:a", f"atempo={factor}",
                               str(tmp)], capture_output=True, timeout=300)
        if done.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(wav)
        else:
            print(f"[bot] voix : accélération ignorée ({(done.stderr or b'').decode(errors='replace')[-200:]})")
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[bot] voix : accélération ignorée ({e!r})")
    finally:
        tmp.unlink(missing_ok=True)


def _synthesize(campaign: Campaign, session_id: str, spoken: str, progress=None, voice: str | None = None) -> Path:
    wav, meta, src = _paths(campaign, session_id)
    return _synthesize_to(wav, meta, src, voice_reference(campaign, voice), spoken, progress)


def _synthesize_to(wav: Path, meta: Path, src: Path, reference: Path | None, spoken: str, progress=None) -> Path:
    """Synthétise `spoken` dans `wav` (texte source écrit dans `src`, empreinte dans `meta`)."""
    key = cache_key(spoken, reference)
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    src.write_text(spoken + "\n", encoding="utf-8")
    script, _pyfile, installer = ENGINES[engine()]
    cmd = [python_exe(), str(ROOT / "tools" / script), "--in", str(src), "--out", str(wav), "--lang", SET["voix.langue"],
           "--device", SET["voix.device"] or "auto", "--pause", str(SET["voix.pause_s"])]
    if engine() == "omnivoice":
        cmd += ["--steps", str(SET["voix.etapes"])]            # vitesse : ffmpeg après coup (atempo), pas le modèle
        if reference is not None and reference_text(reference):
            cmd += ["--voice-text", reference_text(reference)]
    else:
        cmd += ["--temperature", str(SET["voix.temperature"])]
    if reference is not None:
        cmd += ["--voice", str(reference)]
    runner = _server_run if use_server() else _run
    if runner is _run and SERVER.running():
        SERVER.stop()                                   # Whisper n'est plus conservé : on rend la mémoire du modèle vocal
    try:
        done = runner(cmd, SET["voix.delai_s"], _progress_from(progress)) if progress else runner(cmd, SET["voix.delai_s"])
    except subprocess.TimeoutExpired:
        raise VoixError(f"La synthèse vocale a dépassé {SET['voix.delai_s']} s (`delai_s` dans [voix]) : "
                        "GPU absent ou occupé ?") from None
    except OSError as e:
        raise VoixError(f"Impossible de lancer Python pour la voix ({e}). Vérifie `python` dans [voix] ou lance install_voix.bat.") from e
    err = (done.stderr or b"").decode("utf-8", errors="replace")
    if done.returncode != 0 or not wav.exists():
        wav.unlink(missing_ok=True)
        if "No module named" in err and ("chatterbox" in err or "omnivoice" in err or "torch" in err):
            raise VoixError(f"{engine().capitalize()} n'est pas installé dans cet environnement : lance {installer}.")
        if "PerthImplicitWatermarker" in err:
            raise VoixError('Le filigrane « perth » de Chatterbox ne s\'importe pas (souvent : setuptools trop récent). Dans '
                            'l\'environnement de la voix : `pip install "setuptools<81"`, puis relance. Détail : '
                            + (err.strip().splitlines() or [""])[-1][:200])
        if "out of memory" in err.lower():
            raise VoixError("Mémoire GPU insuffisante pour la voix : mets `device = cpu` dans [voix] (plus lent).")
        tail = " | ".join(line.strip() for line in err.strip().splitlines()[-3:]) or f"code {done.returncode}"
        raise VoixError(f"La synthèse vocale a échoué : {tail[:300]}")
    if progress:
        progress("Finalisation", 1, 1)
    if SPEED_APPLIED not in (done.stdout or b"").decode("utf-8", errors="replace"):
        _speed_up(wav, float(SET["voix.vitesse"]))
    meta.write_text(json.dumps({"cle": key, "voix": reference.name if reference else "défaut",
                                "reglages": settings_snapshot(reference)}, ensure_ascii=False) + "\n", encoding="utf-8")
    return wav
