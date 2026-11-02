"""Voix du bot : synthèse vocale avec Chatterbox, lancée dans un processus séparé (environnement conda dédié).

Module indépendant de Discord. Le texte est nettoyé, converti en WAV par tools/tts_chatterbox.py (exécuté avec le Python
de l'environnement de la voix) et mis en cache dans la séance : le même texte n'est synthétisé qu'une fois.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .campagne import Campaign
from .config import SET

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "tools" / "tts_chatterbox.py"
PYTHON_FILE = ROOT / ".voix_python"
REFERENCE_NAMES = ("voix.wav", "voix.mp3", "voix.flac")


class VoixError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def python_exe() -> str:
    """Python de l'environnement de la voix : réglage `voix.python`, sinon .voix_python (écrit par install_voix.bat),
    sinon l'interpréteur courant."""
    configured = SET["voix.python"].strip()
    if configured:
        return configured
    try:
        saved = PYTHON_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        saved = ""
    return saved or sys.executable


def voice_reference(campaign: Campaign | None) -> Path | None:
    """Échantillon de voix à imiter : `voix.wav` dans le dossier de la campagne, sinon `voix.voix_reference`."""
    if campaign is not None:
        for name in REFERENCE_NAMES:
            if (campaign.path / name).is_file():
                return campaign.path / name
    configured = SET["voix.voix_reference"].strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    return None


def clean_for_speech(text: str) -> str:
    """Retire la mise en forme Markdown et les marqueurs de secret, pour une lecture à voix haute."""
    text = re.sub(r"\[(?:secret:[^\]]*|mj|groupe|à confirmer)\]", "", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)                  # liens Markdown
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)             # titres
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)                  # puces
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def cache_key(spoken: str, reference: Path | None) -> str:
    ref = ""
    if reference is not None:
        st = reference.stat()
        ref = f"{reference.name}:{st.st_size}:{st.st_mtime_ns}"
    raw = "|".join([spoken, SET["voix.langue"], ref, "chatterbox-mtl", f"v{SET['voix.vitesse']}", f"p{SET['voix.pause_s']}", f"t{SET['voix.temperature']}"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_lock = threading.Lock()        # une seule synthèse à la fois (un seul modèle Chatterbox chargé)


def is_busy() -> bool:
    """Vrai pendant qu'une synthèse vocale tourne (Chatterbox chargé)."""
    return _lock.locked()


def _paths(campaign: Campaign, session_id: str) -> tuple:
    d = campaign.path / "seances" / session_id
    return d / "precedemment.wav", d / "precedemment.voix.json", d / "precedemment.voix.txt"


def cached_audio(campaign: Campaign, session_id: str, text: str) -> Path | None:
    """WAV déjà préparé ET à jour pour ce texte et cette voix, sinon None. Ne synthétise jamais."""
    spoken = clean_for_speech(text)
    wav, meta, _src = _paths(campaign, session_id)
    if not spoken or not wav.exists():
        return None
    try:
        return wav if json.loads(meta.read_text(encoding="utf-8")).get("cle") == cache_key(spoken, voice_reference(campaign)) else None
    except (OSError, ValueError):
        return None


def _run(cmd: list, timeout: int):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def ensure_audio(campaign: Campaign, session_id: str, text: str, force: bool = False) -> Path:
    """Prépare le WAV du « précédemment » de cette séance (ou le renvoie s'il est déjà à jour).
    `force=True` régénère même si l'audio est à jour (la synthèse varie d'un essai à l'autre) ; l'ancien audio est gardé
    en `precedemment.precedent.wav` et remis en place si la nouvelle synthèse échoue.
    Appel bloquant (à lancer dans un thread) ; jamais pendant un enregistrement : c'est à l'appelant de le vérifier."""
    spoken = clean_for_speech(text)
    if not spoken:
        raise VoixError("Texte vide : rien à lire.")
    if not force:
        ready = cached_audio(campaign, session_id, text)
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
            return _synthesize(campaign, session_id, spoken)
        except Exception:
            if kept:                                    # on ne perd pas l'audio précédent
                shutil.copy2(old_wav, wav)
                if old_meta.exists():
                    shutil.copy2(old_meta, meta)
            raise
    finally:
        _lock.release()


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


def _synthesize(campaign: Campaign, session_id: str, spoken: str) -> Path:
    reference = voice_reference(campaign)
    key = cache_key(spoken, reference)
    wav, meta, src = _paths(campaign, session_id)
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    src.write_text(spoken + "\n", encoding="utf-8")
    cmd = [python_exe(), str(WORKER), "--in", str(src), "--out", str(wav), "--lang", SET["voix.langue"],
           "--device", SET["voix.device"] or "auto", "--pause", str(SET["voix.pause_s"]),
           "--temperature", str(SET["voix.temperature"])]
    if reference is not None:
        cmd += ["--voice", str(reference)]
    try:
        done = _run(cmd, SET["voix.delai_s"])
    except subprocess.TimeoutExpired:
        raise VoixError(f"La synthèse vocale a dépassé {SET['voix.delai_s']} s (`delai_s` dans [voix]) : "
                        "GPU absent ou occupé ?") from None
    except OSError as e:
        raise VoixError(f"Impossible de lancer Python pour la voix ({e}). Vérifie `python` dans [voix] ou lance install_voix.bat.") from e
    err = (done.stderr or b"").decode("utf-8", errors="replace")
    if done.returncode != 0 or not wav.exists():
        wav.unlink(missing_ok=True)
        if "No module named" in err and ("chatterbox" in err or "torch" in err):
            raise VoixError("Chatterbox n'est pas installé dans cet environnement : lance install_voix.bat.")
        if "PerthImplicitWatermarker" in err:
            raise VoixError('Le filigrane « perth » de Chatterbox ne s\'importe pas (souvent : setuptools trop récent). Dans '
                            'l\'environnement de la voix : `pip install "setuptools<81"`, puis relance. Détail : '
                            + (err.strip().splitlines() or [""])[-1][:200])
        if "out of memory" in err.lower():
            raise VoixError("Mémoire GPU insuffisante pour la voix : mets `device = cpu` dans [voix] (plus lent).")
        tail = " | ".join(line.strip() for line in err.strip().splitlines()[-3:]) or f"code {done.returncode}"
        raise VoixError(f"La synthèse vocale a échoué : {tail[:300]}")
    _speed_up(wav, float(SET["voix.vitesse"]))
    meta.write_text(json.dumps({"cle": key, "voix": reference.name if reference else "défaut"}, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return wav
