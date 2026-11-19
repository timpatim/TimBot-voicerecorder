"""Mémoire de campagne : nettoyage du transcript, rollback d'une séance et « précédemment » au /record."""
import asyncio
import json
import types

import pytest

from voicebot import campagne, commands, memoire
from voicebot.config import SET
from tests.test_campagne import FakeInter, MJ, P1, make_bot


@pytest.fixture
def camp(tmp_path, monkeypatch, cfg_file):
    monkeypatch.setitem(SET.values, "campagnes.dossier", str(tmp_path / "camp"))
    monkeypatch.setitem(SET.values, "sessions.dossier", str(tmp_path / "sessions"))
    store = campagne.CampaignStore(tmp_path / "camp")
    c = store.create("Camp", "Pathfinder", MJ, [MJ, P1])
    j = c.joueurs()
    j["joueurs"][0]["personnage"] = "Max le Rouge"
    c.save_joueurs(j)
    return c, None


def integrer(c, sid, precedemment="Dans l'épisode précédent de Camp… Max est en ville."):
    """Une séance intégrée, écrite à la main (le résumé lui-même est testé dans test_analyse / test_claude)."""
    seance = c.path / "seances" / sid
    seance.mkdir(parents=True)
    (seance / "compte_rendu.md").write_text("# Compte rendu\nMax est entré.\n", encoding="utf-8")
    (seance / "precedemment.md").write_text(precedemment + "\n", encoding="utf-8")
    version = campagne.add_version(c, "# État\nMax est en ville.", sid)
    (seance / "meta.json").write_text(json.dumps({"version": version.name}), encoding="utf-8")


ROWS = [
    {"user": "1", "name": "couette_", "start": 0, "end": 3, "text": "Vous arrivez devant la porte."},
    {"user": "2", "name": "silentgob_", "start": 4, "end": 5, "text": "merci"},
    {"user": "2", "name": "silentgob_", "start": 6, "end": 8, "text": "J'ouvre la porte."},
    {"user": "2", "name": "silentgob_", "start": 9, "end": 10, "text": "doucement."},
    {"user": "9", "name": "inconnu", "start": 2000, "end": 2003, "text": "Un autre passage."},
]


def test_nettoyage_etiquettes_par_id_sans_fusion_par_defaut(camp):
    c, _ = camp
    rows = memoire.clean_rows(ROWS, memoire.labeler(c.joueurs()))
    assert [(r[0], r[2], r[3]) for r in rows] == [
        (0, "MJ", "Vous arrivez devant la porte."),
        (4, "Max le Rouge (Silent)", "merci"),                # conservé : seuls les sons sans sens sont retirés
        (6, "Max le Rouge (Silent)", "J'ouvre la porte."),    # une réplique = une ligne, horodatage d'origine
        (9, "Max le Rouge (Silent)", "doucement."),
        (2000, "inconnu", "Un autre passage.")]
    assert len(memoire.windows(rows, 30)) == 2


def test_reponses_et_paroles_simultanees_preservees(camp):
    c, _ = camp
    rows = [{"user": "1", "name": "mj", "start": 10, "end": 18, "text": "Tu veux attaquer ?"},
            {"user": "2", "name": "p", "start": 15, "end": 16, "text": "Non."},               # réponse : à garder
            {"user": "2", "name": "p", "start": 16.5, "end": 17, "text": "euh"},             # son sans sens : retiré
            {"user": "1", "name": "mj", "start": 17, "end": 20, "text": "D'accord, tour suivant."}]
    out = memoire.clean_rows(rows, memoire.labeler(c.joueurs()))
    assert [(r[0], r[2], r[3]) for r in out] == [(10, "MJ", "Tu veux attaquer ?"), (15, "Max le Rouge (Silent)", "Non."),
                                                  (17, "MJ", "D'accord, tour suivant.")]


def test_fusion_optionnelle_jamais_par_dessus_un_autre_locuteur(camp, monkeypatch):
    c, _ = camp
    rows = [{"user": "1", "name": "mj", "start": 0, "end": 2, "text": "Un."},
            {"user": "1", "name": "mj", "start": 3, "end": 4, "text": "Deux."},
            {"user": "2", "name": "p", "start": 4.5, "end": 5, "text": "Attends."},
            {"user": "1", "name": "mj", "start": 5.5, "end": 6, "text": "Trois."}]
    out = memoire.clean_rows(rows, memoire.labeler(c.joueurs()), gap_merge_s=2)
    assert [(r[0], r[3]) for r in out] == [(0, "Un. Deux."), (4.5, "Attends."), (5.5, "Trois.")]


def test_parse_formats_de_transcript():
    bot = "# en-tête\n[00:00:01.500 → 00:00:03.000] silentgob : Bonjour"
    srt = "1\n00:00:01,000 --> 00:00:02,000\ncouette : Salut"
    assert memoire.parse_transcript_text(bot)[0]["name"] == "silentgob" and memoire.parse_transcript_text(bot)[0]["start"] == 1.5
    assert memoire.parse_transcript_text(srt)[0]["text"] == "Salut"


def test_rollback_retire_la_seance_et_son_precedemment(camp):
    c, _ = camp
    campagne.add_version(c, "# journal", "journal")
    integrer(c, "2026-10-30_200000")
    assert memoire.latest_precedemment(c) and c.nb_seances() == 1
    undone, now, gone = campagne.rollback(c)
    assert gone == "2026-10-30_200000" and now.startswith("v001")
    assert memoire.latest_precedemment(c) is None and c.nb_seances() == 0
    assert (c.path / "seances" / "_annulees" / "2026-10-30_200000" / "compte_rendu.md").exists()
    integrer(c, "2026-10-30_200000")                                      # on peut refaire la séance après rollback
    assert len(campagne.versions(c)) == 2


class Att:
    """Pièce jointe Discord factice."""
    def __init__(self, text): self._b, self.size = text.encode(), len(text.encode())
    async def read(self): return self._b


def test_record_affiche_le_precedemment(camp):
    c, _ = camp
    integrer(c, "2026-10-30_200000")
    assert memoire.latest_precedemment(c) == "Dans l'épisode précédent de Camp… Max est en ville."
