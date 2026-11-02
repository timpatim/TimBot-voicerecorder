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
