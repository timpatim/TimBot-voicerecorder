import json
import re
from pathlib import Path

import pytest

from voicebot.config import (CONFIG_KEYS, SETTINGS, Settings, default_config_text, ini_set, load_config,
                             parse_config_value, parse_setting, save_config, SET)

ROOT = Path(__file__).resolve().parents[1]


def test_config_example_est_a_jour():
    """config.example.ini doit refléter les paramètres déclarés (python tools/make_config_example.py)."""
    example = (ROOT / "config.example.ini").read_text(encoding="utf-8")
    assert example.replace("\r\n", "\n").strip() == default_config_text().strip()


def test_creation_du_fichier_avec_les_valeurs_par_defaut(tmp_path):
    path = tmp_path / "config.ini"
    s = Settings(path)
    assert path.exists()
    for key, typ, default, _env, _desc in SETTINGS:
        assert s[key] == default, key


def test_migration_de_l_ancienne_config_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_config.json").write_text(json.dumps({"mp3": False, "mp3_kbps": 96}), encoding="utf-8")
    s = Settings(tmp_path / "config.ini")
    assert s["fin_de_session.mp3"] is False and s["fin_de_session.mp3_kbps"] == 96


def test_variable_d_environnement_prioritaire(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "medium")
    assert Settings(tmp_path / "c.ini")["whisper.modele"] == "medium"


def test_valeur_invalide_retombe_sur_le_defaut(tmp_path, capsys):
    path = tmp_path / "c.ini"
    Settings(path)
    path.write_text(path.read_text(encoding="utf-8").replace("agressivite = 2", "agressivite = 7"), encoding="utf-8")
    s = Settings(path)
    assert s["vad.agressivite"] == 2
    assert "invalide" in capsys.readouterr().out


def test_parametre_manquant_est_ajoute_dans_la_bonne_section(tmp_path):
    path = tmp_path / "c.ini"
    Settings(path)
    path.write_text(path.read_text(encoding="utf-8").replace("nb_mots_top = 5\n", ""), encoding="utf-8")
    s = Settings(path)
    assert s["stats.nb_mots_top"] == 5
    assert re.search(r"\[stats\](?:.|\n)*?nb_mots_top = 5", path.read_text(encoding="utf-8"))


def test_fichier_illisible_utilise_les_defauts(tmp_path):
    path = tmp_path / "bad.ini"
    path.write_text("ceci n'est pas un ini\n[a\n", encoding="utf-8")
    assert Settings(path)["whisper.modele"] == "small"


def test_ini_set_preserve_les_commentaires(tmp_path):
    path = tmp_path / "c.ini"
    path.write_text("# en-tête\n[a]\n# note\nx = 1\n\n[b]\ny = 2\n", encoding="utf-8")
    ini_set(path, "a", "x", "10")
    ini_set(path, "a", "z", "3", "nouveau")
    ini_set(path, "c", "w", "4")
    text = path.read_text(encoding="utf-8")
    assert "# en-tête" in text and "# note" in text and "x = 10" in text
    assert re.search(r"\[a\](?:.|\n)*# nouveau\nz = 3(?:.|\n)*\[b\]", text)
    assert "[c]\nw = 4" in text


def test_config_des_etapes_lue_et_ecrite_dans_config_ini(cfg_file):
    cfg = load_config()
    assert set(cfg) == set(CONFIG_KEYS) and cfg["mp3"] is True
    before = cfg_file.read_text(encoding="utf-8")
    cfg["mp3"] = False
    save_config(cfg)
    after = cfg_file.read_text(encoding="utf-8")
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == [("mp3 = oui", "mp3 = non")]
    assert load_config()["mp3"] is False


def test_modification_a_la_main_prise_en_compte_au_rechargement(cfg_file):
    cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace("partage = lien", "partage = aucun"), encoding="utf-8")
    assert load_config()["partage"] == "aucun"


@pytest.mark.parametrize("key,raw", [("mp3", "peut-être"), ("mp3_kbps", "9999"), ("mp3_kbps", "abc"), ("partage", "tous")])
def test_valeurs_refusees(key, raw):
    with pytest.raises(ValueError):
        parse_config_value(key, raw)


@pytest.mark.parametrize("raw,expected", [("oui", True), ("Non", False), ("1", True), ("off", False)])
def test_booleens(raw, expected):
    assert parse_setting("fin_de_session.mp3", raw) is expected


def test_regex_invalide_refusee():
    with pytest.raises(ValueError):
        parse_setting("whisper.filtre_hallucinations", "(")


def test_set_global_contient_tous_les_parametres():
    assert all(key in SET.values for key, *_ in SETTINGS)


def test_la_page_wiki_configuration_est_a_jour(tmp_path):
    """wiki/Configuration.md est générée : `python tools/make_wiki.py` après toute modification des paramètres."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    target = root / "wiki" / "Configuration.md"
    before = target.read_text(encoding="utf-8")
    try:
        subprocess.run([sys.executable, str(root / "tools" / "make_wiki.py")], check=True, capture_output=True)
        assert target.read_text(encoding="utf-8") == before, "wiki/Configuration.md périmée : lancez python tools/make_wiki.py"
    finally:
        target.write_text(before, encoding="utf-8", newline="\n")


def test_la_page_wiki_commandes_cite_toutes_les_commandes_et_actions():
    from pathlib import Path
    from tests.test_commands import build
    from voicebot.commands import CAMPAGNE_ACTIONS
    page = (Path(__file__).resolve().parents[1] / "wiki" / "Commandes.md").read_text(encoding="utf-8")
    bot, _ = build()
    for name in bot.tree.commands:
        assert f"`/{name}`" in page, f"/{name} absente de wiki/Commandes.md"
    for action in CAMPAGNE_ACTIONS:
        assert f"| `{action}`" in page, f"action « {action} » absente de wiki/Commandes.md"


def test_l_arbre_des_commandes_du_wiki_cite_chaque_action():
    import re
    from pathlib import Path
    from voicebot.commands import CAMPAGNE_ACTIONS
    page = (Path(__file__).resolve().parents[1] / "wiki" / "Commandes.md").read_text(encoding="utf-8")
    for action in CAMPAGNE_ACTIONS:
        assert re.search(rf"^[├└]─ {re.escape(action)}\s", page, re.M), f"« {action} » absente de l'arbre"
        assert re.search(rf"^\| `{re.escape(action)}` \|", page, re.M), f"« {action} » absente du tableau croisé"
