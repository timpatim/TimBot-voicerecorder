"""Diagnostic de l'environnement : vérifie ce dont le bot a besoin et dit quoi corriger.

Usage : python tools/check_env.py      (code de sortie 1 s'il manque quelque chose d'indispensable)
"""
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, KO = "[ OK ]", "[ !! ]", "[ KO ]"
failures = 0


def report(status: str, text: str, hint: str = ""):
    global failures
    if status == KO:
        failures += 1
    print(f"{status} {text}")
    if hint and status != OK:
        print(f"       -> {hint}")


def try_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as e:  # noqa: BLE001 - on veut afficher n'importe quelle erreur d'import
        return e


def main() -> int:
    v = sys.version_info
    ok_py = (3, 10) <= (v.major, v.minor) <= (3, 13)
    report(OK if ok_py else KO, f"Python {v.major}.{v.minor}.{v.micro}",
           "WhisperX exige Python 3.10 à 3.13 (3.12 recommandé)")

    # --- indispensables
    for mod, hint in (("discord", "pip install -r requirements.txt"), ("numpy", "pip install -r requirements.txt")):
        r = try_import(mod)
        report(OK if not isinstance(r, Exception) else KO, f"module {mod}", hint)
    vr = try_import("discord.ext.voice_recv")
    if isinstance(vr, Exception):
        report(KO, "discord.ext.voice_recv", "pip install -r requirements.txt (le fork compatible DAVE, nécessite git)")
    else:
        report(OK, "discord.ext.voice_recv (réception audio) : vérifie que c'est bien le fork DAVE de requirements.txt")

    # --- transcription : torch / GPU / whisperx
    torch = try_import("torch")
    gpu_ok = False
    if isinstance(torch, Exception):
        report(KO, "torch", "python tools/install_torch.py")
    else:
        gpu_ok = bool(torch.cuda.is_available())
        has_nvidia = shutil.which("nvidia-smi") is not None
        if gpu_ok:
            report(OK, f"torch {torch.__version__} avec GPU : {torch.cuda.get_device_name(0)}")
        elif has_nvidia:
            report(WARN, f"torch {torch.__version__} sans accès au GPU alors qu'une carte NVIDIA est présente",
                   "relance : python tools/install_torch.py   (puis pip install -r requirements.txt)")
        else:
            report(OK, f"torch {torch.__version__} (CPU) : la transcription sera plus lente, modèle conseillé : base ou small")
    wx = try_import("whisperx")
    report(OK if not isinstance(wx, Exception) else KO, "whisperx", "pip install -r requirements.txt")
    if gpu_ok:
        ct = try_import("ctranslate2")
        if not isinstance(ct, Exception):
            n = ct.get_cuda_device_count()
            report(OK if n else WARN, f"ctranslate2 voit {n} GPU",
                   "bibliothèques CUDA/cuDNN introuvables : installe le CUDA Toolkit 12.8 (voir README)")

    # --- étapes de fin de session
    ff_cfg = "ffmpeg"
    try:
        from voicebot.config import SET
        ff_cfg = SET["mp3.ffmpeg"]
        drive_on = SET["fin_de_session.drive"]
        creds = Path(SET["drive.identifiants"])
        report(OK, f"configuration : {SET.path}")
    except Exception as e:  # noqa: BLE001
        report(KO, f"configuration illisible : {e!r}")
        drive_on, creds = False, Path("credentials.json")
    report(OK if shutil.which(ff_cfg) else WARN, "ffmpeg (conversion MP3)",
           "conda install -c conda-forge ffmpeg  (ou désactive l’étape : mp3 = non)")
    report(OK if not isinstance(try_import("webrtcvad"), Exception) else WARN, "webrtcvad (détection de voix)",
           "pip install webrtcvad-wheels : sans lui, un VAD de repli moins précis est utilisé")
    report(OK if not isinstance(try_import("reportlab"), Exception) else WARN, "reportlab (rapport PDF)",
           "pip install reportlab, ou désactive l'étape : rapport = non")
    if drive_on:
        g = try_import("googleapiclient.discovery")
        report(OK if not isinstance(g, Exception) else WARN, "bibliothèques Google Drive", "pip install -r requirements.txt")
        report(OK if creds.exists() or Path(SET["drive.jeton"]).exists() else WARN, f"{creds} (Google Drive)",
               "voir README, section Google Drive ; ou désactive l'étape : drive = non")

    # --- jeton Discord
    try:
        has_token = bool(SET["discord.token"])
    except Exception:  # noqa: BLE001
        has_token = bool(os.environ.get("DISCORD_TOKEN"))
    report(OK if has_token else WARN, "jeton Discord (DISCORD_TOKEN ou [discord] token dans config.ini)",
           'setx DISCORD_TOKEN "ton_jeton"  puis ouvre un nouveau terminal')

    # --- git (pour réinstaller le fork)
    report(OK if shutil.which("git") else WARN, "git (utile pour réinstaller le fork de voice_recv)",
           "conda install -c conda-forge git")

    print()
    print("Environnement prêt." if not failures else f"{failures} problème(s) bloquant(s) à corriger ci-dessus.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
