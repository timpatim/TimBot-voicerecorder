"""Rejeu d'une session avec d'autres réglages (voicebot/rejeu.py et tools/comparer_transcription.py)."""
import json
import sys
import wave
from dataclasses import replace

import numpy as np
import pytest

from voicebot import rejeu
from voicebot.constants import FRAME, RATE

F = FRAME
OLD = rejeu.Params(min_trames_voisees=10, mots_seuls_ignores="merci")       # anciens réglages : base des tests ci-dessous


def flags_of(*runs):
    """((voisé?, nombre de trames de 20 ms), ...) -> liste de booléens."""
    return [v for v, n in runs for _ in range(n)]


def test_decoupage_reglages_par_defaut_et_silence_plus_long():
    base = OLD
    # deux phrases séparées par 800 ms de silence (40 trames)
    flags = flags_of((False, 10), (True, 50), (False, 40), (True, 50), (False, 100))
    utts, _ = rejeu.segment(flags, base)
    assert len(utts) == 2                                          # 700 ms de silence suffisent à couper
    utts, _ = rejeu.segment(flags, replace(base, silence_fin_ms=1100))
    assert len(utts) == 1                                          # 1100 ms : la pause est absorbée


def test_fragments_trop_courts_ecartes_selon_le_seuil():
    flags = flags_of((False, 10), (True, 15), (False, 100), (True, 60), (False, 100))   # 300 ms puis 1,2 s de voix
    base = OLD
    assert len(rejeu.segment(flags, base)[0]) == 2
    utts, rej = rejeu.segment(flags, replace(base, min_trames_voisees=22))
    assert len(utts) == 1 and rej["trop peu de voix"] == 1


def test_enonce_trop_long_coupe_de_force():
    utts, _ = rejeu.segment(flags_of((True, 3000)), replace(OLD, enonce_max_s=20))
    assert len(utts) == 3 and all((e - s) / RATE <= 20.5 for s, e, _ in utts)


def test_fusion_des_enonces_proches_du_meme_joueur():
    p = replace(OLD, fusion_s=1.0)
    u = [rejeu.Utterance("1", "A", 0, RATE), rejeu.Utterance("1", "A", int(1.5 * RATE), 2 * RATE),
         rejeu.Utterance("1", "A", 5 * RATE, 6 * RATE)]
    merged = rejeu.merge_close(u, p)
    assert [(m.start, m.end) for m in merged] == [(0, 2 * RATE), (5 * RATE, 6 * RATE)]
    assert rejeu.merge_close(u, OLD) == u               # inactif par défaut


def test_filtre_selon_la_duree():
    p = replace(OLD, filtre_duree_s=0.6)
    assert rejeu.rejection("Au revoir.", 0.3, p) == "phrase typique sur un énoncé très court"
    assert rejeu.rejection("Au revoir.", 2.0, p) is None            # un vrai au revoir, plus long : conservé
    assert rejeu.rejection("Au revoir, à demain !", 0.3, p) is None
    assert rejeu.rejection("Au revoir.", 0.3, OLD) is None
    assert rejeu.rejection("Merci.", 5, OLD) == "mot isolé ignoré"             # règle de live.py conservée
    assert rejeu.rejection("...", 5, p) == "hallucination probable"


def test_variantes_personnalisees():
    base = OLD
    p = rejeu.parse_variant("essai=silence_fin_ms:900,fusion_s:1.5,contexte:oui", base)
    assert (p.nom, p.silence_fin_ms, p.fusion_s, p.contexte) == ("essai", 900, 1.5, True)
    with pytest.raises(ValueError, match="réglage inconnu"):
        rejeu.parse_variant("x=bidon:1", base)
    names = list(rejeu.presets(base))
    assert names == ["actuel", "reglages", "filtre", "fusion", "contexte"]
    assert rejeu.presets(base)["contexte"].fusion_s == 1.2 and rejeu.presets(base)["contexte"].contexte


def test_mesures_et_wer():
    rows = [{"name": "A", "start": 0, "end": 0.3, "text": "Au revoir."}, {"name": "A", "start": 5, "end": 6, "text": "Il faut ouvrir la porte vite."},
            {"name": "A", "start": 9, "end": 12, "text": "Je regarde dans la salle sans bruit"}]
    m = rejeu.metrics(rows)
    assert m["lignes"] == 3 and m["« au revoir / bye / amen / merci »"] == 1 and m["% < 0,5 s"] == 33.3
    assert m["% sans ponctuation finale (≥ 4 mots)"] == 33.3
    ref = [{"name": "A", "start": 5, "end": 6, "text": "Il faut ouvrir la porte vite."}]
    hyp = [{"name": "A", "start": 5, "end": 6, "text": "Il faut ouvrir la sortie vite."}]
    assert rejeu.wer(ref, hyp) == {"wer": 16.7, "erreurs": 1, "mots_ref": 6}
    assert rejeu.wer(ref, hyp, (100, 200)) == {"wer": None, "erreurs": 0, "mots_ref": 0}      # fenêtre sans référence
    assert rejeu.differences(ref, hyp)[0][3] == 1


# --- de bout en bout avec de faux WAV et un faux Whisper -------------------------------------------

def write_wav(path, seconds_amp):
    """[(secondes, amplitude)] -> WAV 16 kHz mono."""
    pcm = np.concatenate([np.full(int(s * RATE), a, dtype="<i2") for s, a in seconds_amp])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


@pytest.fixture
def session(tmp_path, monkeypatch, cfg_file):
    d = tmp_path / "2026-11-03_214508"
    (d / "audio").mkdir(parents=True)
    # Anna : une phrase, 0,8 s de silence, une phrase, puis un bruit de 0,25 s
    write_wav(d / "audio" / "Anna.wav", [(1, 0), (2, 8000), (0.8, 0), (2, 8000), (1.5, 0), (0.25, 8000), (2, 0)])
    (d / "meta.json").write_text(json.dumps({"participants": [{"id": "1", "name": "Anna", "file": "Anna.wav"}]}), encoding="utf-8")
    # détection de voix simulée : amplitude élevée = voix (indépendant de webrtcvad)
    monkeypatch.setattr(rejeu, "voiced_flags", lambda pcm, agg: [bool(abs(pcm[i * F:(i + 1) * F]).max() > 1000)
                                                                  for i in range(len(pcm) // F)])
    return d


class FakeEngine:
    def __init__(self):
        self.calls, self.prompts, self.prompt = [], [], None

    def set_prompt(self, p): self.prompt = p

    def transcribe(self, audio):
        dur = len(audio) / RATE
        self.calls.append(dur)
        self.prompts.append(self.prompt)
        text = "Au revoir." if dur < 1.0 else f"Phrase de {dur:.0f} secondes."
        return [{"start": 0.0, "end": dur, "text": text}]


def test_rejeu_variantes_sur_une_session(session, tmp_path):
    tracks = rejeu.session_tracks(session)
    assert [t[1] for t in tracks] == ["Anna"]
    engine, cache = FakeEngine(), rejeu.AsrCache(tmp_path / "cache.jsonl")
    pres = rejeu.presets(OLD)
    # actuel : deux phrases (pause de 0,8 s > 700 ms) + le bruit de 0,25 s, transcrit « Au revoir. »
    base = rejeu.transcribe_variant(pres["actuel"], tracks, engine, cache=cache)
    assert base["enonces"] == 3 and [r["text"] for r in base["rows"]][-1] == "Au revoir."
    assert rejeu.metrics(base["rows"])["« au revoir / bye / amen / merci »"] == 1
    # reglages : la pause est absorbée (une seule phrase) et le bruit, trop peu voisé, n'est même pas envoyé à Whisper
    reg = rejeu.transcribe_variant(pres["reglages"], tracks, engine, cache=cache)
    assert reg["enonces"] == 1 and reg["avant_whisper"]["trop peu de voix"] == 1 and len(reg["rows"]) == 1
    # filtre durée : on garde les 2 phrases, le « Au revoir. » du bruit est écarté par la durée de l'énoncé
    custom = rejeu.parse_variant("f=min_trames_voisees:5,filtre_duree_s:1.0", OLD)
    f = rejeu.transcribe_variant(custom, tracks, engine, cache=cache)
    assert len(f["rows"]) == 2 and f["rejetes"][0]["reason"] == "phrase typique sur un énoncé très court"
    assert f["stats_asr"]["cache"] == 3                              # mêmes audios que « actuel » : rien n'est retranscrit
    # contexte : la 2e phrase reçoit la fin de la 1re comme contexte
    engine.prompts.clear()
    ctx = rejeu.transcribe_variant(replace(rejeu.parse_variant("c=min_trames_voisees:22", OLD), contexte=True), tracks, engine)
    assert engine.prompts[0] is None and engine.prompts[1].startswith("Phrase de")
    assert ctx["stats_asr"]["contexte_applique"] is True
    # extrait : seule la 2e phrase (entre 3,8 s et 6 s)
    part = rejeu.transcribe_variant(pres["actuel"], tracks, engine, window=(3.5, 6.0))
    assert part["enonces"] == 1


def test_contexte_non_supporte_est_signale(session):
    class Bare:
        def transcribe(self, audio): return [{"start": 0, "end": 1, "text": "x y"}]
    res = rejeu.transcribe_variant(replace(OLD, contexte=True), rejeu.session_tracks(session), Bare())
    assert res["stats_asr"]["contexte_applique"] is False
    assert "n'accepte pas de contexte" in rejeu.report({"contexte": res})


def test_wav_au_mauvais_format_refuse(tmp_path):
    p = tmp_path / "x.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000); w.writeframes(bytes(400))
    with pytest.raises(ValueError, match="format inattendu"):
        rejeu.read_wav(p)


def test_script_decoupage_seul_puis_comparaison_complete(session, capsys):
    sys.path.insert(0, str(session.parents[3] if False else __import__("pathlib").Path(__file__).resolve().parents[1] / "tools"))
    import comparer_transcription as cli
    assert cli.main([str(session), "--sans-whisper", "--variantes", "actuel,reglages"]) == 0
    out = capsys.readouterr().out
    assert "actuel" in out and "reglages" in out and not (session / "comparaison" / "rapport.md").exists()
    ref = session / "ref.txt"
    ref.write_text("[00:00:01.000 → 00:00:03.000] Anna : Phrase de 3 secondes.\n", encoding="utf-8")
    assert cli.main([str(session), "--variantes", "actuel,filtre", "--variante", "perso=silence_fin_ms:1500",
                     "--reference", str(ref), "--de", "00:00:00", "--a", "00:00:04"], engine=FakeEngine()) == 0
    d = session / "comparaison"
    report = (d / "rapport.md").read_text(encoding="utf-8")
    assert "| Indicateur | actuel | filtre | perso |" in report and "WER" in report
    assert (d / "transcript_filtre.txt").exists() and (d / "differences_actuel_filtre.md").exists() and (d / "rejetes.txt").exists()
    assert (d / "cache_whisper.jsonl").exists()
    assert cli.main([str(session), "--variantes", "inconnue", "--sans-whisper"]) == 1


def test_nouveaux_reglages_par_defaut():
    """Valeurs retenues après comparaison sur une séance entière : bruit court écarté, parasites ignorés."""
    p = rejeu.from_config()
    assert (p.silence_fin_ms, p.min_trames_voisees) == (700, 22)
    assert {"merci", "au revoir", "bye", "amen"} <= {x.strip() for x in p.mots_seuls_ignores.split(",")}
    assert rejeu.rejection("Au revoir.", 2.0, p) == "mot isolé ignoré"
    assert rejeu.rejection("Au revoir à tous !", 2.0, p) is None
