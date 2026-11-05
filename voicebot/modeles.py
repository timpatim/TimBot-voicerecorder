"""Dossier des modèles téléchargés (Whisper, alignement, OmniVoice, Chatterbox...).

Par défaut, Hugging Face et PyTorch rangent leurs modèles dans le profil de l'utilisateur (~/.cache). Ici ils vont dans
un dossier du programme (réglage `modeles.dossier`, relatif au dossier du programme), pour tout garder au même endroit
et pouvoir le déplacer ou le supprimer facilement. Vide = comportement habituel.
"""
import os
from pathlib import Path

from .config import SET

ROOT = Path(__file__).resolve().parent.parent


def models_dir() -> Path | None:
    configured = SET["modeles.dossier"].strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    return path if path.is_absolute() else ROOT / path


def env_vars() -> dict:
    """Variables d'environnement qui redirigent les téléchargements. Vide si le réglage est vide."""
    base = models_dir()
    if base is None:
        return {}
    return {"HF_HOME": str(base / "huggingface"),
            "HF_HUB_CACHE": str(base / "huggingface" / "hub"),
            "TORCH_HOME": str(base / "torch")}


def apply() -> dict:
    """À appeler AVANT tout import de huggingface_hub, transformers ou torch (ils lisent ces variables à l'import)."""
    env = env_vars()
    for key, value in env.items():
        os.environ[key] = value
    for value in env.values():
        Path(value).mkdir(parents=True, exist_ok=True)
    return env


def child_env() -> dict:
    """Environnement d'un processus enfant (worker de la voix) : celui du bot, avec les dossiers de modèles."""
    return {**os.environ, **env_vars(), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
