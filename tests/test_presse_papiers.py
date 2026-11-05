"""Presse-papiers de la machine du bot : choix de l'outil selon le système, refus propres."""
import subprocess
import sys
import types

import pytest

from voicebot import presse_papiers as pp
from voicebot.config import SET


class Runner:
    def __init__(self, stdout=b"", code=0, stderr=b""):
        self.calls, self.stdout, self.code, self.stderr = [], stdout, code, stderr

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=self.code, stdout=self.stdout, stderr=self.stderr)


def test_desactive_par_reglage(cfg_file, monkeypatch):
    monkeypatch.setitem(SET.values, "campagnes.presse_papiers", False)
    assert "presse_papiers = non" in pp.reason_unavailable() and not pp.available()
    with pytest.raises(pp.ClipboardError):
        pp.copy("x")
    with pytest.raises(pp.ClipboardError):
        pp.paste()


def test_linux_sans_session_graphique_refuse(cfg_file, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    assert "aucun presse-papiers" in pp.reason_unavailable()


def test_linux_xclip_ecrit_et_lit_en_utf8(cfg_file, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(pp.shutil, "which", lambda name: "/usr/bin/" + name if name == "xclip" else None)
    run = Runner(stdout="Réponse accentuée".encode())
    monkeypatch.setattr(pp.subprocess, "run", run)
    pp.copy("Texte é")
    assert run.calls[0][0][0] == "xclip" and run.calls[0][1]["input"] == "Texte é".encode()
    assert pp.paste() == "Réponse accentuée"
    monkeypatch.setattr(pp.subprocess, "run", Runner(code=1, stderr=b"boom"))
    with pytest.raises(pp.ClipboardError, match="boom"):
        pp.paste()


def test_windows_passe_par_powershell_et_un_fichier_temporaire(cfg_file, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(pp.shutil, "which", lambda name: "powershell.exe")
    seen = {}

    def fake_run(cmd, **kw):
        script, path = cmd[-2], cmd[-1]
        if "Set-Clipboard" in script:
            seen["copied"] = open(path, encoding="utf-8").read()
        else:
            open(path, "w", encoding="utf-8").write("réponse collée")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    pp.copy("Bonjour é\n" * 3)
    assert seen["copied"] == "Bonjour é\n" * 3
    assert pp.paste() == "réponse collée"


def test_erreurs_systeme_deviennent_des_erreurs_lisibles(cfg_file, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pp.shutil, "which", lambda name: "/usr/bin/" + name)
    def boom(*a, **k): raise subprocess.TimeoutExpired("pbcopy", 1)
    monkeypatch.setattr(pp.subprocess, "run", boom)
    with pytest.raises(pp.ClipboardError, match="impossible"):
        pp.copy("x")
