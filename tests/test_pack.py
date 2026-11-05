"""Packs à coller dans un chat, contrôle de lecture et intégration de la réponse."""
import asyncio
import json
import types

import pytest

from voicebot import campagne, commands, memoire, pack
from voicebot.config import SET
from tests.test_campagne import FakeInter, MJ, P1, make_bot
from tests.test_memoire import Att, fake_rec, run_memoire_step, ROWS

SID = "2026-10-30_200000"


@pytest.fixture
def camp(tmp_path, monkeypatch, cfg_file):
    monkeypatch.setitem(SET.values, "campagnes.dossier", str(tmp_path / "camp"))
    monkeypatch.setitem(SET.values, "sessions.dossier", str(tmp_path / "s"))
    c = campagne.CampaignStore(tmp_path / "camp").create("Camp", "Pathfinder", MJ, [MJ, P1])
    c.contexte_path.write_text("# Contexte secret-du-jeu\n", encoding="utf-8")
    return c


def long_rows():
    return [{"user": "1" if i % 2 else "2", "name": "x", "start": i * 600, "end": i * 600 + 5,
             "text": f"Réplique numéro {i}."} for i in range(0, 14)]          # 0 → 2h10, une réplique toutes les 10 min


def test_pack_unique(camp):
    made = pack.build_packs(camp, ROWS[:3], SID)
    assert len(made["fichiers"]) == 1 and made["fichiers"][0].name == f"pack_{SID}.md"
    text = made["fichiers"][0].read_text(encoding="utf-8")
    for needle in ("Contexte secret-du-jeu", "<transcription>", "[00:00:00] MJ: Vous arrivez", "=== COMPTE RENDU ===",
                   "=== CHRONOLOGIE ===", "=== SECTIONS ===", "=== PRECEDEMMENT ===", "=== FIN LUE ===", "30/10/2026", "Dans l'épisode précédent de Camp"):
        assert needle in text, needle
    assert made["parties"][0]["fin_attendue"] == "00:00:04"            # répliques consécutives fusionnées : « merci J'ouvre la porte. »


def test_pack_en_parties_horodatages_et_livrables_seulement_a_la_fin(camp):
    made = pack.build_packs(camp, long_rows(), SID, part_minutes=60)
    assert [p["fin_attendue"] for p in made["parties"]] == ["00:50:00", "01:50:00", "02:10:00"]
    texts = [f.read_text(encoding="utf-8") for f in made["fichiers"]]
    assert "partie 1 sur 3" in texts[0] and "partie 3 sur 3" in texts[2]
    assert "Contexte secret-du-jeu" in texts[0] and "Contexte secret-du-jeu" not in texts[1] and "Contexte secret-du-jeu" in texts[2]
    assert "=== COMPTE RENDU ===" not in texts[0] and "=== COMPTE RENDU ===" not in texts[1] and "=== COMPTE RENDU ===" in texts[2]
    assert "MÊME conversation" in texts[1] and "[00:10:00]" in texts[0] and "[00:10:00]" not in texts[1]   # horodatages d'origine
    assert json.loads((camp.path / "seances" / SID / "pack" / "pack.json").read_text(encoding="utf-8"))["parties"][2]["fin_attendue_s"] == 7800


REPONSE = """=== FIN LUE === 00:00:06 | « J'ouvre la porte. »
=== NOTES ===
- [00:00:00] Arrivée ●
=== COMPTE RENDU ===
# Compte rendu du 30/10
Max ouvre la porte.
=== RESUME GLOBAL ===
# Changements depuis la version précédente
Max est en ville.
=== PRECEDEMMENT ===
Dans l'épisode précédent de Camp… Max ouvre la porte."""


def test_parse_reponse_tolerant_et_incomplet():
    d = pack.parse_reponse(REPONSE)
    assert d["fin_lue_s"] == 6 and d["fin_lue_texte"] == "J'ouvre la porte." and d["notes_fin_s"] == 0 and d["compte_rendu"].endswith("la porte.") and d["precedemment"].startswith("Dans l'épisode")
    assert d["resume_global"].startswith("# Changements") and "Arrivée" in d["notes"]
    d2 = pack.parse_reponse(REPONSE.replace("=== COMPTE RENDU ===", "**=== COMPTE RENDU ===**").replace("\n", "\r\n"))
    assert d2["compte_rendu"].startswith("# Compte rendu")
    with pytest.raises(campagne.CampaignError, match="PRECEDEMMENT"):
        pack.parse_reponse(REPONSE.split("=== PRECEDEMMENT")[0])


def test_integration_complete_et_rollback(camp):
    campagne.add_version(camp, "# journal", "journal")
    pack.build_packs(camp, ROWS[:3], SID)
    assert pack.pending_session(camp) == SID
    out = pack.integrate(camp, REPONSE)
    assert out["avertissement"] is None and out["version"] == "v002_2026-10-30-200000.md"
    assert camp.resume_global().read_text(encoding="utf-8").startswith("# Changements")
    assert memoire.latest_precedemment(camp).startswith("Dans l'épisode précédent")
    assert pack.pending_session(camp) is None
    with pytest.raises(campagne.CampaignError, match="déjà intégrée|en attente"):
        pack.integrate(camp, REPONSE, SID)
    assert campagne.rollback(camp)[2] == SID and memoire.latest_precedemment(camp) is None


def test_avertissement_lecture_tronquee_ou_controle_absent(camp):
    pack.build_packs(camp, long_rows(), SID, part_minutes=200)                 # une seule partie, fin 02:10:00
    out = pack.integrate(camp, REPONSE.replace("00:00:06", "01:20:00"))
    assert "tronquée" in out["avertissement"] and "02:10:00" in out["avertissement"]
    campagne.rollback(camp) if len(campagne.versions(camp)) > 1 else None
    (camp.path / "seances" / SID / "meta.json").unlink()
    out = pack.integrate(camp, REPONSE.split("\n", 1)[1], SID)
    assert "FIN LUE" in out["avertissement"]


def test_la_fin_de_session_ne_prepare_plus_de_pack(camp, tmp_path, monkeypatch):
    monkeypatch.setitem(SET.values, "regles.cle_api", "")
    rec = fake_rec(tmp_path, True)
    out, res, state = run_memoire_step(rec)
    assert out is None and state == ("skipped", "à la demande : /campagne pack")
    assert not (rec.dir / "pack").exists() and "pack" not in res


def test_commandes_pack_et_integrer(camp):
    bot, inter = make_bot(), FakeInter()
    inter.channel_id = 1
    cmd = bot.tree.commands["campagne"]
    asyncio.run(cmd(inter, types.SimpleNamespace(value="pack"), session=SID, sortie=types.SimpleNamespace(value="fichier"),
                    fichier=Att("[00:00:01.000 → 00:00:04.000] couette : Vous entrez.\n[00:00:05.000 → 00:00:07.000] silentgob : J'entre.")))
    assert (camp.path / "seances" / SID / "pack" / "pack.json").exists()
    inter2 = FakeInter()
    asyncio.run(cmd(inter2, types.SimpleNamespace(value="integrer"), fichier=Att(REPONSE.replace("00:00:06", "00:00:05"))))
    assert "intégrée" in inter2.out[0] and camp.nb_seances() == 1
    inter3 = FakeInter()
    asyncio.run(cmd(inter3, types.SimpleNamespace(value="integrer"), fichier=Att(REPONSE)))
    assert "❌" in inter3.out[0]


def test_controle_citation_et_etendue_des_notes(camp):
    pack.build_packs(camp, long_rows(), SID, part_minutes=200)           # dernière réplique : « Réplique numéro 13. » à 02:10:00
    base = "=== FIN LUE === 02:10:00 | « {} »\n=== NOTES ===\n- [02:09:00] fin\n" + REPONSE.split("=== NOTES ===\n", 1)[1].split("\n", 1)[1]
    good = pack.parse_reponse(base.format("Réplique numéro 13."))
    expected = json.loads((camp.path / "seances" / SID / "pack" / "pack.json").read_text(encoding="utf-8"))["parties"][-1]
    assert pack.controle_lecture(good, expected) == []
    # heure juste mais citation inventée : un modèle qui n'a pas tout lu ne peut pas la deviner
    bad = pack.controle_lecture(pack.parse_reponse(base.format("Une phrase qui n'existe pas.")), expected)
    assert len(bad) == 1 and "ne figure pas" in bad[0]
    # pas de citation
    assert "recopiée" in pack.controle_lecture(pack.parse_reponse(base.format("")), expected)[0]
    # notes qui s'arrêtent très tôt malgré une fin annoncée correcte
    early = pack.parse_reponse(base.format("Réplique numéro 13.").replace("[02:09:00]", "[00:40:00]"))
    assert "Les notes s'arrêtent à 00:40:00" in pack.controle_lecture(early, expected)[0]


def test_session_obligatoire_avec_un_fichier_joint(camp, clipboard):
    bot, inter = make_bot(), FakeInter()
    cmd = bot.tree.commands["campagne"]
    asyncio.run(cmd(inter, types.SimpleNamespace(value="pack"), fichier=Att("[00:00:01.000 → 00:00:04.000] couette : Salut.")))
    assert "date de la séance" in inter.out[-1] and camp.nb_seances() == 0
    asyncio.run(cmd(inter, types.SimpleNamespace(value="pack"), session="2026-10-24",
                    fichier=Att("[00:00:01.000 → 00:00:04.000] couette : Salut.")))
    assert (camp.path / "seances" / "2026-10-24" / "pack" / "pack.json").exists()


def test_pas_de_decoupe_par_defaut_et_option(camp):
    assert SET["campagnes.pack_minutes"] == 0
    made = pack.build_packs(camp, long_rows(), SID)                         # séance de 2 h 10 : un seul pack
    assert len(made["fichiers"]) == 1 and made["parties"][0]["fin_attendue"] == "02:10:00"
    assert "partie" not in made["fichiers"][0].read_text(encoding="utf-8").split("<transcription>")[0].split("</etat_campagne>")[-1]
    assert len(pack.build_packs(camp, long_rows(), SID, 60)["fichiers"]) == 3     # option : paramètre explicite
    assert len(pack.build_packs(camp, long_rows(), SID, 0)["fichiers"]) == 1


def test_afficher_et_autocompletion_des_sessions(camp, tmp_path):
    for sid in ("2026-10-17_190000", "2026-10-24_190000", "2026-10-31_190000"):
        pack.build_packs(camp, ROWS[:3], sid)
    pack.integrate(camp, REPONSE, "2026-10-24_190000")
    (tmp_path / "s" / "2026-10-31_190000").mkdir(parents=True)
    (tmp_path / "s" / "2026-10-31_190000" / "transcript_live.jsonl").write_text("{}", encoding="utf-8")
    bot = make_bot()
    cmd = bot.tree.commands["campagne"]
    inter = FakeInter()
    choices = asyncio.run(cmd.autocompletes["session"](inter, ""))
    assert len(choices) == 3                                             # l'autocomplétion renvoie une proposition par séance
    labels = memoire.session_choices(camp, tmp_path / "s")
    assert labels == [("2026-10-31_190000", "2026-10-31_190000 — enregistrée, pack prêt"),
                      ("2026-10-24_190000", "2026-10-24_190000 — intégrée"),
                      ("2026-10-17_190000", "2026-10-17_190000 — pack prêt")]
    assert [i for i, _ in memoire.session_choices(camp, tmp_path / "s", "10-24")] == ["2026-10-24_190000"]
    # afficher : dernière séance intégrée par défaut, ou une séance précise
    asyncio.run(cmd(inter, types.SimpleNamespace(value="afficher"), quoi=types.SimpleNamespace(value="precedemment")))
    assert inter.out[-1] is None                                         # embed envoyé, sans texte brut
    inter2 = FakeInter()
    asyncio.run(cmd(inter2, types.SimpleNamespace(value="afficher"), quoi=types.SimpleNamespace(value="compte_rendu"),
                    session="2026-10-17_190000"))
    assert "Pas de compte_rendu.md" in inter2.out[-1]
    inter3 = FakeInter()
    asyncio.run(cmd(inter3, types.SimpleNamespace(value="afficher"), quoi=types.SimpleNamespace(value="resume")))
    assert "Résumé global" in inter3.out[-1]                             # le résumé existe après l'intégration
    (camp.path / "resume_global.md").unlink()
    inter4 = FakeInter()
    asyncio.run(cmd(inter4, types.SimpleNamespace(value="afficher"), quoi=types.SimpleNamespace(value="resume")))
    assert "Pas encore de résumé global" in inter4.out[-1]


SAMPLE = """Intro ignorée

## Alliés, adversaires et PNJ
- Laria

**Fils ouverts**
- Le chef est vivant

---------- EPISODE 1 ----------
- Manifestation

# Lieux :
- Kintargo
"""


def test_rubriques_du_resume_global(camp, tmp_path):
    assert [t for t, _ in campagne.split_sections(SAMPLE)] == ["Alliés, adversaires et PNJ", "Fils ouverts", "EPISODE 1", "Lieux"]
    assert campagne.find_section(SAMPLE, "fils")[1] == "- Le chef est vivant"
    assert campagne.find_section(SAMPLE, "allies")[0].startswith("Alliés")
    assert campagne.find_section(SAMPLE, "episode 1")[1] == "- Manifestation"
    with pytest.raises(campagne.CampaignError, match="introuvable"):
        campagne.find_section(SAMPLE, "zzz")
    campagne.add_version(camp, SAMPLE, "test")
    bot, cmd = make_bot(), None
    cmd = bot.tree.commands["campagne"]
    inter = FakeInter()
    asyncio.run(cmd(inter, types.SimpleNamespace(value="afficher"), rubrique="fils"))
    assert inter.out[-1] is None                                           # embed de la rubrique
    inter2 = FakeInter()
    asyncio.run(cmd(inter2, types.SimpleNamespace(value="afficher"), rubrique="zzz"))
    assert "introuvable" in inter2.out[-1] and "Fils ouverts" in inter2.out[-1]
    titles = asyncio.run(cmd.autocompletes["rubrique"](FakeInter(), "lieu"))
    assert len(titles) == 1


# --- presse-papiers --------------------------------------------------------------------------------------------

def run_pack(camp, session=SID, **kw):
    inter = FakeInter()
    rows = "\n".join(f"[{r['start'] // 3600:02d}:{r['start'] % 3600 // 60:02d}:{r['start'] % 60:02d}.000 → "
                     f"{r['end'] // 3600:02d}:{r['end'] % 3600 // 60:02d}:{r['end'] % 60:02d}.000] {r['user']} : {r['text']}"
                     for r in long_rows())
    asyncio.run(make_bot().tree.commands["campagne"](inter, types.SimpleNamespace(value="pack"), session=session,
                                                     fichier=Att(rows), **kw))
    return inter


def test_pack_copie_dans_le_presse_papiers_partie_par_partie(camp, clipboard):
    def said(inter):
        return " ".join(str(x) for x in inter.out)
    inter = run_pack(camp, minutes=60)
    text = said(inter)
    assert "partie 1/3" in text and "Partie suivante" in text and "/campagne pack partie:2" in text and "FIN LUE" in text
    assert "partie 1 sur 3" in clipboard.text and "Réplique numéro 0." in clipboard.text
    assert "Réplique numéro 13." not in clipboard.text
    inter = run_pack(camp, minutes=60, partie=3)
    assert "partie 3 sur 3" in clipboard.text and "Réplique numéro 13." in clipboard.text
    assert "integrer" in said(inter)
    assert "3 partie" in said(run_pack(camp, minutes=60, partie=4))


def test_pack_refuse_sans_presse_papiers_mais_reste_possible_en_fichier(camp, monkeypatch):
    from voicebot import presse_papiers
    monkeypatch.setattr(presse_papiers, "reason_unavailable", lambda: "aucun presse-papiers sur la machine du bot")
    inter = run_pack(camp)
    assert "sortie:fichier" in inter.out[-1] and not (camp.path / "seances" / SID / "pack").exists()
    inter = run_pack(camp, sortie=types.SimpleNamespace(value="fichier"))
    assert (camp.path / "seances" / SID / "pack" / "pack.json").exists()


def test_integrer_depuis_le_presse_papiers(camp, clipboard):
    pack.build_packs(camp, ROWS[:3], SID)
    cmd = make_bot().tree.commands["campagne"]
    inter = FakeInter()
    asyncio.run(cmd(inter, types.SimpleNamespace(value="integrer")))                      # presse-papiers vide
    assert "ne contient pas de texte" in inter.out[0] and not (camp.path / "seances" / SID / "meta.json").exists()
    clipboard.text = REPONSE.replace("00:00:06", "00:00:05")
    inter = FakeInter()
    asyncio.run(cmd(inter, types.SimpleNamespace(value="integrer")))
    assert "intégrée" in inter.out[0] and (camp.path / "seances" / SID / "meta.json").exists()
    clipboard.text = "du texte qui n'est pas une réponse"
    inter = FakeInter()
    asyncio.run(cmd(inter, types.SimpleNamespace(value="integrer")))
    assert "❌" in inter.out[0]


def test_integrer_refuse_sans_presse_papiers_ni_fichier(camp, monkeypatch):
    from voicebot import presse_papiers
    monkeypatch.setattr(presse_papiers, "reason_unavailable", lambda: "le presse-papiers est désactivé")
    inter = FakeInter()
    asyncio.run(make_bot().tree.commands["campagne"](inter, types.SimpleNamespace(value="integrer")))
    assert "`fichier`" in inter.out[0] and "désactivé" in inter.out[0]
