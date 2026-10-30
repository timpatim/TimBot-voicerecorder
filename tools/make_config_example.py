"""Régénère config.example.ini à partir des paramètres déclarés dans voicebot/config.py.

Usage : python tools/make_config_example.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BOT_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.ini")   # n'écrit pas dans le vrai config.ini
sys.argv = sys.argv[:1]

from voicebot.config import default_config_text  # noqa: E402

(ROOT / "config.example.ini").write_text(default_config_text(), encoding="utf-8", newline="\n")
print("config.example.ini régénéré.")
