"""Presse-papiers de la machine qui fait tourner le bot (pack à coller dans un chat, réponse à réintégrer).

Le bot ne peut pas savoir si ton client Discord est sur la même machine : il lit et écrit le presse-papiers de SA machine.
Windows : PowerShell (Set-Clipboard / Get-Clipboard) ; macOS : pbcopy / pbpaste ; Linux : wl-copy / wl-paste ou xclip.
Les textes passent par un fichier temporaire pour éviter tout problème d'encodage (accents) et de taille.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import SET

TIMEOUT_S = 30


class ClipboardError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def _ps(script: str, *args: str):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script, *args],
                          capture_output=True, timeout=TIMEOUT_S)


def _linux_tools():
    """(commande d'écriture, commande de lecture) disponibles, ou None."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") and shutil.which("wl-paste"):
        return ["wl-copy"], ["wl-paste", "--no-newline"]
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]
    return None


def available() -> bool:
    """Vrai si le presse-papiers est autorisé (réglage `campagnes.presse_papiers`) et utilisable sur cette machine."""
    return reason_unavailable() is None


def reason_unavailable() -> str | None:
    """Pourquoi le presse-papiers n'est pas utilisable (message pour l'utilisateur), sinon None."""
    if not SET["campagnes.presse_papiers"]:
        return "le presse-papiers est désactivé (`presse_papiers = non` dans [campagnes] de config.ini)"
    if sys.platform == "win32":
        return None if shutil.which("powershell") else "PowerShell est introuvable sur la machine du bot"
    if sys.platform == "darwin":
        return None if shutil.which("pbcopy") and shutil.which("pbpaste") else "pbcopy/pbpaste introuvables"
    if _linux_tools() is None:
        return "aucun presse-papiers sur la machine du bot (session graphique et xclip ou wl-clipboard requis)"
    return None


def copy(text: str) -> None:
    """Copie `text` dans le presse-papiers de la machine du bot."""
    problem = reason_unavailable()
    if problem:
        raise ClipboardError(problem)
    try:
        if sys.platform == "win32":
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "copie.txt"
                path.write_text(text, encoding="utf-8")
                done = _ps("Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 -LiteralPath $args[0])", str(path))
        elif sys.platform == "darwin":
            done = subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=TIMEOUT_S,
                                  env={**os.environ, "LC_CTYPE": "UTF-8"})
        else:
            done = subprocess.run(_linux_tools()[0], input=text.encode("utf-8"), capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ClipboardError(f"écriture dans le presse-papiers impossible ({e!r})") from e
    if done.returncode != 0:
        raise ClipboardError("écriture dans le presse-papiers impossible : "
                             + (done.stderr or b"").decode("utf-8", errors="replace").strip()[:200])


def paste() -> str:
    """Texte du presse-papiers de la machine du bot (chaîne vide s'il ne contient pas de texte)."""
    problem = reason_unavailable()
    if problem:
        raise ClipboardError(problem)
    try:
        if sys.platform == "win32":
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "colle.txt"
                done = _ps("$t = Get-Clipboard -Raw; if ($null -eq $t) { $t = '' }; "
                           "[IO.File]::WriteAllText($args[0], $t, (New-Object Text.UTF8Encoding $false))", str(path))
                if done.returncode == 0:
                    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        elif sys.platform == "darwin":
            done = subprocess.run(["pbpaste"], capture_output=True, timeout=TIMEOUT_S, env={**os.environ, "LC_CTYPE": "UTF-8"})
            if done.returncode == 0:
                return done.stdout.decode("utf-8", errors="replace")
        else:
            done = subprocess.run(_linux_tools()[1], capture_output=True, timeout=TIMEOUT_S)
            if done.returncode == 0:
                return done.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ClipboardError(f"lecture du presse-papiers impossible ({e!r})") from e
    raise ClipboardError("lecture du presse-papiers impossible : "
                         + (done.stderr or b"").decode("utf-8", errors="replace").strip()[:200])
