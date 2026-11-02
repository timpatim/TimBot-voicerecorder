"""Régénère wiki/Configuration.md à partir des paramètres déclarés dans voicebot/config.py (valeurs par défaut exactes).

Usage : python tools/make_wiki.py
Les pages Home.md et Commandes.md sont écrites à la main ; un test vérifie que Commandes.md cite toutes les commandes.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BOT_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.ini")   # n'écrit pas dans le vrai config.ini
sys.argv = sys.argv[:1]

from voicebot.config import CHECKS, SECTION_TITLES, SETTINGS, format_setting  # noqa: E402

TYPES = {str: "texte", int: "entier", float: "nombre", bool: "oui / non"}

INTRO = """# Configuration (`config.ini`)

> Page générée par `python tools/make_wiki.py` à partir de `voicebot/config.py` : ne la modifiez pas à la main.

Au premier lancement, le bot crée `config.ini` avec toutes les valeurs par défaut et tous les commentaires
([`config.example.ini`](https://github.com/timpatim/TimBot-voicerecorder/blob/main/config.example.ini) en est une copie). Les clés manquantes sont ajoutées
automatiquement ; une valeur invalide retombe sur le défaut avec un avertissement dans la console.

- Les booléens s'écrivent `oui` / `non` (`true`, `1`, `on`… sont aussi acceptés).
- Le fichier est relu au démarrage. Seules les étapes de `[fin_de_session]` sont relues à chaque `/stop`, et peuvent
  se modifier depuis Discord avec [`/config`](Commandes#config).
- Mieux vaut ne pas écrire de secret dans le fichier : `token` et `cle_api` peuvent venir des variables d'environnement
  `DISCORD_TOKEN` et `GEMINI_API_KEY`. Ne commitez jamais `config.ini`.

"""


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def main() -> None:
    out = [INTRO, "## Sections\n"]
    out += [f"- [`[{s}]`](#{s}) : {t}" for s, t in SECTION_TITLES.items()]
    for section, title in SECTION_TITLES.items():
        out += ["", f"## {section}", "", f"*{title}*", "",
                "| Clé | Type | Défaut | Valeurs admises | Rôle |", "|---|---|---|---|---|"]
        for key, typ, default, env, desc in SETTINGS:
            if not key.startswith(section + "."):
                continue
            shown = format_setting(default)
            shown = f"`{shown}`" if shown != "" else "*(vide)*"
            check = CHECKS.get(key)
            allowed = cell(check[1].replace("attendu : ", "")) if check else ""
            role = cell(desc)
            if env:
                role += f" (variable d'environnement : `{env}`)"
            out.append(f"| `{key.split('.', 1)[1]}` | {TYPES[typ]} | {cell(shown)} | {allowed} | {role} |")
    (ROOT / "wiki" / "Configuration.md").write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print("wiki/Configuration.md régénéré.")


if __name__ == "__main__":
    main()
