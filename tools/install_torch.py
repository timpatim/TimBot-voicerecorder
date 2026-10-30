"""Installe PyTorch avec le bon build (CPU ou CUDA) AVANT le reste des dépendances.

Pourquoi : sur Windows, `pip install whisperx` installe un torch CPU uniquement (c'est ce que fournit PyPI).
Pour utiliser un GPU NVIDIA il faut installer torch depuis l'index dédié de PyTorch. Ce script :

  1. interroge `nvidia-smi` pour connaître la version de CUDA que le pilote NVIDIA sait gérer ;
  2. choisit le build adapté (cu128, cu126, ou cpu s'il n'y a pas de GPU / pilote trop ancien) ;
  3. lance `pip install torch torchaudio` depuis l'index correspondant.

Usage : python tools/install_torch.py [--device auto|cpu|cu126|cu128] [--dry-run]
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

# Doit rester compatible avec la contrainte de torch dans whisperx (torch~=2.8.0 à la date d'écriture).
# Si WhisperX évolue, c'est la seule ligne à changer.
TORCH_VERSION = "2.8.*"

PYTORCH_INDEX = "https://download.pytorch.org/whl/"
PYPI_INDEX = "https://pypi.org/simple"
VARIANTS = ("cpu", "cu126", "cu128")


def parse_cuda_version(nvidia_smi_output: str):
    """Extrait (majeur, mineur) de la ligne « CUDA Version: 12.6 » de nvidia-smi, ou None."""
    m = re.search(r"CUDA Version\s*:\s*(\d+)\.(\d+)", nvidia_smi_output or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def choose_variant(cuda) -> str:
    """Build PyTorch à installer pour une version de CUDA maximale prise en charge par le pilote."""
    if cuda is None:
        return "cpu"
    major, minor = cuda
    if major >= 13 or (major == 12 and minor >= 8):
        return "cu128"          # les pilotes plus récents restent compatibles avec les builds 12.8
    if major == 12 and minor >= 6:
        return "cu126"
    return "cpu"                # pilote trop ancien pour torch 2.8 : mettre à jour le pilote NVIDIA


def _smi_candidates():
    """nvidia-smi n'est pas toujours dans le PATH : on essaie aussi ses emplacements habituels sous Windows."""
    found = shutil.which("nvidia-smi")
    if found:
        yield found
    root = os.environ.get("SystemRoot", r"C:\Windows")
    prog = os.environ.get("ProgramFiles", r"C:\Program Files")
    for path in (os.path.join(root, "System32", "nvidia-smi.exe"),
                 os.path.join(prog, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")):
        if os.path.isfile(path):
            yield path


def installed_torch_version():
    """Version de torch déjà installée dans cet environnement (ex. « 2.8.0+cpu »), ou None."""
    try:
        from importlib import metadata
        return metadata.version("torch")
    except Exception:
        return None


def _run_smi(exe, *args):
    """Sortie (stdout + stderr) de nvidia-smi, décodée sans jamais échouer ; None si l'exécution échoue."""
    try:
        out = subprocess.run([exe, *args], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout + b"\n" + out.stderr).decode("utf-8", errors="replace")


def detect_cuda():
    """Renvoie (version CUDA ou None, nvidia-smi trouvé ?, texte brut pour le diagnostic)."""
    has_smi, last = False, ""
    for exe in _smi_candidates():
        # Sortie standard, puis `-q` (format « CUDA Version : 13.0 ») si la première ne contient pas la ligne.
        for args in ((), ("-q",)):
            text = _run_smi(exe, *args)
            if text is None:
                break
            has_smi, last = True, text
            cuda = parse_cuda_version(text)
            if cuda:
                return cuda, True, text
    return None, has_smi, last


MENU = (
    ("cpu", "CPU uniquement", "aucun GPU NVIDIA, ou pour tester ; transcription nettement plus lente"),
    ("cu126", "GPU NVIDIA - CUDA 12.6", "pilote compatible CUDA 12.6 ou 12.7"),
    ("cu128", "GPU NVIDIA - CUDA 12.8", "pilote récent (CUDA 12.8 ou supérieur) : RTX 50xx, et le plus courant aujourd'hui"),
)


def ask_variant(default: str) -> str:
    """Menu interactif : renvoie le build choisi (Entrée = valeur proposée)."""
    print()
    print("Quel build de PyTorch installer ?")
    for i, (key, label, hint) in enumerate(MENU, 1):
        mark = "  <- proposé" if key == default else ""
        print(f"  {i}) {label} [{key}]{mark}")
        print(f"       {hint}")
    print("  (Pour connaître la version CUDA de votre pilote : tapez nvidia-smi dans un terminal, "
          "ligne « CUDA Version ».)")
    default_idx = [k for k, *_ in MENU].index(default) + 1
    while True:
        try:
            raw = input(f"Votre choix [1-{len(MENU)}, Entrée = {default_idx}] : ").strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(MENU):
            return MENU[int(raw) - 1][0]
        if raw in VARIANTS:
            return raw
        print("Choix invalide.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--device", default="auto", choices=("auto",) + VARIANTS,
                    help="forcer le build : cpu, cu126, cu128 (défaut : détection automatique)")
    ap.add_argument("--dry-run", action="store_true", help="affiche la commande sans l'exécuter")
    ap.add_argument("--no-prompt", action="store_true",
                    help="ne jamais poser de question : si la détection échoue, installer le build cpu")
    args = ap.parse_args(argv)

    if args.device == "auto":
        cuda, has_smi, raw = detect_cuda()
        variant = choose_variant(cuda)
        if cuda:
            print(f"GPU NVIDIA détecté, CUDA {cuda[0]}.{cuda[1]} pris en charge par le pilote -> build {variant}.")
        elif has_smi:
            print("nvidia-smi répond mais la version de CUDA est illisible -> build cpu. Sortie reçue :")
            print("\n".join("    " + line for line in (raw or "(vide)").strip().splitlines()[:15]))
        else:
            print("Aucun GPU NVIDIA détecté (nvidia-smi introuvable) -> build cpu.")
        if variant == "cpu" and cuda:
            print("ATTENTION : le pilote NVIDIA est trop ancien pour PyTorch 2.8 (CUDA 12.6 minimum). "
                  "Mettez-le à jour pour utiliser le GPU.")
        if variant == "cpu" and not args.no_prompt and sys.stdin.isatty():
            print("Détection du GPU non concluante : choisissez vous-même le build.")
            variant = ask_variant(variant)
    else:
        variant = args.device
        print(f"Build forcé : {variant}.")

    cmd = [sys.executable, "-m", "pip", "install", f"torch=={TORCH_VERSION}", f"torchaudio=={TORCH_VERSION}",
           "--index-url", PYTORCH_INDEX + variant, "--extra-index-url", PYPI_INDEX]
    # Un torch déjà installé dans un autre build (ex. 2.8.0+cpu) satisfait torch==2.8.* : pip ne le remplacerait pas.
    old = installed_torch_version()
    must_uninstall = bool(old) and not old.endswith("+" + variant)
    if must_uninstall:
        print(f"torch {old} est déjà installé : il sera remplacé par le build {variant}.")
    print("Commande :", " ".join(cmd))
    if args.dry_run:
        return 0
    if must_uninstall:
        subprocess.call([sys.executable, "-m", "pip", "uninstall", "-y", "torch", "torchaudio"])
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
