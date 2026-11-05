"""Voix du bot : nettoyage du texte, cache de l'audio, découpage, installation et commandes (Chatterbox simulé)."""
import asyncio
import tempfile
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from voicebot import campagne, commands, memoire, pack, voix
from voicebot.config import SET
from tests.test_campagne import FakeInter, MJ, P1, make_bot
from tests.test_pack import REPONSE, ROWS, SID, camp  # noqa: F401  (fixture camp)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import tts_chatterbox  # noqa: E402
import tts_omnivoice  # noqa: E402
import fix_voix_torch  # noqa: E402


def integrated(camp):
    pack.build_packs(camp, ROWS[:3], SID)
    return pack.integrate(camp, REPONSE)


class FakeRun:
    """Remplace le lancement du processus Chatterbox : écrit un WAV bidon, ou échoue."""
    def __init__(self, code=0, stderr=b"", wav_bytes=b"RIFFfake", stdout=b""):
        self.code, self.stderr, self.cmds, self.wav_bytes, self.stdout = code, stderr, [], wav_bytes, stdout

    lines = ()                                         # lignes que le worker « écrit » au fil de l'eau

    def __call__(self, cmd, timeout, on_line=None):
        self.cmds.append(cmd)
        for line in (self.lines if on_line else ()):
            on_line(line)
        if self.code == 0:
            Path(cmd[cmd.index("--out") + 1]).write_bytes(self.wav_bytes)
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, self.stderr)


def test_nettoyage_du_texte_pour_la_voix():
    raw = "# Titre\n**Dans l'épisode précédent** de [Camp](http://x)…\n- puce [secret:Max] un\n\n\n\nFin."
    assert voix.clean_for_speech(raw) == "Titre\nDans l'épisode précédent de Camp…\npuce un\n\nFin."


def test_decoupage_en_morceaux_courts():
    text = "Première phrase. Deuxième phrase plus longue, avec une virgule, et encore des mots pour dépasser. " * 4 + "\n\nAutre paragraphe."
    chunks = tts_chatterbox.split_text(text, 80)
    assert all(len(c) <= 80 for c in chunks) and chunks[-1] == "Autre paragraphe." and len(chunks) > 4
    assert tts_chatterbox.split_text("  \n ") == []


def test_audio_genere_puis_reutilise_depuis_le_cache(camp, monkeypatch):
    out = integrated(camp)
    run = FakeRun()
    monkeypatch.setattr(voix, "_run", run)
    wav = voix.ensure_audio(camp, SID, out["precedemment"])
    assert wav.read_bytes() == b"RIFFfake" and len(run.cmds) == 1 and "--voice" not in run.cmds[0]
    assert run.cmds[0][run.cmds[0].index("--lang") + 1] == "fr"
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert len(run.cmds) == 1                                               # cache : pas de 2e synthèse
    voix.ensure_audio(camp, SID, out["precedemment"] + " Un mot de plus.")
    assert len(run.cmds) == 2                                               # texte modifié : nouvelle synthèse


def test_voix_clonee_si_un_echantillon_existe_sinon_voix_par_defaut(camp, monkeypatch, tmp_path):
    out = integrated(camp)
    run = FakeRun()
    monkeypatch.setattr(voix, "_run", run)
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert "--voice" not in run.cmds[-1]
    general = tmp_path / "general.wav"
    general.write_bytes(b"x" * 10)
    monkeypatch.setitem(SET.values, "voix.voix_reference", str(general))
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert run.cmds[-1][run.cmds[-1].index("--voice") + 1] == str(general)   # réglage général
    (camp.path / "voix.wav").write_bytes(b"y" * 10)
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert run.cmds[-1][run.cmds[-1].index("--voice") + 1] == str(camp.path / "voix.wav")   # la campagne prime


@pytest.mark.parametrize("stderr,expected", [
    (b"ModuleNotFoundError: No module named 'omnivoice'", "install_omnivoice.bat"),
    (b"torch.OutOfMemoryError: CUDA out of memory", "Mémoire GPU"),
    (b"boom\nautre erreur", "autre erreur")])
def test_erreurs_de_synthese_lisibles(camp, monkeypatch, stderr, expected):
    out = integrated(camp)
    monkeypatch.setattr(voix, "_run", FakeRun(1, stderr))
    with pytest.raises(voix.VoixError, match=expected):
        voix.ensure_audio(camp, SID, out["precedemment"])


def test_delai_depasse(camp, monkeypatch):
    out = integrated(camp)
    def slow(cmd, timeout, on_line=None): raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(voix, "_run", slow)
    with pytest.raises(voix.VoixError, match="dépassé"):
        voix.ensure_audio(camp, SID, out["precedemment"])


def test_python_de_la_voix(monkeypatch, tmp_path):
    monkeypatch.setattr(voix, "ROOT", tmp_path)
    assert voix.python_exe() == sys.executable
    (tmp_path / ".voix_python_omnivoice").write_text("C:\\envs\\voix\\python.exe\n", encoding="utf-8")
    assert voix.python_exe() == "C:\\envs\\voix\\python.exe"
    monkeypatch.setitem(SET.values, "voix.moteur", "chatterbox")             # chaque moteur a son environnement
    assert voix.python_exe() == sys.executable
    (tmp_path / ".voix_python").write_text("C:\\envs\\chatter\\python.exe\n", encoding="utf-8")
    assert voix.python_exe() == "C:\\envs\\chatter\\python.exe"
    monkeypatch.setitem(SET.values, "voix.python", "D:\\autre\\python.exe")
    assert voix.python_exe() == "D:\\autre\\python.exe"


def test_torch_cuda_de_la_meme_version():
    assert fix_voix_torch.target_variant("2.6.0+cpu", "cu128") == "cu126"      # cu128 n'existe pas pour torch 2.6
    assert fix_voix_torch.target_variant("2.8.0+cpu", "cu128") == "cu128"
    cmd = fix_voix_torch.build_command("2.6.0+cpu", "cu126")
    assert "torch==2.6.0" in cmd and "torchaudio==2.6.0" in cmd and "--no-deps" in cmd
    assert cmd[cmd.index("--index-url") + 1].endswith("/cu126")


def test_cached_audio_ne_synthetise_jamais_et_detecte_un_texte_modifie(camp, monkeypatch):
    out = integrated(camp)
    run = FakeRun()
    monkeypatch.setattr(voix, "_run", run)
    assert voix.cached_audio(camp, SID, out["precedemment"]) is None and run.cmds == []
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert voix.cached_audio(camp, SID, out["precedemment"]) is not None
    assert voix.cached_audio(camp, SID, out["precedemment"] + " Modifié.") is None       # texte changé : audio périmé


def test_une_seule_synthese_a_la_fois(camp, monkeypatch):
    out = integrated(camp)
    def reentrant(cmd, timeout, on_line=None):
        assert voix.is_busy()
        with pytest.raises(voix.VoixError, match="déjà en cours"):
            voix.ensure_audio(camp, "autre-seance", "Un autre texte.")
        return FakeRun()(cmd, timeout, on_line)
    monkeypatch.setattr(voix, "_run", reentrant)
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert not voix.is_busy()


# --- commandes ------------------------------------------------------------------------------------

class FakeVC:
    def __init__(self): self.played = []; self.disconnected = False

    def is_connected(self): return True
    def play(self, source, after=None): self.played.append(source); after(None)
    async def disconnect(self): self.disconnected = True


def voice_inter(vc, perm=True):
    inter = FakeInter(perm=perm)
    inter.user.voice.channel.connect = lambda: asyncio.sleep(0, vc)
    return inter


def run_action(inter, action, bot=None, **kw):
    cmd = (bot or make_bot()).tree.commands["campagne"]
    asyncio.run(cmd(inter, types.SimpleNamespace(value=action), **kw))


@pytest.fixture(autouse=True)
def pas_de_processus_permanent(monkeypatch):
    """Les tests lancent la synthèse « une fois » (voix._run factice) ; le processus permanent a ses propres tests."""
    monkeypatch.setattr(voix, "use_server", lambda: False)


@pytest.fixture(autouse=True)
def whisper_libere(monkeypatch):
    """La plupart des tests couvrent le mode « Whisper libéré pendant la synthèse » ; le mode inverse a ses propres tests."""
    monkeypatch.setitem(SET.values, "voix.liberer_whisper", True)


@pytest.fixture(autouse=True)
def engine_calls(monkeypatch):
    """Trace les libérations / rechargements de Whisper (le vrai moteur ne charge rien dans les tests)."""
    from voicebot.whisper_engine import WhisperEngine
    calls = []
    monkeypatch.setattr(WhisperEngine, "release", lambda self: calls.append("release") or True)
    monkeypatch.setattr(WhisperEngine, "start_loading", lambda self: calls.append("load"))
    return calls


@pytest.fixture
def voix_on(monkeypatch):
    monkeypatch.setitem(SET.values, "voix.activer", True)
    monkeypatch.setitem(SET.values, "voix.liberer_whisper", True)       # mode « Whisper libéré » ; l'autre a ses tests
    return FakeRun()


def test_voix_desactivee_par_defaut(camp):
    for action in ("dicter", "audio"):
        inter = FakeInter()
        run_action(inter, action)
        assert "désactivée" in inter.out[-1]


def test_dicter_refuse_si_l_audio_n_est_pas_prepare_et_ne_synthetise_pas(camp, voix_on, monkeypatch):
    integrated(camp)
    monkeypatch.setattr(voix, "_run", voix_on)
    vc = FakeVC()
    inter = voice_inter(vc)
    run_action(inter, "dicter")
    assert "non prêt" in inter.out[-1] and voix_on.cmds == [] and vc.played == []


def test_audio_prepare_puis_dicter_lit_et_quitte(camp, voix_on, monkeypatch, engine_calls):
    integrated(camp)
    monkeypatch.setattr(voix, "_run", voix_on)
    inter = voice_inter(FakeVC())
    run_action(inter, "audio")
    assert "prêt" in inter.out[-1] and len(voix_on.cmds) == 1
    assert engine_calls == ["release", "load"]                                  # Whisper libéré avant, rechargé après
    run_action(voice_inter(FakeVC()), "audio")                                  # déjà prêt : pas de 2e synthèse
    assert len(voix_on.cmds) == 1 and engine_calls == ["release", "load"]
    vc = FakeVC()
    inter2 = voice_inter(vc)
    run_action(inter2, "dicter")
    assert len(vc.played) == 1 and vc.played[0][0] == "ffmpeg" and vc.disconnected and not commands._speaking


def test_audio_refuse_pendant_un_enregistrement_et_sans_permission(camp, voix_on, monkeypatch):
    integrated(camp)
    monkeypatch.setattr(voix, "_run", voix_on)
    bot = make_bot()
    bot.sessions[99] = (object(), object(), FakeVC())                           # enregistrement sur n'importe quel serveur
    inter = FakeInter()
    run_action(inter, "audio", bot)
    assert "Impossible pendant un enregistrement" in inter.out[-1] and voix_on.cmds == []
    inter2 = FakeInter(perm=False)
    run_action(inter2, "audio")
    assert "Gérer le serveur" in inter2.out[-1]


def test_dicter_dans_l_enregistrement_en_cours(camp, voix_on, monkeypatch):
    out = integrated(camp)
    monkeypatch.setattr(voix, "_run", voix_on)
    voix.ensure_audio(camp, SID, out["precedemment"])
    vc, bot, inter = FakeVC(), make_bot(), FakeInter()
    bot.sessions[7] = (object(), object(), vc)
    run_action(inter, "dicter", bot)
    assert len(vc.played) == 1 and not vc.disconnected                         # reste connecté à l'enregistrement


def test_recap_au_demarrage_lit_seulement_un_audio_pret(camp, voix_on, monkeypatch):
    out = integrated(camp)
    monkeypatch.setattr(voix, "_run", voix_on)
    vc, bot, sent = FakeVC(), make_bot(), []
    bot.sessions[7] = (object(), object(), vc)
    channel = types.SimpleNamespace(send=lambda msg: asyncio.sleep(0, sent.append(msg)))
    asyncio.run(commands.speak_recap(bot, 7, camp, vc, channel))                # audio pas prêt : message, pas de synthèse
    assert vc.played == [] and voix_on.cmds == [] and "non prêt" in sent[0]
    voix.ensure_audio(camp, SID, out["precedemment"])
    asyncio.run(commands.speak_recap(bot, 7, camp, vc, channel))
    assert len(vc.played) == 1 and len(sent) == 1


def test_integrer_prepare_l_audio_automatiquement_sauf_pendant_un_enregistrement(camp, voix_on, monkeypatch, tmp_path):
    monkeypatch.setattr(voix, "_run", voix_on)
    pack.build_packs(camp, ROWS[:3], SID)
    from tests.test_memoire import Att

    async def go(bot, inter):
        await bot.tree.commands["campagne"](inter, types.SimpleNamespace(value="integrer"), fichier=Att(REPONSE))
        await asyncio.gather(*list(commands._tasks))                            # attend la préparation en arrière-plan
    bot, inter = make_bot(), FakeInter()
    asyncio.run(go(bot, inter))
    assert any("en préparation" in str(o) for o in inter.out) and len(voix_on.cmds) == 1
    assert voix.cached_audio(camp, SID, memoire.seance_text(camp, "precedemment.md")[1]) is not None
    # avec un enregistrement en cours ailleurs : pas de synthèse
    camp2_dir = SID + "b"
    pack.build_packs(camp, ROWS[:3], camp2_dir)
    bot2, inter2 = make_bot(), FakeInter()
    bot2.sessions[99] = (object(), object(), FakeVC())
    inter2.guild_id = 8
    asyncio.run(go(bot2, inter2))
    assert any("non préparé" in str(o) for o in inter2.out) and len(voix_on.cmds) == 1


def test_record_refuse_pendant_la_synthese(camp, voix_on, monkeypatch):
    bot = make_bot()
    inter = FakeInter()
    inter.user.voice = types.SimpleNamespace(channel=object())
    monkeypatch.setattr(voix, "is_busy", lambda: True)
    asyncio.run(bot.tree.commands["record"](inter))
    assert "en cours de préparation" in inter.out[-1]


def test_whisper_rechargé_meme_si_la_synthese_echoue(camp, voix_on, monkeypatch, engine_calls):
    integrated(camp)
    monkeypatch.setattr(voix, "_run", FakeRun(1, b"boom"))
    inter = voice_inter(FakeVC())
    run_action(inter, "audio")
    assert "❌" in inter.out[-1] and engine_calls == ["release", "load"] and not commands._preparing["on"]


def test_moteur_whisper_liberation_et_rechargement(monkeypatch):
    from voicebot.whisper_engine import WhisperEngine
    monkeypatch.undo()                                                          # vraies méthodes, pas les doublures de la fixture
    engine = WhisperEngine()
    assert engine.release() is False                                            # jamais chargé : rien à libérer
    engine.state, engine.model, engine.align_model = "ready", object(), object()
    engine._done.set()
    assert engine.release() is True and engine.state == "released" and engine.model is None and engine.align_model is None
    assert engine.release() is False                                            # déjà libéré
    loads = []
    monkeypatch.setattr(WhisperEngine, "_load", lambda self: loads.append(1) or self._done.set())
    engine.start_loading()
    engine._done.wait(5)
    assert loads == [1] and engine.state == "loading"                           # (l'état « prêt » est posé par _load réel)


def test_message_clair_quand_le_filigrane_perth_est_indisponible(camp, monkeypatch):
    out = integrated(camp)
    err = b"Traceback...\n  self.watermarker = perth.PerthImplicitWatermarker()\nTypeError: 'NoneType' object is not callable"
    monkeypatch.setattr(voix, "_run", FakeRun(1, err))
    with pytest.raises(voix.VoixError, match=r'setuptools<81'):
        voix.ensure_audio(camp, SID, out["precedemment"])


def test_le_worker_diagnostique_perth(monkeypatch, capsys):
    fake = types.ModuleType("perth")
    fake.PerthImplicitWatermarker = None
    monkeypatch.setitem(sys.modules, "perth", fake)
    monkeypatch.setitem(sys.modules, "perth.perth_net", None)                   # force ImportError sur l'import
    with pytest.raises(SystemExit) as e:
        tts_chatterbox.check_perth()
    assert e.value.code == 3 and "setuptools<81" in capsys.readouterr().err


def test_vitesse_et_pause_changent_la_cle_du_cache(camp, cfg_file):
    from voicebot.config import SET
    k1 = voix.cache_key("bonjour", None)
    SET.values["voix.vitesse"] = 1.5
    assert voix.cache_key("bonjour", None) != k1


def test_acceleration_ffmpeg_et_repli_si_echec(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"orig")
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"rapide")
        return types.SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(voix.subprocess, "run", fake)
    voix._speed_up(wav, 1.2)
    assert wav.read_bytes() == b"rapide" and "atempo=1.2" in calls[0]
    monkeypatch.setattr(voix.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stderr=b"boom"))
    wav.write_bytes(b"orig")
    voix._speed_up(wav, 1.2)
    assert wav.read_bytes() == b"orig"
    voix._speed_up(wav, 1.0)                                   # vitesse normale : rien à faire


def test_la_consigne_de_longueur_suit_le_reglage():
    from voicebot import memoire
    from voicebot.config import SET
    SET.values["voix.mots"] = 80
    assert "80 mots" in memoire.longueur_precedemment()


def test_forcer_la_regeneration_garde_l_ancien_audio_et_le_restaure_en_cas_d_echec(camp, monkeypatch):
    out = integrated(camp)
    monkeypatch.setattr(voix, "_run", FakeRun(0, b"", wav_bytes=b"V1"))
    wav = voix.ensure_audio(camp, SID, out["precedemment"])
    assert wav.read_bytes() == b"V1"
    assert voix.ensure_audio(camp, SID, out["precedemment"]) == wav            # à jour : pas de nouvelle synthèse
    monkeypatch.setattr(voix, "_run", FakeRun(0, b"", wav_bytes=b"V2"))
    assert voix.ensure_audio(camp, SID, out["precedemment"], force=True).read_bytes() == b"V2"
    assert wav.with_suffix(".precedent.wav").read_bytes() == b"V1"
    monkeypatch.setattr(voix, "_run", FakeRun(1, b"boom"))
    with pytest.raises(voix.VoixError):
        voix.ensure_audio(camp, SID, out["precedemment"], force=True)
    assert wav.read_bytes() == b"V2" and voix.cached_audio(camp, SID, out["precedemment"]) == wav


def test_audio_force_regenere_meme_si_pret(camp, voix_on, monkeypatch, engine_calls):
    integrated(camp)
    monkeypatch.setattr(voix, "_run", voix_on)
    run_action(voice_inter(FakeVC()), "audio")
    run_action(voice_inter(FakeVC()), "audio")                                  # prêt : rien de nouveau
    assert len(voix_on.cmds) == 1
    run_action(voice_inter(FakeVC()), "audio force")
    assert len(voix_on.cmds) == 2


def test_moteur_chatterbox_garde_son_worker_son_installeur_et_ses_messages(camp, monkeypatch):
    out = integrated(camp)
    monkeypatch.setitem(SET.values, "voix.moteur", "chatterbox")
    run = FakeRun()
    monkeypatch.setattr(voix, "_run", run)
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert run.cmds[-1][1].endswith("tts_chatterbox.py") and "--temperature" in run.cmds[-1] and "--speed" not in run.cmds[-1]
    monkeypatch.setattr(voix, "_run", FakeRun(1, b"No module named 'chatterbox'"))
    with pytest.raises(voix.VoixError, match="install_voix.bat"):
        voix.ensure_audio(camp, SID, out["precedemment"], force=True)


def test_omnivoice_par_defaut_vitesse_native_et_texte_de_reference(camp, monkeypatch):
    out = integrated(camp)
    ref = camp.path / "voix.wav"
    ref.write_bytes(b"x" * 10)
    ref.with_suffix(".txt").write_text("Bonjour, voici ma voix.\n", encoding="utf-8")
    run = FakeRun()
    monkeypatch.setattr(voix, "_run", run)
    seen = []
    monkeypatch.setattr(voix, "_speed_up", lambda wav, f: seen.append(f))
    voix.ensure_audio(camp, SID, out["precedemment"])
    cmd = run.cmds[-1]
    assert cmd[1].endswith("tts_omnivoice.py") and "--temperature" not in cmd
    assert "--speed" not in cmd and cmd[cmd.index("--steps") + 1] == "32"                 # la vitesse est réglée par ffmpeg
    assert cmd[cmd.index("--voice-text") + 1] == "Bonjour, voici ma voix."
    assert seen == [SET["voix.vitesse"]]                                       # le bot accélère avec ffmpeg
    monkeypatch.setattr(voix, "_run", FakeRun(stdout=voix.SPEED_APPLIED.encode()))   # un worker qui l'a déjà fait : pas de double
    voix.ensure_audio(camp, SID, out["precedemment"], force=True)
    assert seen == [SET["voix.vitesse"]]


def test_changer_de_moteur_ou_de_texte_de_reference_invalide_l_audio(camp, monkeypatch):
    out = integrated(camp)
    monkeypatch.setattr(voix, "_run", FakeRun())
    assert voix.cached_audio(camp, SID, out["precedemment"]) is None
    voix.ensure_audio(camp, SID, out["precedemment"])
    assert voix.cached_audio(camp, SID, out["precedemment"]) is not None
    monkeypatch.setitem(SET.values, "voix.moteur", "chatterbox")
    assert voix.cached_audio(camp, SID, out["precedemment"]) is None


def test_worker_omnivoice_choisit_les_parametres_selon_la_signature():
    import argparse
    args = argparse.Namespace(speed=1.2, steps=16, lang="fr", voice="r.wav", voice_text="texte")
    kwargs, notes = tts_omnivoice.pick_params({"text", "speed", "num_step", "language", "ref_audio", "ref_text"}, args)
    assert kwargs == {"speed": 1.2, "num_step": 16, "language": "fr", "ref_audio": "r.wav", "ref_text": "texte"} and notes == []
    kwargs, notes = tts_omnivoice.pick_params({"text", "ref_audio"}, args)          # ni vitesse ni langue : on le dit
    assert "speed" not in kwargs and "language" not in kwargs and len(notes) == 2
    kwargs, _ = tts_omnivoice.pick_params(None, args)                               # **kwargs : on ne devine pas la langue
    assert "language" not in kwargs and kwargs["speed"] == 1.2


# --- progression et fin de lecture ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pas_d_attente_apres_lecture(monkeypatch):
    monkeypatch.setattr(commands, "PLAY_TAIL_S", 0)


def test_la_progression_suit_les_lignes_du_worker(camp, monkeypatch):
    out = integrated(camp)
    run = FakeRun()
    run.lines = ["Chargement d'OmniVoice (cuda)...", "MORCEAU 1/2", "FAIT 1/2 duree=4.0s", "ALERTE : morceau 2 trop court",
                 "MORCEAU 2/2", "FAIT 2/2 duree=5.0s", "OK"]
    monkeypatch.setattr(voix, "_run", run)
    seen = []
    voix.ensure_audio(camp, SID, out["precedemment"], progress=lambda *a: seen.append(a))
    assert seen[0] == ("Chargement du modèle", 0, 0)
    assert ("Synthèse", 0, 2) in seen and ("Synthèse", 1, 2) in seen and ("Synthèse", 2, 2) in seen
    assert seen[-1] == ("Finalisation", 1, 1)


def test_run_lit_la_sortie_au_fil_de_l_eau_et_respecte_le_delai():
    lines = []
    done = voix._run([sys.executable, "-c", "print('MORCEAU 1/1', flush=True); print('OK')"], 30, lines.append)
    assert lines == ["MORCEAU 1/1", "OK"] and done.returncode == 0 and b"OK" in done.stdout
    bad = voix._run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], 30, lines.append)
    assert bad.returncode == 3 and bad.stderr == b"boom"
    with pytest.raises(subprocess.TimeoutExpired):
        voix._run([sys.executable, "-c", "import time; time.sleep(30)"], 1, lines.append)


def test_texte_de_progression():
    assert "30 %" in commands.progress_text("Synthèse", 3, 10) and "3/10" in commands.progress_text("Synthèse", 3, 10)
    assert commands.progress_text("Chargement du modèle", 0, 0, "Titre").startswith("🎙️ Titre")
    assert "▰" * commands.BAR_WIDTH in commands.progress_text("Synthèse", 4, 4)


def test_la_commande_audio_affiche_la_progression_puis_le_resultat(camp, voix_on, monkeypatch):
    integrated(camp)
    voix_on.lines = ["MORCEAU 1/2", "FAIT 1/2 duree=3.0s", "MORCEAU 2/2", "FAIT 2/2 duree=3.0s"]
    monkeypatch.setattr(voix, "_run", voix_on)
    inter = voice_inter(FakeVC())
    edits = []

    async def edit(content=None, **kw): edits.append(content)
    inter.edit_original_response = edit
    run_action(inter, "audio")
    assert any("Synthèse" in e for e in edits) and "prêt" in edits[-1]
    assert not any("prêt" in (o or "") for o in inter.out)                      # un seul message : l'original est modifié


def test_worker_omnivoice_detecte_un_morceau_tronque():
    chunk = "Dans l'épisode précédent de la campagne les héros entrent dans la crypte oubliée"   # 13 mots, ~4,8 s attendues
    assert tts_omnivoice.is_truncated(1.5, chunk, 1.0)
    assert not tts_omnivoice.is_truncated(4.5, chunk, 1.0)
    assert not tts_omnivoice.is_truncated(0.2, "Oui.", 1.0)                       # trop court pour juger


# --- OmniVoice : morceaux tronqués -------------------------------------------------

class FakeModel:
    """generate() qui rend un tableau dont la durée dépend du réglage `duration`."""
    def __init__(self, short_seconds=1.0):
        self.calls, self.short = [], short_seconds

    def generate(self, text, **kw):
        import numpy as np
        self.calls.append(kw)
        seconds = kw.get("duration", self.short)
        return [np.zeros(int(tts_omnivoice.SAMPLE_RATE * seconds), dtype=np.float32)]


CHUNK = "Dans l'épisode précédent de la campagne les héros entrent dans la crypte oubliée"


def test_un_morceau_tronque_est_regenere_avec_une_duree_imposee():
    model, logs = FakeModel(short_seconds=1.0), []
    wav, seconds = tts_omnivoice.synthesize_chunk(model, CHUNK, {"speed": 1.0}, 1.0, True, logs.append)
    assert "duration" not in model.calls[0] and "duration" in model.calls[1]
    assert seconds >= tts_omnivoice.MIN_RATIO * tts_omnivoice.expected_seconds(CHUNK, 1.0)
    assert len(model.calls) == 2 and any("trop court" in m for m in logs)


def test_un_morceau_complet_n_est_pas_regenere_et_les_essais_sont_limites():
    model = FakeModel(short_seconds=6.0)
    tts_omnivoice.synthesize_chunk(model, CHUNK, {}, 1.0, True, lambda m: None)
    assert len(model.calls) == 1
    stubborn = FakeModel(short_seconds=0.5)                                       # ignore même la durée imposée
    stubborn.generate = lambda text, **kw: FakeModel(0.5).generate(text)         # (toujours trop court)
    wav, seconds = tts_omnivoice.synthesize_chunk(stubborn, CHUNK, {}, 1.0, True, lambda m: None)
    assert seconds == 0.5                                                          # on garde le meilleur, sans boucler


# --- dossier des modèles ---------------------------------------------------------------------------------

def test_les_modeles_vont_dans_le_dossier_du_programme_par_defaut(monkeypatch):
    from voicebot import modeles
    env = modeles.env_vars()
    assert SET["modeles.dossier"] == "modeles"
    assert Path(env["HF_HOME"]) == modeles.ROOT / "modeles" / "huggingface"
    assert Path(env["HF_HUB_CACHE"]) == modeles.ROOT / "modeles" / "huggingface" / "hub"
    assert Path(env["TORCH_HOME"]) == modeles.ROOT / "modeles" / "torch"
    monkeypatch.setitem(SET.values, "modeles.dossier", "D:/ia/modeles" if sys.platform == "win32" else "/data/modeles")
    assert Path(modeles.env_vars()["HF_HOME"]).parts[-2:] == ("modeles", "huggingface")      # chemin absolu respecté
    assert not str(modeles.env_vars()["HF_HOME"]).startswith(str(modeles.ROOT))
    monkeypatch.setitem(SET.values, "modeles.dossier", "")
    assert modeles.env_vars() == {}                                                         # vide : cache habituel


def test_le_worker_recoit_les_dossiers_de_modeles(monkeypatch, tmp_path):
    from voicebot import modeles
    monkeypatch.setitem(SET.values, "modeles.dossier", str(tmp_path / "m"))
    seen = []
    done = voix._run([sys.executable, "-c", "import os; print(os.environ['HF_HOME'])"], 30, seen.append)
    assert seen == [str(tmp_path / "m" / "huggingface")] and done.returncode == 0
    plain = voix._run([sys.executable, "-c", "import os; print(os.environ['TORCH_HOME'])"], 30)
    assert plain.stdout.decode().strip() == str(tmp_path / "m" / "torch")
    env_before = dict(__import__("os").environ)
    modeles.apply()
    assert (tmp_path / "m" / "huggingface" / "hub").is_dir()
    __import__("os").environ.clear()
    __import__("os").environ.update(env_before)


# --- audio « introuvable » : les causes sont distinguées -----------------------------------------------------

def prepared(camp, monkeypatch):
    out = integrated(camp)
    monkeypatch.setattr(voix, "_run", FakeRun())
    voix.ensure_audio(camp, SID, out["precedemment"])
    return out["precedemment"]


def test_changer_un_reglage_de_voix_ne_rend_pas_l_audio_introuvable(camp, monkeypatch):
    text = prepared(camp, monkeypatch)
    assert voix.audio_state(camp, SID, text)["etat"] == "ok"
    monkeypatch.setitem(SET.values, "voix.vitesse", 1.3)
    monkeypatch.setitem(SET.values, "voix.pause_s", 0.9)
    state = voix.audio_state(camp, SID, text)
    assert state["etat"] == "reglages" and state["wav"] is not None                 # lisible
    assert "vitesse : 1.0 → 1.3" in state["detail"] and "pause_s" in state["detail"]
    assert voix.cached_audio(camp, SID, text) is None                              # mais plus « à jour » : /campagne audio refait
    assert voix.playable_audio(camp, SID, text)[0] is not None


def test_texte_modifie_audio_refuse_et_fichier_absent(camp, monkeypatch):
    text = prepared(camp, monkeypatch)
    assert voix.audio_state(camp, SID, text + " Une phrase de plus.")["etat"] == "texte"
    assert voix.playable_audio(camp, SID, text + " Autre.")[0] is None
    assert voix.audio_state(camp, "2000-01-01", text)["etat"] == "absent"


def test_audio_prepare_par_une_ancienne_version_reste_lisible(camp, monkeypatch):
    text = prepared(camp, monkeypatch)
    wav, meta, src = voix._paths(camp, SID)
    meta.write_text('{"cle": "ancienne-cle", "voix": "défaut"}\n', encoding="utf-8")      # ancien format, sans réglages
    state = voix.audio_state(camp, SID, text)
    assert state["etat"] == "reglages" and state["wav"] == wav and "réglages de la voix modifiés" in state["detail"]


def test_dicter_lit_un_audio_aux_reglages_changes_et_explique_l_absence(camp, voix_on, monkeypatch):
    text = prepared(camp, monkeypatch)
    monkeypatch.setitem(SET.values, "voix.activer", True)
    monkeypatch.setitem(SET.values, "voix.vitesse", 1.4)
    vc = FakeVC()
    inter = voice_inter(vc)
    sent = []

    async def followup(content=None, **kw): sent.append(content)
    inter.followup = types.SimpleNamespace(send=followup)
    run_action(inter, "dicter")
    assert len(vc.played) == 1 and any("autres réglages" in (x or "") for x in sent)
    # une séance plus récente, sans audio : le message nomme la séance visée et propose celle qui a un audio
    newer = camp.path / "seances" / "2999-01-01"
    newer.mkdir(parents=True)
    (newer / "precedemment.md").write_text("Dans l'épisode précédent…", encoding="utf-8")
    inter2 = voice_inter(FakeVC())
    run_action(inter2, "dicter")
    msg = inter2.out[-1]
    assert "non prêt pour la séance 2999-01-01" in msg and SID in msg


# --- /dire ---------------------------------------------------------------------------------------------------

def run_dire(inter, texte, bot=None):
    asyncio.run((bot or make_bot()).tree.commands["dire"](inter, texte))


@pytest.fixture(autouse=True)
def dossier_temporaire_isole(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp_systeme"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


def temporaires(tmp_path):
    return list((tmp_path / "tmp_systeme").glob(voix.TEMP_PREFIX + "*"))


def test_dire_synthetise_joue_puis_supprime_l_audio(camp, voix_on, monkeypatch, engine_calls, tmp_path):
    monkeypatch.setattr(voix, "_run", voix_on)
    vc = FakeVC()
    inter = voice_inter(vc)
    run_dire(inter, "Bienvenue, **aventuriers** !")
    assert len(voix_on.cmds) == 1 and engine_calls == ["release", "load"]
    assert len(vc.played) == 1 and vc.disconnected and not commands._speaking
    assert not (camp.path / "dires").exists() and temporaires(tmp_path) == []        # rien n'est conservé
    vc2 = FakeVC()
    run_dire(voice_inter(vc2), "Bienvenue, aventuriers !")                          # redit : resynthétisé (pas de cache)
    assert len(voix_on.cmds) == 2 and len(vc2.played) == 1 and temporaires(tmp_path) == []


def test_dire_refus(camp, voix_on, monkeypatch):
    monkeypatch.setattr(voix, "_run", voix_on)
    for kwargs, texte, attendu in [
            (dict(perm=False), "Salut", "Gérer le serveur"),
            (dict(), "x" * (SET["voix.dire_max"] + 1), "trop long"),
            (dict(), "   **  ", "Rien à dire")]:
        inter = voice_inter(FakeVC(), **kwargs)
        run_dire(inter, texte)
        assert attendu in inter.out[-1]
    inter = voice_inter(FakeVC())
    bot = make_bot()
    bot.sessions[7] = object()
    run_dire(inter, "Salut", bot)
    assert "pendant un enregistrement" in inter.out[-1]
    seul = FakeInter()
    seul.user.voice = None
    run_dire(seul, "Salut")
    assert "canal vocal" in seul.out[-1]
    assert voix_on.cmds == []                                                       # aucune synthèse pour ces refus


def test_dire_desactive_par_defaut(camp):
    inter = voice_inter(FakeVC())
    run_dire(inter, "Salut")
    assert "désactivée" in inter.out[-1]


def test_dire_echec_de_synthese_est_explique(camp, monkeypatch):
    monkeypatch.setitem(SET.values, "voix.activer", True)
    monkeypatch.setattr(voix, "_run", FakeRun(1, b"torch.OutOfMemoryError: CUDA out of memory"))
    vc = FakeVC()
    inter = voice_inter(vc)
    sent = []

    async def edit(content=None, **kw): sent.append(content)
    inter.edit_original_response = edit
    run_dire(inter, "Salut")
    assert any("Mémoire GPU" in (x or "") for x in sent) and vc.played == [] and not commands._speaking


# --- Whisper conservé en mémoire (liberer_whisper = non) ---------------------------------------------------------

def test_par_defaut_whisper_n_est_plus_libere():
    from voicebot.config import SPEC
    assert SPEC["voix.liberer_whisper"][2] is False


def test_whisper_reste_charge_et_la_voix_marche_pendant_un_enregistrement(camp, voix_on, monkeypatch, engine_calls):
    monkeypatch.setitem(SET.values, "voix.liberer_whisper", False)
    monkeypatch.setattr(voix, "_run", voix_on)
    integrated(camp)
    bot = make_bot()
    session_vc = FakeVC()
    bot.sessions[7] = (object(), object(), session_vc)                              # enregistrement en cours
    inter = voice_inter(FakeVC())
    run_action(inter, "audio", bot)                                                 # autorisé : plus de conflit de modèles
    assert "prêt" in inter.out[-1] and len(voix_on.cmds) == 1 and engine_calls == []   # Whisper jamais touché
    run_dire(voice_inter(FakeVC()), "Salut les héros", bot)                         # autorisé aussi, et dit dans le canal de la séance
    assert len(voix_on.cmds) == 2 and len(session_vc.played) == 1 and not session_vc.disconnected
    assert engine_calls == []


def test_integrer_prepare_l_audio_pendant_un_enregistrement_si_whisper_reste_charge(camp, voix_on, monkeypatch):
    monkeypatch.setitem(SET.values, "voix.liberer_whisper", False)
    assert commands.whisper_conflict(types.SimpleNamespace(sessions={7: 1})) is False
    monkeypatch.setitem(SET.values, "voix.liberer_whisper", True)
    assert commands.whisper_conflict(types.SimpleNamespace(sessions={7: 1})) is True
    assert commands.whisper_conflict(types.SimpleNamespace(sessions={})) is False


def test_la_voix_du_bot_n_est_jamais_enregistree_ni_transcrite(tmp_path):
    from voicebot.audio import SessionRecorder
    rec = SessionRecorder("2026-11-02_220000", tmp_path)
    heard = []
    rec.on_chunk = lambda uid, start, pcm: heard.append(uid)
    rec.ignored_ids.add(999)
    pcm = types.SimpleNamespace(pcm=b"\x01\x00" * 3840)                             # 20 ms stéréo 48 kHz
    rec.write(types.SimpleNamespace(id=999, display_name="LeBot"), pcm)
    rec.write(types.SimpleNamespace(id=5, display_name="Joueur"), pcm)
    rec.stop()
    assert heard == [5] and 999 not in rec.tracks and 5 in rec.tracks


def test_record_autorise_pendant_la_synthese_si_whisper_reste_charge(camp, monkeypatch):
    monkeypatch.setitem(SET.values, "voix.liberer_whisper", False)
    monkeypatch.setattr(voix, "is_busy", lambda: True)
    assert commands.whisper_conflict(types.SimpleNamespace(sessions={})) is False
    inter = FakeInter()
    inter.user.voice = None                                                  # pas de canal : refus pour une autre raison
    asyncio.run(make_bot().tree.commands["record"](inter))
    assert "canal vocal" in inter.out[-1] and "en cours de préparation" not in inter.out[-1]


# --- processus permanent (modèle chargé une seule fois) --------------------------------------------------------

FAKE_WORKER = r'''
import json, sys, time, os
log = os.environ["FAKE_LOG"]
open(log, "a").write("charge\n")
time.sleep(float(os.environ.get("FAKE_LOAD_S", "0")))
print("Chargement du faux modele...", flush=True)
print("PRET", flush=True)
for line in sys.stdin:
    argv = json.loads(line)["argv"]
    if "--die" in argv:
        os._exit(7)
    if "--hang" in argv:
        time.sleep(60)
    if "--fail" in argv:
        print("ERREUR RuntimeError: CUDA out of memory", flush=True)
        print("TERMINE 1", flush=True)
        continue
    out = argv[argv.index("--out") + 1]
    open(out, "wb").write(b"RIFFfake")
    print("MORCEAU 1/1", flush=True)
    print("FAIT 1/1 duree=1.0s attendu=1.0s", flush=True)
    print("TERMINE 0", flush=True)
'''


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    log = tmp_path / "loads.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    server = voix._Server()
    monkeypatch.setattr(voix._Server, "_command", lambda self: [sys.executable, str(script)])
    monkeypatch.setattr(voix, "SERVER", server)
    yield types.SimpleNamespace(server=server, log=log, tmp=tmp_path)
    server.stop()


def loads(fake): return fake.log.read_text().count("charge") if fake.log.exists() else 0


def test_le_modele_est_charge_une_seule_fois_pour_plusieurs_syntheses(fake_server):
    server = fake_server.server
    server.start()                                                                 # comme au démarrage du bot
    seen = []
    for i in range(3):
        out = fake_server.tmp / f"{i}.wav"
        done = server.request(["--in", "x.txt", "--out", str(out)], 30, seen.append)
        assert done.returncode == 0 and out.read_bytes() == b"RIFFfake" and b"FAIT 1/1" in done.stdout
    assert loads(fake_server) == 1 and server.state == "prêt"
    assert seen.count("MORCEAU 1/1") == 3


def test_une_demande_pendant_le_chargement_attend_et_le_signale(fake_server, monkeypatch):
    monkeypatch.setenv("FAKE_LOAD_S", "1.0")
    seen = []
    out = fake_server.tmp / "a.wav"
    done = fake_server.server.request(["--out", str(out)], 30, seen.append)
    assert done.returncode == 0 and any(m.startswith("Chargement du modèle") for m in seen)


def test_une_erreur_n_arrete_pas_le_processus(fake_server):
    server = fake_server.server
    bad = server.request(["--fail"], 30)
    assert bad.returncode == 1 and b"out of memory" in bad.stderr
    ok = server.request(["--out", str(fake_server.tmp / "b.wav")], 30)
    assert ok.returncode == 0 and loads(fake_server) == 1                           # même processus, rien rechargé


def test_un_processus_mort_est_signale_puis_relance(fake_server):
    server = fake_server.server
    server.request(["--out", str(fake_server.tmp / "c.wav")], 30)
    dead = server.request(["--die"], 30)
    assert dead.returncode == 1
    again = server.request(["--out", str(fake_server.tmp / "d.wav")], 30)
    assert again.returncode == 0 and loads(fake_server) == 2                        # relancé automatiquement


def test_delai_depasse_tue_le_processus(fake_server):
    with pytest.raises(subprocess.TimeoutExpired):
        fake_server.server.request(["--hang"], 1)
    assert not fake_server.server.running()


def test_ensure_audio_passe_par_le_processus_permanent_quand_whisper_reste_charge(camp, fake_server, monkeypatch):
    monkeypatch.setattr(voix, "use_server", lambda: True)
    monkeypatch.setitem(SET.values, "voix.vitesse", 1.0)
    out = integrated(camp)
    voix.ensure_audio(camp, SID, out["precedemment"])
    voix.ensure_audio(camp, SID, out["precedemment"], force=True)
    assert loads(fake_server) == 1 and fake_server.server.running()                  # deux synthèses, un seul chargement
    monkeypatch.setattr(voix, "use_server", lambda: False)                          # Whisper libéré : on rend la mémoire
    monkeypatch.setattr(voix, "_run", FakeRun())
    voix.ensure_audio(camp, SID, out["precedemment"], force=True)
    assert not fake_server.server.running()


def test_precharge_au_demarrage_seulement_si_la_voix_est_active_et_whisper_conserve(fake_server, monkeypatch):
    started = []
    monkeypatch.setattr(voix.SERVER, "start", lambda: started.append(1))
    monkeypatch.setattr(voix, "use_server", lambda: True)
    monkeypatch.setitem(SET.values, "voix.activer", False)
    assert voix.preload() is False and started == []
    monkeypatch.setitem(SET.values, "voix.activer", True)
    assert voix.preload() is True and started == [1]
    monkeypatch.setattr(voix, "use_server", lambda: False)                          # libérer_whisper = oui : pas de préchargement
    assert voix.preload() is False and started == [1]


def test_le_mode_permanent_n_est_utilise_qu_avec_omnivoice_et_whisper_conserve(monkeypatch):
    monkeypatch.undo()
    for engine_name, free, expected in [("omnivoice", False, True), ("omnivoice", True, False), ("chatterbox", False, False)]:
        monkeypatch.setitem(SET.values, "voix.moteur", engine_name)
        monkeypatch.setitem(SET.values, "voix.liberer_whisper", free)
        assert voix.use_server() is expected


def test_boucle_du_worker_omnivoice(monkeypatch, capsys):
    import io
    calls = []

    def fake_run(model, accepted, args):
        calls.append(args)
        if args.src == "boom":
            raise RuntimeError("CUDA out of memory\navec details")
        return 0
    monkeypatch.setattr(tts_omnivoice, "run_request", fake_run)
    requests = "\n".join(json.dumps({"argv": a}) for a in (["--in", "a", "--out", "b"], ["--in", "boom", "--out", "c"],
                                                          ["--vitesse-inconnue"], ["--in", "d", "--out", "e", "--steps", "16"]))
    tts_omnivoice.serve(object(), None, io.StringIO(requests + "\n"))
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "PRET" and out.count("TERMINE 0") == 2 and out.count("TERMINE 1") == 1 and out.count("TERMINE 2") == 1
    assert any(line.startswith("ERREUR RuntimeError: CUDA out of memory avec details") for line in out)
    assert [c.src for c in calls] == ["a", "boom", "d"] and calls[-1].steps == 16        # le processus survit aux erreurs


# --- plusieurs voix -------------------------------------------------------------------------------------------

@pytest.fixture
def voix_dir(tmp_path, monkeypatch):
    d = tmp_path / "lesvoix"
    d.mkdir()
    for name in ("narrateur", "Sorciere"):
        (d / f"voix.{name}.wav").write_bytes(b"RIFF" + name.encode())
    (d / "voix.narrateur.txt").write_text("Bonjour à tous.", encoding="utf-8")
    (d / "voix.wav").write_bytes(b"x")                      # sans nom : ignorée
    (d / "autre.wav").write_bytes(b"x")
    monkeypatch.setitem(SET.values, "voix.dossier", str(d))
    return d


def run_voix(inter, nom=None, bot=None):
    run_action(inter, "voix", bot=bot, voix_nom=nom)


def test_liste_des_voix_du_dossier_commun(voix_dir):
    assert voix.list_voices() == ["narrateur", "Sorciere"]
    assert voix.voice_path("sorciere").name == "voix.Sorciere.wav"                   # insensible à la casse
    with pytest.raises(voix.VoixError, match="Disponibles : narrateur, Sorciere"):
        voix.voice_path("inconnue")
    assert voix.reference_text(voix.voice_path("narrateur")) == "Bonjour à tous."


def test_ordre_de_priorite_des_voix(camp, voix_dir, monkeypatch):
    general = voix_dir.parent / "general.wav"
    general.write_bytes(b"g")
    monkeypatch.setitem(SET.values, "voix.voix_reference", str(general))
    assert voix.voice_reference(camp) == general                                    # ancien réglage
    (camp.path / "voix.wav").write_bytes(b"c")
    assert voix.voice_reference(camp) == camp.path / "voix.wav"                     # ancien voix.wav de la campagne
    from voicebot import campagne
    campagne.set_voix(camp, "narrateur")
    assert voix.voice_reference(camp).name == "voix.narrateur.wav"                  # voix choisie pour la campagne
    assert voix.voice_reference(camp, "Sorciere").name == "voix.Sorciere.wav"        # paramètre explicite
    assert voix.voice_reference(camp, "défaut") is None
    campagne.set_voix(camp, "défaut")
    assert voix.voice_reference(camp) is None
    campagne.set_voix(camp, "disparue")                                             # fichier supprimé : repli, sans planter
    assert voix.voice_reference(camp) == camp.path / "voix.wav"
    assert json.loads((camp.path / "campagne.json").read_text(encoding="utf-8"))["voix"] == "disparue"


def test_commande_voix_liste_puis_change(camp, voix_dir):
    inter = FakeInter()
    run_voix(inter)
    text = inter.out[-1]
    assert "`narrateur`" in text and "`Sorciere`" in text and "`défaut`" in text and "voix.wav" not in text.replace("voix.<nom>.wav", "")
    inter = FakeInter()
    run_voix(inter, "sorciere")
    from voicebot.campagne import Campaign
    assert "✅" in inter.out[-1] and voix.campaign_voice(Campaign(camp.path)) == "Sorciere"          # nom normalisé comme le fichier
    inter = FakeInter()
    run_voix(inter)
    assert "Sorciere` ← campagne" in inter.out[-1]


def test_commande_voix_refus(camp, voix_dir):
    from voicebot.campagne import Campaign
    inter = FakeInter(perm=False)
    run_voix(inter, "narrateur")
    assert "Gérer le serveur" in inter.out[-1] and voix.campaign_voice(Campaign(camp.path)) == ""
    inter = FakeInter()
    run_voix(inter, "inconnue")
    assert "❌" in inter.out[-1] and "narrateur, Sorciere" in inter.out[-1]
    assert [c.value for c in commands.voice_choices("sor")] == ["Sorciere"]
    assert [c.value for c in commands.voice_choices("")][0] == "défaut"


def test_changer_de_voix_change_la_cle_du_cache(camp, voix_dir):
    text = integrated(camp)["precedemment"]
    spoken = voix.clean_for_speech(text)
    keys = {voix.cache_key(spoken, voix.voice_reference(camp, v)) for v in (None, "narrateur", "Sorciere")}
    assert len(keys) == 3


def test_audio_avec_une_autre_voix_utilise_son_echantillon(camp, voix_on, voix_dir, monkeypatch):
    monkeypatch.setattr(voix, "_run", voix_on)
    out = integrated(camp)
    voix.ensure_audio(camp, SID, out["precedemment"], voice="narrateur")
    cmd = voix_on.cmds[-1]
    assert cmd[cmd.index("--voice") + 1].endswith("voix.narrateur.wav") and cmd[cmd.index("--voice-text") + 1] == "Bonjour à tous."
    assert voix.cached_audio(camp, SID, out["precedemment"], "narrateur") is not None
    assert voix.cached_audio(camp, SID, out["precedemment"]) is None                  # autre voix que celle de la campagne
    voix.ensure_audio(camp, SID, out["precedemment"], voice="narrateur")              # déjà prêt : pas de resynthèse
    assert len(voix_on.cmds) == 1


def test_dire_avec_voix_et_voix_inconnue(camp, voix_on, voix_dir, monkeypatch):
    monkeypatch.setattr(voix, "_run", voix_on)
    inter = voice_inter(FakeVC())
    asyncio.run(make_bot().tree.commands["dire"](inter, "Salut", "Sorciere"))
    cmd = voix_on.cmds[-1]
    assert cmd[cmd.index("--voice") + 1].endswith("voix.Sorciere.wav")
    inter = voice_inter(FakeVC())
    asyncio.run(make_bot().tree.commands["dire"](inter, "Salut", "inconnue"))
    assert "❌" in inter.out[-1] and "Disponibles" in inter.out[-1] and len(voix_on.cmds) == 1


def test_campagne_audio_avec_voix_inconnue(camp, voix_on, voix_dir):
    integrated(camp)
    inter = FakeInter()
    run_action(inter, "audio", voix_nom="inconnue")
    assert "❌" in inter.out[-1] and "narrateur" in inter.out[-1]


# --- /regle ... dicter ----------------------------------------------------------------------------------------

def run_regle(inter, bot=None, **kw):
    asyncio.run((bot or make_bot()).tree.commands["regle"](inter, **kw))


@pytest.fixture
def gemini(monkeypatch):
    asked = []
    monkeypatch.setattr(commands, "api_key", lambda: "k")
    monkeypatch.setitem(SET.values, "regles.delai_min_s", 0)
    monkeypatch.setattr(commands, "explain_question", lambda q, game: (asked.append((q, game)),
                        "Premier point. Deuxième point **important**. " + "Mot " * 300)[1])
    return asked


def test_texte_coupe_a_la_fin_de_phrase():
    text, cut = voix.truncate_for_speech("Une phrase. Une autre phrase. Et une troisième qui dépasse", 30)
    assert (text, cut) == ("Une phrase. Une autre phrase.", True)
    assert voix.truncate_for_speech("Court.", 30) == ("Court.", False)
    text, cut = voix.truncate_for_speech("mot " * 50, 30)
    assert cut and len(text) <= 30 and text.endswith("mot")


def test_regle_dicte_la_reponse_avec_la_voix_demandee(camp, voix_on, voix_dir, gemini, monkeypatch):
    monkeypatch.setattr(voix, "_run", voix_on)
    monkeypatch.setitem(SET.values, "voix.dire_max", 80)
    vc = FakeVC()
    inter = voice_inter(vc)
    inter.follow = []
    run_regle(inter, question="Une boule de feu ?", jeu="D&D 5e", dicter=True, voix_nom="Sorciere")
    assert gemini == [("Une boule de feu ?", "D&D 5e")]
    cmd = voix_on.cmds[-1]
    assert cmd[cmd.index("--voice") + 1].endswith("voix.Sorciere.wav")
    assert not (camp.path / "dires").exists()
    assert len(vc.played) == 1 and vc.disconnected and not commands._speaking


def test_regle_sans_dicter_ne_synthetise_pas(camp, voix_on, gemini, monkeypatch):
    monkeypatch.setattr(voix, "_run", voix_on)
    run_regle(voice_inter(FakeVC()), question="Une question")
    assert voix_on.cmds == []


def test_regle_dicter_refus_avant_d_appeler_gemini(camp, voix_on, voix_dir, gemini, monkeypatch):
    inter = voice_inter(FakeVC())
    run_regle(inter, question="Q", dicter=True, voix_nom="inconnue")
    assert "Disponibles" in inter.out[-1] and gemini == []
    monkeypatch.setitem(SET.values, "voix.activer", False)
    inter = voice_inter(FakeVC())
    run_regle(inter, question="Q", dicter=True)
    assert "désactivée" in inter.out[-1] and gemini == []
    monkeypatch.setitem(SET.values, "voix.activer", True)
    inter = voice_inter(FakeVC())
    inter.user.voice = None
    run_regle(inter, question="Q", dicter=True)
    assert "canal vocal" in inter.out[-1] and gemini == []


def test_dicter_avec_voix_genere_a_la_volee_sans_toucher_au_precedemment_prepare(camp, voix_on, voix_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(voix, "_run", voix_on)
    out = integrated(camp)
    voix.ensure_audio(camp, SID, out["precedemment"])                               # audio préparé (voix de la campagne)
    wav = camp.path / "seances" / SID / "precedemment.wav"
    before, meta_before = wav.read_bytes(), wav.with_suffix(".voix.json").read_text(encoding="utf-8")
    voix_on.cmds.clear()
    vc = FakeVC()
    inter = voice_inter(vc)
    run_action(inter, "dicter", voix_nom="Sorciere")
    cmd = voix_on.cmds[-1]
    assert cmd[cmd.index("--voice") + 1].endswith("voix.Sorciere.wav") and len(vc.played) == 1
    assert wav.read_bytes() == before and wav.with_suffix(".voix.json").read_text(encoding="utf-8") == meta_before
    assert temporaires(tmp_path) == [] and not (camp.path / "dires").exists()
    voix_on.cmds.clear()
    run_action(voice_inter(FakeVC()), "dicter")                                      # sans voix : lit l'audio préparé
    assert voix_on.cmds == []


def test_dicter_avec_voix_inconnue_ou_sans_audio_prepare(camp, voix_on, voix_dir, monkeypatch):
    monkeypatch.setattr(voix, "_run", voix_on)
    integrated(camp)
    inter = voice_inter(FakeVC())
    run_action(inter, "dicter", voix_nom="inconnue")
    assert "Disponibles" in inter.out[-1] and voix_on.cmds == []
    vc = FakeVC()
    run_action(voice_inter(vc), "dicter", voix_nom="narrateur")                       # pas d'audio préparé : ça marche quand même
    assert len(vc.played) == 1


def test_audio_temporaire_supprime_meme_en_cas_d_echec_et_purge_au_demarrage(camp, voix_on, monkeypatch, tmp_path):
    monkeypatch.setattr(voix, "_run", FakeRun(code=1, stderr=b"boom"))
    with pytest.raises(voix.VoixError):
        voix.ensure_free_audio(camp, "Un texte.")
    assert temporaires(tmp_path) == []
    stale = tmp_path / "tmp_systeme" / (voix.TEMP_PREFIX + "oublie")
    stale.mkdir()
    (stale / "dire.wav").write_bytes(b"x")
    other = tmp_path / "tmp_systeme" / "autre_chose"
    other.mkdir()
    assert voix.purge_temp() == 1 and not stale.exists() and other.exists()
    voix.discard_free(other / "x.wav")                                                # fichier étranger : jamais supprimé
    assert other.exists()
