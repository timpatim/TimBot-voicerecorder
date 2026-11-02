"""Voix du bot : nettoyage du texte, cache de l'audio, découpage, installation et commandes (Chatterbox simulé)."""
import asyncio
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
import fix_voix_torch  # noqa: E402


def integrated(camp):
    pack.build_packs(camp, ROWS[:3], SID)
    return pack.integrate(camp, REPONSE)


class FakeRun:
    """Remplace le lancement du processus Chatterbox : écrit un WAV bidon, ou échoue."""
    def __init__(self, code=0, stderr=b"", wav_bytes=b"RIFFfake"):
        self.code, self.stderr, self.cmds, self.wav_bytes = code, stderr, [], wav_bytes

    def __call__(self, cmd, timeout):
        self.cmds.append(cmd)
        if self.code == 0:
            Path(cmd[cmd.index("--out") + 1]).write_bytes(self.wav_bytes)
        return subprocess.CompletedProcess(cmd, self.code, b"", self.stderr)


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
    (b"ModuleNotFoundError: No module named 'chatterbox'", "install_voix.bat"),
    (b"torch.OutOfMemoryError: CUDA out of memory", "Mémoire GPU"),
    (b"boom\nautre erreur", "autre erreur")])
def test_erreurs_de_synthese_lisibles(camp, monkeypatch, stderr, expected):
    out = integrated(camp)
    monkeypatch.setattr(voix, "_run", FakeRun(1, stderr))
    with pytest.raises(voix.VoixError, match=expected):
        voix.ensure_audio(camp, SID, out["precedemment"])


def test_delai_depasse(camp, monkeypatch):
    out = integrated(camp)
    def slow(cmd, timeout): raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(voix, "_run", slow)
    with pytest.raises(voix.VoixError, match="dépassé"):
        voix.ensure_audio(camp, SID, out["precedemment"])


def test_python_de_la_voix(monkeypatch, tmp_path):
    monkeypatch.setattr(voix, "PYTHON_FILE", tmp_path / "absent")
    assert voix.python_exe() == sys.executable
    (tmp_path / "p").write_text("C:\\envs\\voix\\python.exe\n", encoding="utf-8")
    monkeypatch.setattr(voix, "PYTHON_FILE", tmp_path / "p")
    assert voix.python_exe() == "C:\\envs\\voix\\python.exe"
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
    def reentrant(cmd, timeout):
        assert voix.is_busy()
        with pytest.raises(voix.VoixError, match="déjà en cours"):
            voix.ensure_audio(camp, "autre-seance", "Un autre texte.")
        return FakeRun()(cmd, timeout)
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


def test_record_refuse_pendant_la_synthese(camp, monkeypatch):
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
