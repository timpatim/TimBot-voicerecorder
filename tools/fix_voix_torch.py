"""Donne à l'environnement de la voix un PyTorch CUDA de la MÊME version que celle choisie par chatterbox-tts.

Sur Windows, `pip install chatterbox-tts` installe un torch CPU (c'est ce que fournit PyPI). Ce script lit la
version de torch installée (ex. 2.6.0+cpu) puis réinstalle cette même version depuis l'index CUDA de PyTorch,
sans toucher aux autres dépendances. Il réutilise la détection du GPU de install_torch.py.

Usage : python tools/fix_voix_torch.py [--device auto|cpu|cu126|cu128] [--dry-run]
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import install_torch as it  # noqa: E402


def base_version(version: str) -> str:
    return version.split("+")[0]


def target_variant(version: str, wanted: str) -> str:
    """Les builds cu128 n'existent qu'à partir de torch 2.7 : pour une version plus ancienne on prend cu126."""
    if wanted == "cu128":
        major, minor = (int(x) for x in base_version(version).split(".")[:2])
        if (major, minor) < (2, 7):
            return "cu126"
    return wanted


def build_command(version: str, variant: str) -> list:
    v = base_version(version)
    return [sys.executable, "-m", "pip", "install", f"torch=={v}", f"torchaudio=={v}", "--index-url",
            it.PYTORCH_INDEX + variant, "--force-reinstall", "--no-deps"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--device", default="auto", choices=("auto",) + it.VARIANTS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    version = it.installed_torch_version()
    if not version:
        print("torch n'est pas installé : installez d'abord chatterbox-tts.")
        return 1
    if args.device == "auto":
        cuda, _has_smi, _raw = it.detect_cuda()
        variant = it.choose_variant(cuda)
    else:
        variant = args.device
    if variant == "cpu":
        print("Pas de GPU NVIDIA exploitable : torch CPU conservé (la voix sera lente, voix.device = cpu).")
        return 0
    variant = target_variant(version, variant)
    if version.endswith("+" + variant):
        print(f"torch {version} est déjà le bon build.")
        return 0
    cmd = build_command(version, variant)
    print("Commande :", " ".join(cmd))
    return 0 if args.dry_run else subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
