import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import install_torch  # noqa: E402

SMI = """
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 560.94                 Driver Version: 560.94         CUDA Version: {cuda}     |
+-----------------------------------------+------------------------+----------------------+
"""


@pytest.mark.parametrize("cuda,expected", [
    ("13.0", "cu128"), ("12.9", "cu128"), ("12.8", "cu128"), ("12.7", "cu126"), ("12.6", "cu126"),
    ("12.4", "cpu"), ("11.8", "cpu")])
def test_choix_du_build_selon_le_pilote(cuda, expected):
    assert install_torch.choose_variant(install_torch.parse_cuda_version(SMI.format(cuda=cuda))) == expected


def test_pas_de_gpu_ou_sortie_illisible():
    assert install_torch.parse_cuda_version("") is None and install_torch.parse_cuda_version("pas de gpu") is None
    assert install_torch.choose_variant(None) == "cpu"


def test_commande_pip(capsys):
    assert install_torch.main(["--device", "cu128", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "https://download.pytorch.org/whl/cu128" in out and "torch==2.8.*" in out and "torchaudio==2.8.*" in out


def test_menu_propose_quand_la_detection_echoue(monkeypatch, capsys):
    monkeypatch.setattr(install_torch, "detect_cuda", lambda: (None, False, ""))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["9", "3"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert install_torch.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Choix invalide" in out and "download.pytorch.org/whl/cu128" in out


def test_menu_entree_garde_la_valeur_proposee(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert install_torch.ask_variant("cu126") == "cu126"


def test_pas_de_menu_si_detection_ok_ou_no_prompt(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("ne doit pas poser de question")
    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(install_torch, "detect_cuda", lambda: ((12, 9), True, ""))
    install_torch.main(["--dry-run"])
    monkeypatch.setattr(install_torch, "detect_cuda", lambda: (None, False, ""))
    install_torch.main(["--dry-run", "--no-prompt"])
    assert "whl/cpu" in capsys.readouterr().out


@pytest.mark.parametrize("text", ["CUDA Version : 13.0", "CUDA Version: 13.0", "| CUDA Version:    13.0 |"])
def test_formats_de_la_ligne_cuda(text):
    assert install_torch.parse_cuda_version(text) == (13, 0)


def test_repli_sur_nvidia_smi_q(monkeypatch):
    monkeypatch.setattr(install_torch, "_smi_candidates", lambda: iter(["nvidia-smi"]))
    outputs = {(): "NVIDIA-SMI a répondu sans la ligne utile", ("-q",): "Driver Version : 581.15\nCUDA Version                              : 13.0"}
    monkeypatch.setattr(install_torch, "_run_smi", lambda exe, *a: outputs[a])
    assert install_torch.detect_cuda()[:2] == ((13, 0), True)


def test_remplace_un_torch_d_un_autre_build(monkeypatch, capsys):
    monkeypatch.setattr(install_torch, "installed_torch_version", lambda: "2.8.0+cpu")
    install_torch.main(["--device", "cu128", "--dry-run"])
    assert "sera remplacé par le build cu128" in capsys.readouterr().out
    monkeypatch.setattr(install_torch, "installed_torch_version", lambda: "2.8.0+cu128")
    install_torch.main(["--device", "cu128", "--dry-run"])
    assert "sera remplacé" not in capsys.readouterr().out
