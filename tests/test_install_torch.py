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
