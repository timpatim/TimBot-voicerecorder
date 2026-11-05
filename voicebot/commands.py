"""Commandes slash du bot : /record, /stop, /transcript, /regle, /stats, /status, /retranscrire, /config."""
import asyncio
import io
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import voice_recv

from .audio import SessionRecorder
from . import campagne as camp
from . import claude_cli, memoire, pack as packs, presse_papiers, retranscrire as retrans, voix
from .transcript import read_rows as _read_rows
from .campagne import CampaignError, CampaignStore, generate_contexte
from .config import CONFIG_SPEC, SET, load_config, parse_config_value, save_config
from .constants import RATE
from .live import LiveTranscriber
from .pipeline import PIPELINE_STEPS, StatusBoard, run_pipeline
from .gemini import GeminiError, api_key as gemini_api_key
from .rules import RulesError, api_key, explain_question, explain_rule, transcript_lines
from .stats import render_sections, session_stats
from .transcript import read_rows
from .utils import fmt_ts


def stats_embed(stats: dict, title: str, pending_s: float = 0.0) -> discord.Embed:
    embed = discord.Embed(title=title, colour=discord.Colour.blurple())
    for name, body, code in render_sections(stats, esc=discord.utils.escape_markdown):
        value = f"```\n{body}\n```" if code else body
        if len(value) > 1024:
            value = value[:1020] + ("\n```" if code else "…")
        embed.add_field(name=name, value=value, inline=False)
    if pending_s >= 1:
        embed.set_footer(text=f"ℹ️ {pending_s:.0f} s d'audio encore en attente de transcription : stats partielles")
    return embed


_warned_channels: set = set()


def out_channel(inter: discord.Interaction):
    """Salon des messages SYSTÈME et informatifs (discord.canal_messages : suivi de fin de session, alertes, trace des
    commandes lancées, documents de mémoire de campagne), ou None pour les envoyer là où la commande est lancée.
    Les réponses aux demandes des utilisateurs (/regle, /transcript, /stats, /campagne afficher...) n'y vont PAS.

    Retombe sur None (avec un avertissement unique en console) si le salon est introuvable ou inaccessible au bot.
    """
    raw = SET["discord.canal_messages"].strip()
    guild = getattr(inter, "guild", None)
    if not raw or guild is None:
        return None
    channel = guild.get_channel(int(raw))
    problem = None
    if channel is None or not hasattr(channel, "send"):
        problem = "introuvable (identifiant erroné, ou salon d'un autre serveur ?)"
    elif channel.id == getattr(inter, "channel_id", None):
        return None                              # on est déjà dans le bon salon
    else:
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            problem = "inaccessible : le bot n'a pas le droit d'y voir le salon ni d'y écrire"
    if problem:
        if raw not in _warned_channels:
            _warned_channels.add(raw)
            print(f"[bot] salon des messages {raw} {problem} : réponses dans le salon d'origine.")
        return None
    return channel


async def reply(inter: discord.Interaction, channel, **kwargs):
    """Publie `kwargs` dans `channel` (salon dédié) ou, sans salon dédié / en cas d'échec, là où la commande a été lancée.

    Dans le cas d'un salon dédié, la personne reçoit une simple confirmation que seule elle voit.
    """
    if channel is not None:
        try:
            await channel.send(**kwargs)
        except Exception as e:
            print(f"[bot] envoi dans {channel!r} impossible ({e!r}), repli sur le salon d'origine")
        else:
            note = f"✅ Réponse publiée dans {channel.mention}."
            if inter.response.is_done():
                await inter.followup.send(note, ephemeral=True)
            else:
                await inter.response.send_message(note, ephemeral=True)
            return
    if inter.response.is_done():
        await inter.followup.send(**kwargs)
    else:
        await inter.response.send_message(**kwargs)


def command_line(inter) -> str:
    """Commande telle que l'utilisateur l'a tapée (sans le contenu des fichiers joints), pour la trace."""
    cmd = getattr(inter, "command", None)
    name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", "?")
    parts = []
    try:
        items = list(getattr(inter, "namespace", None) or [])
    except TypeError:
        items = []
    for key, v in items:
        if v is None:
            continue
        if hasattr(v, "filename"):
            text = v.filename
        elif hasattr(v, "display_name"):
            text = v.display_name
        elif hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
            text = str(v.value)
        else:
            text = str(v)
        parts.append(f"{key}:{text.replace(chr(10), ' ')[:80]}")
    return f"/{name} {' '.join(parts)}".strip()[:300]


async def trace_command(inter) -> None:
    """Une ligne dans le salon des messages système chaque fois qu'un utilisateur lance une commande
    (rien si ce salon n'est pas configuré ou si la commande est tapée dans ce salon)."""
    channel = out_channel(inter)
    if channel is None:
        return
    try:
        await channel.send(content=f"▶️ **{inter.user.display_name}** : `{command_line(inter)}`",
                           allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[bot] trace de la commande impossible ({e!r})")


async def _say(inter: discord.Interaction, channel=None, **kwargs):
    """Message dans le salon (pas d'expiration) ; si le bot ne peut pas y écrire, repli sur la réponse de l'interaction."""
    try:
        return await (channel or inter.channel).send(**kwargs)
    except Exception as e:
        print(f"[bot] envoi dans le salon impossible ({e!r}), repli sur la réponse de la commande")
        return await inter.followup.send(wait=True, **kwargs)


def campaign_store() -> CampaignStore:
    return CampaignStore(Path(SET["campagnes.dossier"]))


def campaign_game() -> str:
    """Jeu de la campagne en cours (vide s'il n'y en a pas ou si elle n'en précise pas)."""
    try:
        c = campaign_store().active()
    except CampaignError:
        return ""
    return (c.jeu or "").strip() if c else ""


def _member_info(m) -> dict:
    return {"id": m.id, "name": getattr(m, "display_name", None), "username": getattr(m, "name", None)}


def pack_message(made: dict) -> dict:
    """Message + fichiers des packs ; la fin attendue de chaque partie sert à contrôler la lecture du modèle."""
    parts = made["parties"]
    order = "dans l'ordre, dans la MÊME conversation, " if len(parts) > 1 else ""
    lines = [f"📦 **Packs pour {made['titre']}** — à coller {order}dans Gemini ou Claude (copie le contenu du fichier). "
             "Chaque réponse doit commencer par `=== FIN LUE === hh:mm:ss` :"]
    lines += [f"• `{p['fichier']}` — {p['debut']} → fin attendue **{p['fin_attendue']}**, dernière réplique « {p['derniere'][:50]} »"
              for p in parts]
    lines.append("Relis la réponse de la dernière partie, copie-la (presse-papiers) puis `/campagne integrer`, "
                 "ou enregistre-la dans un fichier texte et `/campagne integrer fichier:…`.")
    return {"content": "\n".join(lines)[:1990], "files": [discord.File(f) for f in made["fichiers"][:10]]}


_speaking: set = set()          # serveurs où le bot est en train de parler
_tasks: set = set()             # références aux tâches de fond (sinon elles peuvent être ramassées)


def background(coro):
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _traced_source(path: Path):
    """FFmpegPCMAudio qui compte les trames lues et leur niveau crête, pour diagnostiquer un « pas de son »."""
    base = discord.FFmpegPCMAudio(str(path))
    if not hasattr(base, "read"):
        return base                                     # (tests : source factice)
    read = base.read
    base.frames, base.peak = 0, 0

    def traced():
        data = read()
        if data:
            base.frames += 1
            try:
                import array
                a = array.array("h", data)
                base.peak = max(base.peak, max(a), -min(a))
            except Exception:
                pass
        return data
    base.read = traced
    return base


PLAY_TAIL_S = 1.5       # attente après la lecture, avant de quitter le canal : évite de couper la fin


async def play_wav(vc, path: Path):
    """Joue un fichier audio dans le canal vocal et attend la fin de la lecture (ffmpeg requis)."""
    loop, done = asyncio.get_running_loop(), asyncio.Event()

    def after(error):
        if error:
            print(f"[bot] lecture audio : erreur {error!r}")
        loop.call_soon_threadsafe(done.set)
    source = _traced_source(path)
    conn = getattr(vc, "_connection", None)
    print(f"[bot] voix : lecture de {path.name} ({path.stat().st_size if path.exists() else -1} octets), client={type(vc).__name__}, "
          f"connecté={vc.is_connected()}, mode={getattr(conn, 'mode', None)}, "
          f"dave={getattr(conn, 'dave_session', None) is not None}")
    vc.play(source, after=after)
    await done.wait()
    await asyncio.sleep(PLAY_TAIL_S)         # `after` part quand ffmpeg a fini, pas quand Discord a tout diffusé
    # 50 trames de 20 ms par seconde ; crête sur 32767 (16 bits)
    if hasattr(source, "frames"):
        print(f"[bot] voix : lecture terminée, {source.frames} trames ({source.frames / 50:.1f} s), "
              f"niveau crête {source.peak}/32767")


def audio_unavailable(campaign, sid: str, state: dict) -> str:
    """Pourquoi l'audio ne peut pas être lu, et quoi faire (la séance visée est toujours nommée)."""
    if state["etat"] == "texte":
        return (f"Audio du « précédemment » non prêt pour la séance {sid} : le texte a changé depuis la préparation de "
                "l'audio. Refais-le avec `/campagne audio force` (hors enregistrement).")
    others = [x for x in voix.sessions_with_audio(campaign) if x != sid]
    more = f" Audio disponible pour : {', '.join(others[-5:])} (paramètre `session`)." if others else ""
    return (f"Audio du « précédemment » non prêt pour la séance {sid} (c'est la dernière séance qui a un « précédemment »)."
            f"{more} Prépare-le avec `/campagne audio` (hors enregistrement) ; il l'est automatiquement après "
            "`/campagne integrer`.")


async def speak_recap(bot, guild_id, campaign, vc, channel):
    """Au démarrage d'un enregistrement : lit l'audio préparé du « précédemment » (jamais de synthèse ici)."""
    try:
        sid, text = memoire.seance_text(campaign, "precedemment.md")
    except CampaignError:
        return                                          # pas encore de « précédemment » : rien à dire
    wav, state = voix.playable_audio(campaign, sid, text)
    if wav is None:
        try:
            await channel.send(f"🔇 {audio_unavailable(campaign, sid, state)}")
        except Exception:
            pass
        return
    if state["etat"] == "reglages":
        print(f"[bot] voix : audio préparé avec d'autres réglages ({state['detail']}), lu tel quel")
    if guild_id in _speaking:
        return
    _speaking.add(guild_id)
    try:
        await play_wav(vc, wav)
    except Exception as e:
        print(f"[bot] voix : lecture impossible {e!r}")
    finally:
        _speaking.discard(guild_id)


_preparing = {"on": False}      # vrai pendant toute la préparation d'un audio (Whisper libéré, moteur vocal chargé)


def whisper_conflict(bot) -> bool:
    """Vrai quand une synthèse vocale couperait la transcription en cours : Whisper serait libéré pendant l'enregistrement."""
    return bool(SET["voix.liberer_whisper"] and bot.sessions)


def audio_busy() -> bool:
    """Vrai quand le GPU est pris par une synthèse vocale ou par une retranscription (WhisperX ne doit pas être libéré)."""
    return _preparing["on"] or voix.is_busy() or bool(_RETRANSCRIBING)


def gpu_report(moment: str) -> None:
    """Affiche la mémoire GPU utilisée (bot et autres processus), pour juger si Whisper et la voix tiennent ensemble."""
    try:
        import torch
        if not torch.cuda.is_available():
            return
        free, total = torch.cuda.mem_get_info()
        print(f"[bot] voix : mémoire GPU {moment} la synthèse : {(total - free) / 2**30:.1f} / {total / 2**30:.1f} Go utilisés")
    except Exception:
        pass


def voice_error(voice) -> str | None:
    """Message d'erreur (avec la liste des voix) si la voix demandée n'existe pas, sinon None."""
    try:
        voix.voice_reference(None, (voice or "").strip() or None)
    except voix.VoixError as e:
        return str(e)
    return None


def voice_choices(current: str) -> list:
    """Voix proposées par l'autocomplétion (25 au plus) : le nom « défaut » puis les fichiers du dossier des voix."""
    needle = (current or "").lower()
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in ["défaut", *voix.list_voices()]
            if needle in n.lower()][:25]


def dictation_problem(bot, inter, voix_nom) -> str | None:
    """Pourquoi la réponse ne pourrait pas être dictée (vérifié avant d'appeler Gemini), sinon None."""
    if not SET["voix.activer"]:
        return "La voix est désactivée (`activer = non` dans [voix] de config.ini)."
    if whisper_conflict(bot):
        return ("Impossible de dicter pendant un enregistrement : la synthèse libère Whisper "
                "(réglage `liberer_whisper` de [voix]).")
    voice = getattr(inter.user, "voice", None)
    if bot.sessions.get(inter.guild_id) is None and not (voice and voice.channel):
        return "Rejoins d'abord un canal vocal pour que je dicte la réponse."
    if inter.guild_id in _speaking or audio_busy():
        return "Je suis déjà en train de parler ou de préparer une voix."
    return voice_error(voix_nom)


async def speak_answer(bot, engine, inter, answer: str, voix_nom) -> None:
    """Dicte `answer` dans le canal vocal (celui de l'enregistrement, sinon celui de la personne). Les erreurs sont
    signalées en message privé : la réponse écrite a déjà été publiée."""
    try:
        campaign = campaign_store().active()
    except CampaignError:
        campaign = None
    spoken, cut = voix.truncate_for_speech(voix.clean_for_speech(answer), SET["voix.dire_max"])
    if not spoken:
        return
    voice = getattr(inter.user, "voice", None)
    active = bot.sessions.get(inter.guild_id)
    _speaking.add(inter.guild_id)
    temporary = wav = None
    try:
        try:
            wav = await prepare_free_audio(engine, campaign, spoken, _console_progress, voix_nom or None)
        except voix.VoixError as e:
            await inter.followup.send(f"❌ Dictée impossible : {e}", ephemeral=True)
            return
        if cut:
            await inter.followup.send(f"ℹ️ Réponse dictée en partie seulement ({SET['voix.dire_max']} caractères au plus, "
                                      "réglage `dire_max` de [voix]).", ephemeral=True)
        try:
            vc = active[2] if active else (temporary := await voice.channel.connect())
            await play_wav(vc, wav)
        except Exception as e:
            print(f"[bot] voix : lecture impossible {e!r}")
            await inter.followup.send(f"❌ Lecture impossible : {e!r}", ephemeral=True)
    finally:
        voix.discard_free(wav)                           # l'audio n'est jamais conservé
        _speaking.discard(inter.guild_id)
        if temporary is not None:
            try:
                await temporary.disconnect()
            except Exception:
                pass


async def prepare_audio(engine, campaign, session_id, text, force: bool = False, progress=None, voice=None) -> Path:
    """Prépare l'audio du « précédemment » sans jamais avoir Whisper et le moteur vocal chargés ensemble :
    Whisper est libéré de la mémoire, le moteur vocal synthétise, puis Whisper est rechargé.
    À n'appeler que hors enregistrement (les appelants le vérifient)."""
    ready = None if force else voix.cached_audio(campaign, session_id, text, voice)
    if ready is not None:
        return ready
    if audio_busy():
        raise voix.VoixError("Une synthèse vocale est déjà en cours.")
    _preparing["on"] = True
    release = bool(SET["voix.liberer_whisper"])
    try:
        if release:
            if progress:
                progress("Libération de Whisper", 0, 0)
            await asyncio.to_thread(engine.release)
        gpu_report("avant")
        return await asyncio.to_thread(voix.ensure_audio, campaign, session_id, text, force, progress, voice)
    finally:
        gpu_report("après")
        if release:
            engine.start_loading()                       # recharge Whisper dans tous les cas
        _preparing["on"] = False


BAR_WIDTH = 12


def progress_text(stage: str, done: int, total: int, title: str = "") -> str:
    """Texte d'avancement : « ▰▰▰▱▱ 3/8 morceaux » ou, sans total connu, l'étape seule."""
    head = f"🎙️ {title}\n" if title else ""
    if total <= 0:
        return f"{head}⏳ {stage}…"
    filled = round(BAR_WIDTH * done / total)
    pct = round(100 * done / total)
    return f"{head}`{'▰' * filled}{'▱' * (BAR_WIDTH - filled)}` {pct} % — {stage.lower()} : {done}/{total}"


async def finish_reply(inter, text: str):
    """Remplace le message d'avancement par le résultat ; à défaut, envoie un nouveau message."""
    try:
        await inter.edit_original_response(content=text)
    except Exception:
        await inter.followup.send(text, ephemeral=True)


def make_progress(inter, loop, title: str, interval_s: float = 2.0):
    """Callback de progression (appelable depuis un thread) qui met à jour la réponse de la commande.
    Les mises à jour sont espacées (limites de débit de Discord) et une erreur d'envoi n'interrompt jamais la synthèse."""
    state = {"last": 0.0, "text": ""}

    async def push(text):
        try:
            await inter.edit_original_response(content=text)
        except Exception as e:
            print(f"[bot] voix : progression non affichée ({e!r})")

    def progress(stage, done, total):
        text = progress_text(stage, done, total, title)
        print(f"[bot] voix : {stage} {done}/{total}" if total else f"[bot] voix : {stage}")
        now = time.monotonic()
        if text == state["text"] or (now - state["last"] < interval_s and 0 < done < total):
            return
        state["text"], state["last"] = text, now
        asyncio.run_coroutine_threadsafe(push(text), loop)
    return progress


def _console_progress(stage, done, total):
    print(f"[bot] voix : {stage} {done}/{total}" if total else f"[bot] voix : {stage}")


async def prepare_free_audio(engine, campaign, text, progress=None, voice=None) -> Path:
    """Audio temporaire d'un texte libre (/dire...) : à jouer puis à supprimer avec voix.discard_free. Whisper est libéré
    pendant la synthèse si `liberer_whisper = oui`."""
    if audio_busy():
        raise voix.VoixError("Une synthèse vocale est déjà en cours.")
    _preparing["on"] = True
    release = bool(SET["voix.liberer_whisper"])
    try:
        if release:
            if progress:
                progress("Libération de Whisper", 0, 0)
            await asyncio.to_thread(engine.release)
        gpu_report("avant")
        return await asyncio.to_thread(voix.ensure_free_audio, campaign, text, progress, voice)
    finally:
        gpu_report("après")
        if release:
            engine.start_loading()
        _preparing["on"] = False


async def pregenerate_audio(engine, campaign, session_id, text):
    """Prépare l'audio du « précédemment » en arrière-plan (silencieux en cas d'échec)."""
    try:
        await prepare_audio(engine, campaign, session_id, text, progress=_console_progress)
        print(f"[bot] voix : audio du « précédemment » prêt pour {session_id}")
    except Exception as e:
        print(f"[bot] voix : préparation de l'audio impossible ({e})")


_RESUMING: set = set()          # serveurs où un résumé est en cours (un seul à la fois)
_RETRANSCRIBING: set = set()    # sessions en cours de retranscription (une seule à la fois : un seul GPU)


async def session_rows(inter, fichier, session) -> tuple | None:
    """(identifiant de séance, lignes de transcript) d'un fichier joint ou d'une session enregistrée.
    Si on ne peut pas, répond à l'utilisateur (la commande doit être différée) et retourne None."""
    if fichier is not None:
        rows = memoire.parse_transcript_text((await fichier.read()).decode("utf-8", errors="replace"))
        sid = (session or "").strip()
        if not packs.RE_SESSION.match(sid):
            await inter.followup.send("Avec un fichier joint, indique la date de la séance dans `session` "
                                      "(ex. `2026-10-24` ou `2026-10-24_190000`).", ephemeral=True)
            return None
    else:
        root = Path(SET["sessions.dossier"])
        found = sorted(p.name for p in root.iterdir() if (p / "transcript_live.jsonl").exists()) if root.is_dir() else []
        sid = (session or (found[-1] if found else "")).strip()
        if not sid or not (root / sid / "transcript_live.jsonl").exists():
            await inter.followup.send("Session introuvable : précise `session` ou joins un transcript.", ephemeral=True)
            return None
        rows = await asyncio.to_thread(_read_rows, root / sid / "transcript_live.jsonl")
    if not rows:
        await inter.followup.send("Transcript vide ou format non reconnu.", ephemeral=True)
        return None
    return sid, rows


async def announce_integration(bot, engine, inter, c, out: dict, header: str, send) -> None:
    """Message de fin d'intégration (avertissements, audio du « précédemment » en préparation, compte rendu dans le salon)."""
    audio_note = ""
    if SET["voix.activer"]:
        if whisper_conflict(bot) or audio_busy():
            audio_note = "\n🔇 Audio du « précédemment » non préparé (enregistrement ou synthèse en cours) : `/campagne audio` plus tard."
        else:
            background(pregenerate_audio(engine, c, out["seance"].name, out["precedemment"]))
            audio_note = "\n🎙️ Audio du « précédemment » en préparation."
    note = f"\n⚠️ {out['avertissement']}" if out["avertissement"] else ""
    await send(f"{header}{note}{audio_note}", ephemeral=True)
    await _say(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(), embed=discord.Embed(
        title=f"📜 Compte rendu — {c.titre}"[:256], description=out["compte_rendu"][:4000], colour=discord.Colour.blurple()))


def claude_summary_line(rep) -> str:
    cost = f", ≈ {rep.cout_usd:.2f} $ estimés (rien n'est facturé : compte dans les limites de ton abonnement)" \
        if rep.cout_usd else ""
    return (f"par Claude ({rep.modele}, effort {SET['claude.effort']}) en {rep.duree_s:.0f} s : "
            f"{rep.tokens_entree:,} tokens lus, {rep.tokens_sortie:,} écrits{cost}.".replace(",", " "))


def limit_bytes() -> int:
    return SET["campagnes.taille_max_brut_ko"] * 1024


AFFICHER_CHOIX = [("compte rendu", "compte_rendu"), ("précédemment", "precedemment"), ("résumé global", "resume")]


CAMPAGNE_ACTIONS = ["liste", "nouvelle", "choisir", "info", "contexte", "mj", "journal", "rollback", "resumer", "joueur", "pack", "integrer", "afficher", "dicter", "audio", "audio force", "voix"]


def register(bot, engine, drive):
    """Déclare les commandes slash sur `bot` ; `engine` (Whisper) et `drive` sont injectés."""
    @bot.tree.error
    async def on_app_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
        name = inter.command.name if inter.command else "?"
        print(f"[bot] erreur dans /{name} : {error!r}")
        msg = f"Erreur dans /{name} : `{error!r}`"
        if inter.response.is_done():
            await inter.followup.send(msg)
        else:
            await inter.response.send_message(msg, ephemeral=True)


    @bot.tree.command(name="record", description="Démarre l'enregistrement de ton canal vocal")
    @app_commands.guild_only()
    async def record(inter: discord.Interaction):
        print(f"[bot] /record reçu de {inter.user}")
        member = inter.user
        if not getattr(member, "voice", None) or not member.voice.channel:
            await inter.response.send_message("Rejoins d'abord un canal vocal.", ephemeral=True)
            return
        if inter.guild_id in bot.sessions:
            await inter.response.send_message("Un enregistrement est déjà en cours sur ce serveur.", ephemeral=True)
            return

        if _RETRANSCRIBING:
            await inter.response.send_message(f"Une retranscription est en cours (`{next(iter(_RETRANSCRIBING))}`) : WhisperX est "
                                              "occupé, la transcription en direct serait trop lente. Réessaie quand elle sera finie.",
                                              ephemeral=True)
            return
        if audio_busy() and SET["voix.liberer_whisper"]:
            await inter.response.send_message("La voix est en cours de préparation (synthèse vocale) : réessaie dans un instant, "
                                              "pour ne pas charger deux modèles en même temps.", ephemeral=True)
            return
        await inter.response.defer()
        channel = member.voice.channel
        engine.start_loading()              # sans effet si le modèle est déjà chargé (ou en cours)

        rec = SessionRecorder(datetime.now().strftime("%Y-%m-%d_%H%M%S"), Path(SET["sessions.dossier"]))
        live = LiveTranscriber(engine, rec.dir / "transcript_live.jsonl", rec.now_samples, rec.name_of)
        rec.on_chunk = live.offer
        me = getattr(bot, "user", None)
        if me is not None:
            rec.ignored_ids.add(me.id)      # la voix du bot n'est ni enregistrée ni envoyée à Whisper
        live.start()

        try:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
            vc.listen(rec)
        except Exception as e:
            rec.stop()
            live.stop()
            await inter.followup.send(f"Connexion vocale impossible : `{e!r}`")
            raise
        bot.sessions[inter.guild_id] = (rec, live, vc)

        # Message de consentement : l'audio ET la transcription sont conservés
        text = SET["discord.message_consentement"].replace("{canal}", channel.mention)
        campaign = campaign_store().active()
        if campaign:
            text += f"\n📖 Campagne : **{campaign.titre}**"
            try:
                (rec.dir / "campagne.json").write_text(
                    json.dumps({"campagne": campaign.slug, "titre": campaign.titre, "jeu": campaign.jeu},
                               ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as e:
                print(f"[bot] campagne.json non écrit : {e!r}")
        await inter.followup.send(text)
        if campaign:
            recap = memoire.latest_precedemment(campaign)
            if recap:
                await inter.followup.send(embed=discord.Embed(title="📺 Précédemment…", description=recap[:4000],
                                                              colour=discord.Colour.blurple()),
                                          allowed_mentions=discord.AllowedMentions.none())
            if SET["voix.activer"] and SET["voix.au_demarrage"]:
                background(speak_recap(bot, inter.guild_id, campaign, vc, inter.channel))


    @bot.tree.command(name="dire", description="Le bot dit un texte à voix haute dans ton canal vocal")
    @app_commands.guild_only()
    @app_commands.describe(texte="Texte à dire à voix haute (la longueur maximale est dans config.ini)",
                           voix_nom="Voix à utiliser (la liste est proposée) ; défaut : la voix de la campagne")
    @app_commands.rename(voix_nom="nom_voix")
    async def dire(inter: discord.Interaction, texte: str, voix_nom: Optional[str] = None):
        if not inter.user.guild_permissions.manage_guild:
            await inter.response.send_message("Il faut la permission « Gérer le serveur » pour utiliser /dire.", ephemeral=True)
            return
        if not SET["voix.activer"]:
            await inter.response.send_message("La voix est désactivée (`activer = non` dans [voix] de config.ini).", ephemeral=True)
            return
        if whisper_conflict(bot):
            await inter.response.send_message(
                "Impossible pendant un enregistrement : la synthèse libère Whisper, donc la transcription s'arrêterait "
                "(réglage `liberer_whisper` de [voix]). `/campagne dicter` lit le « précédemment » déjà préparé.", ephemeral=True)
            return
        wrong = voice_error(voix_nom)
        if wrong:
            await inter.response.send_message(f"❌ {wrong}", ephemeral=True)
            return
        spoken = voix.clean_for_speech(texte)
        if not spoken:
            await inter.response.send_message("Rien à dire.", ephemeral=True)
            return
        if len(spoken) > SET["voix.dire_max"]:
            await inter.response.send_message(f"Texte trop long : {len(spoken)} caractères (maximum {SET['voix.dire_max']}, "
                                              "réglage `dire_max` de [voix]).", ephemeral=True)
            return
        voice = getattr(inter.user, "voice", None)
        active = bot.sessions.get(inter.guild_id)
        if active is None and not (voice and voice.channel):
            await inter.response.send_message("Rejoins d'abord un canal vocal.", ephemeral=True)
            return
        if inter.guild_id in _speaking or audio_busy():
            await inter.response.send_message("Je suis déjà en train de parler ou de préparer une voix.", ephemeral=True)
            return
        await inter.response.defer(thinking=True, ephemeral=True)
        try:
            campaign = campaign_store().active()
        except CampaignError:
            campaign = None
        print(f"[bot] voix : /dire par {inter.user.display_name} ({len(spoken)} caractères, voix {voix_nom or 'de la campagne'})")
        _speaking.add(inter.guild_id)                    # la synthèse compte aussi : pas deux /dire à la fois
        temporary = wav = None
        try:
            try:
                progress = make_progress(inter, asyncio.get_running_loop(), "Préparation de la voix")
                wav = await prepare_free_audio(engine, campaign, spoken, progress, voix_nom or None)
            except voix.VoixError as e:
                await finish_reply(inter, f"❌ {e}")
                return
            await finish_reply(inter, f"🎙️ « {spoken[:300]}{'…' if len(spoken) > 300 else ''} »")
            try:
                vc = active[2] if active else (temporary := await voice.channel.connect())   # en séance : le bot est déjà là
                await play_wav(vc, wav)
            except Exception as e:
                print(f"[bot] voix : lecture impossible {e!r}")
                await inter.followup.send(f"❌ Lecture impossible : {e!r}", ephemeral=True)
        finally:
            voix.discard_free(wav)                       # l'audio n'est jamais conservé
            _speaking.discard(inter.guild_id)
            if temporary is not None:
                try:
                    await temporary.disconnect()
                except Exception:
                    pass


    @bot.tree.command(name="transcript", description="Affiche les derniers messages transcrits")
    @app_commands.guild_only()
    @app_commands.describe(nombre="Combien de messages afficher (défaut 10)")
    async def transcript(inter: discord.Interaction, nombre: app_commands.Range[int, 1, 50] = 10):
        session = bot.sessions.get(inter.guild_id)
        if session is None:
            await inter.response.send_message("Aucun enregistrement en cours.", ephemeral=True)
            return
        _rec, live, _vc = session
        items = sorted(list(live.recent)[-nombre:], key=lambda x: x[0])
        if not items:
            await inter.response.send_message(
                f"Rien de transcrit pour l'instant.\n`{live.status_line()}`", ephemeral=True)
            return
        lines = [t for _, t in items]
        text = "\n".join(lines)
        while len(text) > 1900 and len(lines) > 1:     # limite Discord : 2000 caractères
            lines.pop(0)
            text = "\n".join(lines)
        await reply(inter, None, content=text[-1900:], allowed_mentions=discord.AllowedMentions.none())


    last_rule_call: dict = {}                 # serveur -> instant du dernier /regle (anti-abus)

    @bot.tree.command(name="regle", description="Demande à Gemini d'expliquer la règle évoquée dans les dernières phrases")
    @app_commands.guild_only()
    @app_commands.describe(jeu="Jeu concerné (défaut : celui de la campagne en cours, sinon config.ini)",
                           phrases="Nombre de dernières phrases à analyser (défaut : config.ini)",
                           question="Question libre envoyée à Gemini à la place du transcript (enregistrement inutile)",
                           dicter="Le bot dicte la réponse à voix haute dans le canal vocal",
                           voix_nom="Voix de la dictée (la liste est proposée) ; défaut : la voix de la campagne")
    @app_commands.rename(voix_nom="nom_voix")
    async def regle(inter: discord.Interaction, jeu: Optional[str] = None,
                    phrases: Optional[app_commands.Range[int, 1, 50]] = None, question: Optional[str] = None,
                    dicter: Optional[bool] = None, voix_nom: Optional[str] = None):
        if not SET["regles.activer"]:
            await inter.response.send_message("La commande /regle est désactivée (`activer = non` dans config.ini).",
                                              ephemeral=True)
            return
        if not api_key():
            await inter.response.send_message(
                "Aucune clé d'API Gemini configurée : définis `GEMINI_API_KEY` (voir le README).", ephemeral=True)
            return
        asked = " ".join((question or "").split())
        if asked and len(asked) > SET["regles.question_max"]:
            await inter.response.send_message(f"Question trop longue : {len(asked)} caractères (maximum "
                                              f"{SET['regles.question_max']}, réglage `question_max` de [regles]).", ephemeral=True)
            return
        session = bot.sessions.get(inter.guild_id)
        if session is None and not asked:
            await inter.response.send_message("Aucun enregistrement en cours (ou pose une `question`).", ephemeral=True)
            return
        wait = SET["regles.delai_min_s"] - (time.monotonic() - last_rule_call.get(inter.guild_id, -1e9))
        if wait > 0:
            await inter.response.send_message(f"Patiente encore {wait:.0f} s avant de redemander une règle.",
                                              ephemeral=True)
            return
        if dicter:
            problem = dictation_problem(bot, inter, voix_nom)
            if problem:
                await inter.response.send_message(f"❌ {problem}", ephemeral=True)
                return
        game = (jeu or "").strip() or campaign_game() or SET["regles.jeu_par_defaut"]
        if asked:                                         # question libre : ni transcript ni enregistrement nécessaires
            last_rule_call[inter.guild_id] = time.monotonic()
            await inter.response.defer(thinking=True)
            try:
                answer = await asyncio.to_thread(explain_question, asked, game)
            except RulesError as e:
                last_rule_call.pop(inter.guild_id, None)
                await inter.followup.send(f"❌ {e}", ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())
                return
            embed = discord.Embed(title=f"🎲 Règle — {game}"[:256], description=answer[:4000], colour=discord.Colour.blurple())
            embed.add_field(name="Question", value=discord.utils.escape_markdown(asked)[:1000], inline=False)
            embed.set_footer(text="Réponse générée par Gemini : à vérifier dans le livre de règles.")
            await reply(inter, None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            if dicter:
                await speak_answer(bot, engine, inter, answer, voix_nom)
            return
        rec, live, _vc = session
        rows = await asyncio.to_thread(read_rows, rec.dir / "transcript_live.jsonl")
        lines = transcript_lines(rows, phrases or SET["regles.nb_phrases"])
        if not lines:
            await inter.response.send_message(
                f"Rien de transcrit pour l'instant.\n`{live.status_line()}`", ephemeral=True)
            return
        last_rule_call[inter.guild_id] = time.monotonic()
        await inter.response.defer(thinking=True)
        try:
            answer = await asyncio.to_thread(explain_rule, lines, game)
        except RulesError as e:
            last_rule_call.pop(inter.guild_id, None)
            await inter.followup.send(f"❌ {e}", ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())
            return
        embed = discord.Embed(title=f"🎲 Règle — {game}"[:256], description=answer[:4000], colour=discord.Colour.blurple())
        quoted = "\n".join(lines)
        embed.add_field(name="Passages analysés", value=discord.utils.escape_markdown(quoted)[-1000:], inline=False)
        embed.set_footer(text="Réponse générée par Gemini à partir d'une transcription automatique : "
                              "à vérifier dans le livre de règles.")
        await reply(inter, None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        if dicter:
            await speak_answer(bot, engine, inter, answer, voix_nom)


    @bot.tree.command(name="stats", description="Statistiques de la session en cours")
    @app_commands.guild_only()
    async def stats_command(inter: discord.Interaction):
        session = bot.sessions.get(inter.guild_id)
        if session is None:
            await inter.response.send_message("Aucun enregistrement en cours.", ephemeral=True)
            return
        rec, live, _vc = session
        await inter.response.defer()
        stats = await asyncio.to_thread(session_stats, rec)
        await reply(inter, None,
                    embed=stats_embed(stats, f"📊 Stats en direct — session {rec.session_id}", live.pending_seconds()),
                    allowed_mentions=discord.AllowedMentions.none())


    @bot.tree.command(name="retranscrire", description="Refait le transcript d'une session depuis ses fichiers audio complets")
    @app_commands.guild_only()
    @app_commands.describe(session="Session à retranscrire (la liste est proposée) ; défaut : la dernière enregistrée")
    async def retranscrire(inter: discord.Interaction, session: Optional[str] = None):
        eph = dict(ephemeral=True)
        if not inter.user.guild_permissions.manage_guild:
            await inter.response.send_message("Il faut la permission « Gérer le serveur » pour cette commande.", **eph)
            return
        root = Path(SET["sessions.dossier"])
        available = retrans.sessions_with_audio(root)
        sid = (session or (available[0] if available else "")).strip()
        if sid not in available:
            await inter.response.send_message("Session introuvable (ou sans fichier audio) : choisis-en une dans la liste.", **eph)
            return
        if bot.sessions:
            await inter.response.send_message("Un enregistrement est en cours : WhisperX en a besoin. Réessaie après `/stop`.", **eph)
            return
        if _RETRANSCRIBING:
            await inter.response.send_message(f"Une retranscription est déjà en cours (`{next(iter(_RETRANSCRIBING))}`).", **eph)
            return
        if audio_busy():
            await inter.response.send_message("Une synthèse vocale est en cours : réessaie quand elle sera finie.", **eph)
            return
        if engine.failed:
            await inter.response.send_message(f"WhisperX est indisponible ({engine.error!r}).", **eph)
            return
        _RETRANSCRIBING.add(sid)
        try:
            await inter.response.defer(ephemeral=True)
            engine.start_loading()                          # sans effet s'il est déjà chargé ; le recharge s'il avait été libéré
            target = out_channel(inter)
            await inter.followup.send(f"🔁 Retranscription de `{sid}` lancée, suivi "
                                      + (f"dans {target.mention}." if target else "ci-dessous.")
                                      + " L'ancien transcript sera conservé.", ephemeral=True)
            no_mentions = discord.AllowedMentions.none()
            board = StatusBoard(target or inter.channel, asyncio.get_running_loop(),
                                f"**Retranscription de la session `{sid}`**",
                                [("whisper", "Modèle WhisperX"), ("asr", "Transcription des fichiers audio"),
                                 ("ecriture", "Transcript et statistiques")], no_mentions)
            try:
                await board.start()
            except Exception as e:
                print(f"[bot] suivi dans le salon impossible ({e!r}), repli sur la réponse de la commande")
                board.msg = await inter.followup.send(board.render(), wait=True)

            def work():
                board.update("whisper", "running", engine.state_text)
                if not engine.wait_ready():
                    raise retrans.RetranscribeError(f"WhisperX indisponible ({engine.error!r}).")
                board.update("whisper", "ok")
                board.update("asr", "running", "découpage")

                def progress(text):
                    if text.startswith("Écriture"):
                        board.update("asr", "ok")
                        board.update("ecriture", "running")
                    else:
                        board.update("asr", "running", text)
                return retrans.retranscribe(root / sid, engine, progress)

            try:
                out = await asyncio.to_thread(work)
            except Exception as e:
                reason = str(e) if isinstance(e, retrans.RetranscribeError) else repr(e)
                for key in ("whisper", "asr", "ecriture"):
                    if board.state[key][0] in ("pending", "running"):
                        board.update(key, "failed" if board.state[key][0] == "running" else "skipped",
                                     reason[:150] if board.state[key][0] == "running" else "")
                await board.refresh()
                await _say(inter, target, content=f"❌ Retranscription de `{sid}` impossible : {reason}\n"
                                                  "Le transcript existant n'a pas été modifié.", allowed_mentions=no_mentions)
                return
            board.update("ecriture", "ok", f"{out['lignes']} lignes, {out['mots']} mots")
            await board.refresh()
            kept = f"\n📂 Ancien transcript conservé dans `{out['sauvegarde'].name}/`." if out["sauvegarde"] else ""
            speed = f" en {out['asr_s']:.0f} s de calcul" if out["asr_s"] else " (résultats déjà calculés)"
            await _say(inter, target, allowed_mentions=no_mentions,
                       content=(f"✅ **Session `{sid}` retranscrite** : {out['lignes']} lignes (avant : {out['ancien_nb_lignes']}), "
                                f"{out['mots']} mots, {out['rejetes']} rejetée(s) comme bruit{speed}.{kept}\n"
                                "ℹ️ Le MP3, le rapport PDF, le Drive et la mémoire de campagne ne sont pas refaits "
                                "(`/campagne resumer` refait le compte rendu à partir du nouveau transcript)."),
                       embed=stats_embed(out["stats"], f"📊 Statistiques — session {sid}"))
        finally:
            _RETRANSCRIBING.discard(sid)


    @retranscrire.autocomplete("session")
    async def retranscrire_session_autocomplete(inter: discord.Interaction, current: str):
        found = [s for s in retrans.sessions_with_audio(Path(SET["sessions.dossier"])) if (current or "").lower() in s.lower()]
        return [app_commands.Choice(name=s, value=s) for s in found[:25]]


    @bot.tree.command(name="status", description="État de l'enregistrement et de la file de transcription")
    @app_commands.guild_only()
    async def status(inter: discord.Interaction):
        session = bot.sessions.get(inter.guild_id)
        if session is None:
            await inter.response.send_message("Aucun enregistrement en cours.", ephemeral=True)
            return
        rec, live, _vc = session
        await inter.response.send_message(
            f"🔴 Session `{rec.session_id}` — {fmt_ts(rec.now_samples() / RATE)[:8]} — "
            f"{len(rec.names)} participant(s)\n`{live.status_line()}`\nGoogle Drive : {drive.state_text}",
            ephemeral=True)


    @bot.tree.command(name="stop", description="Arrête l'enregistrement et lance le traitement de fin de session")
    @app_commands.guild_only()
    async def stop(inter: discord.Interaction):
        session = bot.sessions.pop(inter.guild_id, None)
        if session is None:
            await inter.response.send_message("Aucun enregistrement en cours.", ephemeral=True)
            return
        await inter.response.defer()
        rec, live, vc = session
        cfg = load_config()                    # la config en vigueur au moment de l'arrêt
        vc.stop_listening()
        await vc.disconnect()
        target = out_channel(inter)
        await inter.followup.send("⏹️ Enregistrement arrêté. Traitement en cours, suivi "
                                  + (f"dans {target.mention}." if target else "ci-dessous."))

        no_mentions = discord.AllowedMentions.none()
        board = StatusBoard(target or inter.channel, asyncio.get_running_loop(),
                            f"**Traitement de la session `{rec.session_id}`**", PIPELINE_STEPS, no_mentions)
        for key in ("mp3", "rapport", "drive"):
            if not cfg[key]:
                board.state[key] = ("skipped", "désactivé")
        try:
            await board.start()
        except Exception as e:
            print(f"[bot] suivi dans le salon impossible ({e!r}), repli sur la réponse de la commande")
            board.msg = await inter.followup.send(board.render(), wait=True)

        res = await asyncio.to_thread(run_pipeline, rec, live, cfg, board, drive)
        await board.refresh()

        lines = [f"✅ **Session `{rec.session_id}` terminée.**"]
        if res["link"]:
            lines.append(f"📁 Dossier Drive : {res['link']}")
            lines.append("🔗 Toute personne disposant de ce lien peut consulter les fichiers." if res["shared"]
                         else "🔒 Dossier non partagé : accessible uniquement avec le compte Google du bot.")
        else:
            lines.append(f"📂 Fichiers conservés en local : `{rec.dir}`")
        lines += [f"⚠️ {n}" for n in res["notes"]]
        kwargs = {"content": "\n".join(lines), "allowed_mentions": no_mentions}
        if res["stats"]:
            kwargs["embed"] = stats_embed(res["stats"], f"📊 Statistiques — session {rec.session_id}")
        await _say(inter, target, **kwargs)
        if res["memoire"]:
            m = res["memoire"]
            await _say(inter, target, allowed_mentions=no_mentions, embed=discord.Embed(
                title=f"📜 Compte rendu — {m['titre']}"[:256], description=m["compte_rendu"][:4000],
                colour=discord.Colour.blurple()))


    @bot.tree.command(name="campagne", description="Gère les campagnes de jeu de rôle : mémoire, joueurs, contexte, retour en arrière")
    @app_commands.describe(action="Action à effectuer",
                           nom="Nom de la campagne (nouvelle, choisir)",
                           jeu="Jeu de la nouvelle campagne (défaut : config.ini)",
                           mj="MJ de la campagne (nouvelle, mj) ; sans valeur, le MJ reste à définir",
                           membre="Membre à ajouter comme joueur (joueur)",
                           personnage="Personnage joué, de préférence décrit dans contexte.md (joueur)",
                           quoi="Contenu à afficher (afficher)",
                           rubrique="Une seule rubrique du résumé global (ex. fils ouverts, lieux) ; la liste est proposée",
                           minutes="Pack : durée d'une partie en minutes, 0 = pas de découpe (défaut : config.ini)",
                           session="Identifiant de séance (resumer) ; défaut : la dernière session enregistrée",
                           voix_nom="Choisir une voix dans la liste (actions audio, dicter et voix) ; sans valeur, voix affiche la liste",
                           fichier="Fichier texte : contexte, journal, transcript (pack, resumer) ou réponse du modèle (integrer)",
                           sortie="Pack : copié dans le presse-papiers (défaut) ou publié en fichiers",
                           partie="Pack en plusieurs parties : numéro de la partie à copier (défaut : 1)")
    @app_commands.rename(voix_nom="nom_voix")
    @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in CAMPAGNE_ACTIONS],
                          quoi=[app_commands.Choice(name=n, value=v) for n, v in AFFICHER_CHOIX],
                          sortie=[app_commands.Choice(name=n, value=n) for n in ("presse-papiers", "fichier")])
    @app_commands.guild_only()
    async def campagne(inter: discord.Interaction, action: app_commands.Choice[str], voix_nom: Optional[str] = None,
                       nom: Optional[str] = None,
                       jeu: Optional[str] = None, mj: Optional[discord.Member] = None,
                       membre: Optional[discord.Member] = None, personnage: Optional[str] = None,
                       quoi: Optional[app_commands.Choice[str]] = None, rubrique: Optional[str] = None,
                       minutes: Optional[app_commands.Range[int, 0, 240]] = None,
                       session: Optional[str] = None, fichier: Optional[discord.Attachment] = None,
                       sortie: Optional[app_commands.Choice[str]] = None,
                       partie: Optional[app_commands.Range[int, 1, 20]] = None):
        act, store = action.value, campaign_store()
        eph = dict(ephemeral=True)
        if act in ("nouvelle", "choisir", "contexte", "mj", "journal", "rollback", "resumer", "joueur", "pack", "integrer", "audio", "audio force") and not inter.user.guild_permissions.manage_guild:
            await inter.response.send_message("Il faut la permission « Gérer le serveur » pour cette action.", **eph)
            return
        if act == "liste":
            found, current = store.list(), store.active()
            body = "\n".join(f"{'▶' if current and c.slug == current.slug else '•'} **{c.titre}** — {c.jeu or '?'}"
                             f" ({c.nb_seances()} séance(s))" for c in found)
            await inter.response.send_message(body or "Aucune campagne. Crée-en une avec `/campagne nouvelle`.", **eph)
            return
        if act == "info":
            try:
                c = store.get(nom) if nom else store.active()
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            if c is None:
                await inter.response.send_message("Aucune campagne.", **eph)
                return
            j = c.joueurs()
            players = ", ".join(f"{x['joueur']} ({x.get('personnage') or 'personnage ?'})" for x in j["joueurs"]) or "aucun"
            await inter.response.send_message(
                f"📖 **{c.titre}** — {c.jeu or '?'}\nMJ : {', '.join(j['mj'].get('pseudos', [])) or '?'}\n"
                f"Joueurs : {players}\nContexte : {'rempli' if c.contexte_rempli() else 'à compléter'}"
                f" · Séances : {c.nb_seances()} · Résumé global : {'oui' if c.resume_global() else 'non'}\n"
                f"Dossier : `{c.path}`", **eph)
            return
        if act == "choisir":
            if not nom:
                await inter.response.send_message("Indique le nom de la campagne.", **eph)
                return
            if inter.guild_id in bot.sessions:
                await inter.response.send_message("Impossible de changer de campagne pendant un enregistrement.", **eph)
                return
            try:
                c = store.get(nom)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            store.set_active(c)
            await reply(inter, None, content=f"📖 Campagne active : **{c.titre}**",
                        allowed_mentions=discord.AllowedMentions.none())
            return
        if act == "nouvelle":
            if not nom or not nom.strip():
                await inter.response.send_message("Indique le nom de la campagne.", **eph)
                return
            voice = getattr(inter.user, "voice", None)
            members = [m for m in voice.channel.members if not getattr(m, "bot", False)] if voice and voice.channel else []
            try:
                c = store.create(nom, (jeu or "").strip() or SET["regles.jeu_par_defaut"],
                                 _member_info(mj) if mj else None, [_member_info(m) for m in members])
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            n = len(c.joueurs()["joueurs"])
            await reply(inter, None,
                        content=f"📖 Campagne **{c.titre}** créée et activée ({n} joueur(s) repéré(s) dans le canal vocal, "
                                f"MJ : {mj.display_name if mj else 'à définir avec `/campagne mj`'}).\n"
                                f"Étapes suivantes : `/campagne contexte` (personnages) et `/campagne journal` (historique existant).",
                        allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            c = store.get(nom) if nom else store.active()
        except CampaignError as e:
            await inter.response.send_message(f"❌ {e}", **eph)
            return
        if c is None:
            await inter.response.send_message("Aucune campagne : crée-en une avec `/campagne nouvelle`.", **eph)
            return
        if act == "afficher":
            what = quoi.value if quoi else ("resume" if rubrique else "compte_rendu")
            try:
                if what == "resume":
                    path = c.resume_global()
                    if path is None:
                        raise CampaignError(f"Pas encore de résumé global pour {c.titre}.")
                    if rubrique and rubrique.strip():
                        title, body = camp.find_section(path.read_text(encoding="utf-8"), rubrique)
                        if not body:
                            raise CampaignError(f"La rubrique « {title} » est vide.")
                        if len(body) <= 3900:
                            await reply(inter, None, allowed_mentions=discord.AllowedMentions.none(),
                                        embed=discord.Embed(title=f"📚 {title} — {c.titre}"[:256], description=body,
                                                            colour=discord.Colour.blurple()))
                        else:
                            await reply(inter, None, content=f"📚 {title} — **{c.titre}**",
                                        files=[discord.File(io.BytesIO(body.encode("utf-8")),
                                                            filename=f"{camp.slugify(title) or 'rubrique'}.md")],
                                        allowed_mentions=discord.AllowedMentions.none())
                        return
                    await reply(inter, None, content=f"📚 Résumé global — **{c.titre}**",
                                files=[discord.File(path)], allowed_mentions=discord.AllowedMentions.none())
                    return
                label, fname = ("Compte rendu", "compte_rendu.md") if what == "compte_rendu" else ("Précédemment", "precedemment.md")
                sid, text = memoire.seance_text(c, fname, (session or "").strip() or None)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            if len(text) <= 3900:
                await reply(inter, None, allowed_mentions=discord.AllowedMentions.none(), embed=discord.Embed(
                    title=f"{label} — {c.titre} ({sid})"[:256], description=text, colour=discord.Colour.blurple()))
            else:
                await reply(inter, None, content=f"{label} — **{c.titre}** ({sid})",
                            files=[discord.File(c.path / "seances" / sid / fname)],
                            allowed_mentions=discord.AllowedMentions.none())
            return
        if act == "dicter":
            if not SET["voix.activer"]:
                await inter.response.send_message("La voix est désactivée (`activer = non` dans [voix] de config.ini).", **eph)
                return
            try:
                sid, text = memoire.seance_text(c, "precedemment.md", (session or "").strip() or None)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            if (voix_nom or "").strip():
                # voix choisie : génération à la volée, audio temporaire supprimé après lecture ; le « précédemment »
                # préparé (precedemment.wav) n'est ni lu, ni remplacé
                problem = voice_error(voix_nom)
                if problem:
                    await inter.response.send_message(f"❌ {problem}", **eph)
                    return
                if whisper_conflict(bot):
                    await inter.response.send_message("Impossible pendant un enregistrement : la synthèse libère Whisper "
                                                      "(réglage `liberer_whisper` de [voix]). Sans `nom_voix`, je lis l'audio "
                                                      "déjà préparé.", **eph)
                    return
                if inter.guild_id in _speaking or audio_busy():
                    await inter.response.send_message("Je suis déjà en train de parler ou de préparer une voix.", **eph)
                    return
                active = bot.sessions.get(inter.guild_id)
                voice = getattr(inter.user, "voice", None)
                if active is None and not (voice and voice.channel):
                    await inter.response.send_message("Rejoins d'abord un canal vocal (ou lance /record).", **eph)
                    return
                await inter.response.defer(thinking=True, ephemeral=True)
                _speaking.add(inter.guild_id)
                temporary = live_wav = None
                try:
                    try:
                        progress = make_progress(inter, asyncio.get_running_loop(), f"Synthèse du « précédemment » ({sid})")
                        live_wav = await prepare_free_audio(engine, c, voix.clean_for_speech(text), progress, voix_nom.strip())
                    except voix.VoixError as e:
                        await finish_reply(inter, f"❌ {e}")
                        return
                    await finish_reply(inter, f"🎙️ Lecture du « précédemment » ({sid}) avec la voix `{voix_nom.strip()}`.")
                    try:
                        vc = active[2] if active else (temporary := await voice.channel.connect())
                        await play_wav(vc, live_wav)
                    except Exception as e:
                        print(f"[bot] voix : lecture impossible {e!r}")
                        await inter.followup.send(f"❌ Lecture impossible : {e!r}", ephemeral=True)
                finally:
                    voix.discard_free(live_wav)
                    _speaking.discard(inter.guild_id)
                    if temporary is not None:
                        try:
                            await temporary.disconnect()
                        except Exception:
                            pass
                return
            wav, state = voix.playable_audio(c, sid, text)
            if wav is None:
                await inter.response.send_message(f"🔇 {audio_unavailable(c, sid, state)}", **eph)
                return
            if inter.guild_id in _speaking:
                await inter.response.send_message("Je suis déjà en train de parler.", **eph)
                return
            active = bot.sessions.get(inter.guild_id)
            voice = getattr(inter.user, "voice", None)
            if active is None and not (voice and voice.channel):
                await inter.response.send_message("Rejoins d'abord un canal vocal (ou lance /record).", **eph)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            _speaking.add(inter.guild_id)
            temporary = None
            try:
                vc = active[2] if active else (temporary := await voice.channel.connect())
                note = (f"\nℹ️ Audio préparé avec d'autres réglages ({state['detail']}) ; `/campagne audio force` pour le refaire."
                        if state["etat"] == "reglages" else "")
                await inter.followup.send(f"🎙️ Lecture du « précédemment » ({sid})…{note}", ephemeral=True)
                await play_wav(vc, wav)
            except Exception as e:
                print(f"[bot] voix : lecture impossible {e!r}")
                await inter.followup.send(f"❌ Lecture impossible : {e!r}", ephemeral=True)
            finally:
                _speaking.discard(inter.guild_id)
                if temporary is not None:
                    try:
                        await temporary.disconnect()
                    except Exception:
                        pass
            return
        if act == "voix":
            try:
                c = store.get(nom) if nom else store.active()
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            if c is None:
                await inter.response.send_message("Aucune campagne active (`/campagne nouvelle` ou `/campagne choisir`).", **eph)
                return
            available, chosen = voix.list_voices(), voix.campaign_voice(c)
            if not (voix_nom or "").strip():
                lines = [f"- `{n}`" + (" ← campagne" if n.lower() == chosen.lower() else "") for n in available]
                await inter.response.send_message(
                    f"**Voix disponibles** (dossier `{voix.voices_dir()}`, fichiers `voix.<nom>.wav`) :\n"
                    + ("\n".join(lines) or "aucune") + "\n- `défaut` : voix par défaut du moteur"
                    + (" ← campagne" if chosen.lower() in voix.NO_VOICE else "") + f"\nVoix de **{c.titre}** : "
                    + (f"`{chosen}`" if chosen else "aucune choisie (ancien réglage ou voix par défaut du moteur)"), **eph)
                return
            if not inter.user.guild_permissions.manage_guild:
                await inter.response.send_message("Il faut la permission « Gérer le serveur » pour changer la voix.", **eph)
                return
            wrong = voice_error(voix_nom)
            if wrong:
                await inter.response.send_message(f"❌ {wrong}", **eph)
                return
            picked = "défaut" if voix_nom.strip().lower() in voix.NO_VOICE else next(
                n for n in available if n.lower() == voix_nom.strip().lower())
            camp.set_voix(c, picked)
            await inter.response.send_message(
                f"✅ Voix de **{c.titre}** : `{picked}`. Les audios déjà préparés restent lisibles ; "
                "`/campagne audio force` les refait avec cette voix.", **eph)
            return
        if act in ("audio", "audio force"):
            regenerer = act == "audio force"
            wrong = voice_error(voix_nom)
            if wrong:
                await inter.response.send_message(f"❌ {wrong}", **eph)
                return
            if not SET["voix.activer"]:
                await inter.response.send_message("La voix est désactivée (`activer = non` dans [voix] de config.ini).", **eph)
                return
            if whisper_conflict(bot):
                await inter.response.send_message("Impossible pendant un enregistrement : Whisper et le moteur vocal ne doivent pas "
                                                  "être chargés en même temps (réglage `liberer_whisper` de [voix]).", **eph)
                return
            try:
                sid, text = memoire.seance_text(c, "precedemment.md", (session or "").strip() or None)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            if not regenerer and voix.cached_audio(c, sid, text, voix_nom or None) is not None:
                await inter.response.send_message(f"✅ L'audio du « précédemment » ({sid}) est déjà prêt. "
                                                  "Pour en refaire un autre : `audio force`.", **eph)
                return
            if audio_busy():
                await inter.response.send_message("Une synthèse vocale est déjà en cours.", **eph)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            try:
                progress = make_progress(inter, asyncio.get_running_loop(), f"Synthèse du « précédemment » ({sid})")
                await prepare_audio(engine, c, sid, text, force=regenerer, progress=progress, voice=voix_nom or None)
            except voix.VoixError as e:
                await finish_reply(inter, f"❌ {e}")
                return
            await finish_reply(inter, f"✅ Audio du « précédemment » prêt ({sid}). Whisper se recharge (quelques instants).")
            return
        if act == "mj":
            if mj is None:
                await inter.response.send_message("Indique le MJ (paramètre `mj`).", **eph)
                return
            camp.set_mj(c, _member_info(mj))
            await inter.response.send_message(f"✅ MJ de **{c.titre}** : {mj.display_name}.", **eph)
            return
        if act == "rollback":
            if inter.guild_id in bot.sessions:
                await inter.response.send_message("Impossible pendant un enregistrement.", **eph)
                return
            try:
                undone, now, gone = camp.rollback(c)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            await inter.response.send_message(
                f"↩️ **{c.titre}** : version `{undone}` annulée (archivée dans `resume_global/annulees/`), "
                f"résumé global = `{now}`." + (f" Séance `{gone}` retirée de la mémoire (`seances/_annulees/`)." if gone else ""),
                ephemeral=True)
            return
        if act == "joueur":
            if membre is None:
                await inter.response.send_message("Indique le membre à ajouter (paramètre `membre`).", **eph)
                return
            try:
                status, warns = camp.add_player(c, _member_info(membre), personnage or "")
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            noms = ", ".join(camp.contexte_personnages(c)) or "aucun personnage décrit dans contexte.md"
            await inter.response.send_message(
                f"✅ {membre.display_name} {status} dans **{c.titre}**"
                + (f" (personnage : {personnage})" if personnage else "") + "."
                + "".join(f"\n⚠️ {w}" for w in warns) + (f"\nPersonnages du contexte : {noms}" if not personnage or warns else ""),
                **eph)
            return
        if act == "resumer":
            if inter.guild_id in bot.sessions:
                await inter.response.send_message("Impossible pendant un enregistrement.", **eph)
                return
            claude_mode = SET["campagnes.moteur_resume"] == "claude"
            if claude_mode and claude_cli.reason_unavailable():
                await inter.response.send_message(
                    f"Claude indisponible : {claude_cli.reason_unavailable()}. (`moteur_resume = gemini` dans [campagnes] "
                    "pour l'ancienne méthode.)", **eph)
                return
            if not claude_mode and not gemini_api_key():
                await inter.response.send_message("Aucune clé d'API Gemini configurée.", **eph)
                return
            if fichier is not None and fichier.size > SET["campagnes.taille_max_transcript_ko"] * 1024:
                await inter.response.send_message(
                    f"Fichier trop gros (max {SET['campagnes.taille_max_transcript_ko']} Ko, réglable dans [campagnes]).", **eph)
                return
            if inter.guild_id in _RESUMING:
                await inter.response.send_message("Un résumé est déjà en cours sur ce serveur.", **eph)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            _RESUMING.add(inter.guild_id)
            try:
                loaded = await session_rows(inter, fichier, session)
                if loaded is None:
                    return
                sid, rows = loaded
                if claude_mode:
                    if (c.path / "seances" / sid / "meta.json").exists():
                        await inter.followup.send(f"La séance {sid} est déjà intégrée (`/campagne rollback` pour la refaire).",
                                                  ephemeral=True)
                        return
                    out = await asyncio.to_thread(packs.resume_with_claude, c, rows, sid)
                else:
                    out = await asyncio.to_thread(memoire.summarize_session, c, rows, sid)
            except (CampaignError, GeminiError) as e:
                await inter.followup.send(f"❌ {e}", ephemeral=True)
                return
            finally:
                _RESUMING.discard(inter.guild_id)
            if claude_mode:
                await announce_integration(
                    bot, engine, inter, c, out,
                    f"✅ Séance `{sid}` intégrée à **{c.titre}** (`{out['version']}`) {claude_summary_line(out['reponse'])}\n"
                    "↩️ Un défaut ? `/campagne rollback` annule cette séance.", inter.followup.send)
                return
            await inter.followup.send(f"✅ Séance `{sid}` intégrée à **{c.titre}** (`{out['version']}`).", ephemeral=True)
            await _say(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(), embed=discord.Embed(
                title=f"📜 Compte rendu — {c.titre}"[:256], description=out["compte_rendu"][:4000],
                colour=discord.Colour.blurple()))
            return
        if act == "pack":
            if inter.guild_id in bot.sessions:
                await inter.response.send_message("Impossible pendant un enregistrement.", **eph)
                return
            if fichier is not None and fichier.size > SET["campagnes.taille_max_transcript_ko"] * 1024:
                await inter.response.send_message("Fichier trop gros.", **eph)
                return
            to_file = sortie is not None and sortie.value == "fichier"
            if not to_file and presse_papiers.reason_unavailable():
                await inter.response.send_message(
                    f"Copie impossible : {presse_papiers.reason_unavailable()}. Utilise `sortie:fichier` pour recevoir le pack "
                    "en fichier.", **eph)
                return
            await inter.response.defer(thinking=True, ephemeral=(not to_file) or out_channel(inter) is not None)
            try:
                loaded = await session_rows(inter, fichier, session)
                if loaded is None:
                    return
                sid, rows = loaded
                if (c.path / "seances" / sid / "meta.json").exists():
                    await inter.followup.send(f"La séance {sid} est déjà intégrée (`rollback` pour la refaire).", ephemeral=True)
                    return
                made = await asyncio.to_thread(packs.build_packs, c, rows, sid, minutes)
            except CampaignError as e:
                await inter.followup.send(f"❌ {e}", ephemeral=True)
                return
            if to_file:
                await reply(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(),
                            **pack_message({**made, "titre": c.titre}))
                return
            parts, k = made["parties"], partie or 1
            if k > len(parts):
                await inter.followup.send(f"Ce pack n'a que {len(parts)} partie(s) : `partie:{len(parts)}` au plus.", ephemeral=True)
                return
            text = made["fichiers"][k - 1].read_text(encoding="utf-8")
            try:
                await asyncio.to_thread(presse_papiers.copy, text)
            except presse_papiers.ClipboardError as e:
                await inter.followup.send(f"❌ {e}. Utilise `sortie:fichier` pour recevoir le pack en fichier.", ephemeral=True)
                return
            many = len(parts) > 1
            lines = [f"📋 Pack de **{c.titre}**{f' (partie {k}/{len(parts)})' if many else ''} copié dans le presse-papiers "
                     f"de la machine du bot ({len(text)} caractères). Colle-le (Ctrl+V) dans Claude ou Gemini"
                     f"{', dans la même conversation et dans l' + chr(39) + 'ordre' if many else ''}.",
                     f"La réponse doit commencer par `=== FIN LUE === {parts[k - 1]['fin_attendue']}` "
                     f"(dernière réplique « {parts[k - 1]['derniere'][:50]} »)."]
            lines.append(f"Partie suivante : `/campagne pack partie:{k + 1}`." if k < len(parts) else
                         "Ensuite relis la réponse, copie-la et lance `/campagne integrer`.")
            await inter.followup.send("\n".join(lines), ephemeral=True)
            return
        if act == "integrer":
            if fichier is None and presse_papiers.reason_unavailable():
                await inter.response.send_message(
                    f"Joins la réponse du modèle (fichier texte, paramètre `fichier`) : {presse_papiers.reason_unavailable()}.", **eph)
                return
            if fichier is not None and fichier.size > limit_bytes():
                await inter.response.send_message(f"Fichier trop gros (max {SET['campagnes.taille_max_brut_ko']} Ko).", **eph)
                return
            if inter.guild_id in bot.sessions:
                await inter.response.send_message("Impossible pendant un enregistrement.", **eph)
                return
            try:
                if fichier is not None:
                    answer = (await fichier.read()).decode("utf-8", errors="replace")
                else:
                    try:
                        answer = await asyncio.to_thread(presse_papiers.paste)
                    except presse_papiers.ClipboardError as e:
                        await inter.response.send_message(f"❌ {e}. Joins la réponse en fichier (`fichier`).", **eph)
                        return
                    if not answer.strip():
                        await inter.response.send_message("Le presse-papiers de la machine du bot ne contient pas de texte : "
                                                          "copie d'abord la réponse du modèle (ou joins un `fichier`).", **eph)
                        return
                    if len(answer.encode("utf-8")) > limit_bytes():
                        await inter.response.send_message(f"Texte trop gros (max {SET['campagnes.taille_max_brut_ko']} Ko).", **eph)
                        return
                out = packs.integrate(c, answer, (session or "").strip() or None)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            await announce_integration(bot, engine, inter, c, out, f"✅ Séance intégrée à **{c.titre}** (`{out['version']}`).",
                                       inter.response.send_message)
            return
        if act == "journal":
            if fichier is None:
                await inter.response.send_message("Joins le journal existant (paramètre `fichier`, texte brut).", **eph)
                return
            if fichier.size > limit_bytes():
                await inter.response.send_message(f"Fichier trop gros (max {SET['campagnes.taille_max_brut_ko']} Ko).", **eph)
                return
            try:
                text = (await fichier.read()).decode("utf-8", errors="replace")
                path = camp.import_journal(c, text)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            await inter.response.send_message(f"✅ Journal importé comme résumé global initial (`{path.name}`).", **eph)
            return
        # contexte
        limit = limit_bytes()
        if fichier is not None and fichier.size > limit:
            await inter.response.send_message(f"Fichier trop gros (max {SET['campagnes.taille_max_brut_ko']} Ko).", **eph)
            return
        await inter.response.defer(thinking=True, ephemeral=True)
        try:
            if fichier is not None:
                brut = (await fichier.read()).decode("utf-8", errors="replace")
                c.brut_path.write_text(brut, encoding="utf-8")
            elif c.brut_path.exists():
                brut = c.brut_path.read_text(encoding="utf-8", errors="replace")
            else:
                await inter.followup.send(f"Joins un fichier texte (paramètre `fichier`) ou dépose `contexte_brut.txt` "
                                          f"dans `{c.path}`.", ephemeral=True)
                return
            if len(brut.encode("utf-8")) > limit:
                await inter.followup.send("Texte trop long.", ephemeral=True)
                return
            result = await asyncio.to_thread(generate_contexte, c, brut)
        except (CampaignError, GeminiError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        await inter.followup.send(
            f"✅ `{result['fichier']}` écrit dans `{c.path}`. Personnages associés : {result['remplis']}/{result['total']}.\n"
            f"⚠️ Relis-le : les faits au doute sont marqués [à confirmer], aucun secret n'est déduit automatiquement.",
            ephemeral=True)


    @campagne.autocomplete("session")
    async def campagne_session_autocomplete(inter: discord.Interaction, current: str):
        """Propose les séances existantes (recherche par morceau d'identifiant, 25 maximum)."""
        try:
            wanted = getattr(getattr(inter, "namespace", None), "nom", None)
            store = campaign_store()
            c = store.get(wanted) if wanted else store.active()
        except CampaignError:
            c = None
        return [app_commands.Choice(name=label, value=sid)
                for sid, label in memoire.session_choices(c, Path(SET["sessions.dossier"]), current or "")]


    @campagne.autocomplete("voix_nom")
    async def campagne_voix_autocomplete(inter: discord.Interaction, current: str):
        return voice_choices(current)


    @regle.autocomplete("voix_nom")
    async def regle_voix_autocomplete(inter: discord.Interaction, current: str):
        return voice_choices(current)


    @dire.autocomplete("voix_nom")
    async def dire_voix_autocomplete(inter: discord.Interaction, current: str):
        return voice_choices(current)


    @campagne.autocomplete("rubrique")
    async def campagne_rubrique_autocomplete(inter: discord.Interaction, current: str):
        """Rubriques du résumé global de la campagne (titres détectés dans le document)."""
        try:
            wanted = getattr(getattr(inter, "namespace", None), "nom", None)
            store = campaign_store()
            c = store.get(wanted) if wanted else store.active()
            path = c.resume_global() if c else None
            titles = [t for t, _ in camp.split_sections(path.read_text(encoding="utf-8"))] if path else []
        except (CampaignError, OSError):
            titles = []
        needle = (current or "").lower()
        return [app_commands.Choice(name=t[:100], value=t[:100]) for t in titles if needle in t.lower()][:25]


    @bot.tree.command(name="config", description="Affiche ou modifie la configuration du traitement de fin de session")
    @app_commands.describe(cle="Paramètre à consulter ou modifier (vide = tout afficher)",
                           valeur="Nouvelle valeur : oui/non, un nombre, ou lien/aucun selon le paramètre")
    @app_commands.choices(cle=[app_commands.Choice(name=k, value=k) for k in CONFIG_SPEC])
    @app_commands.guild_only()
    async def config_command(inter: discord.Interaction, cle: Optional[app_commands.Choice[str]] = None,
                             valeur: Optional[str] = None):
        cfg = load_config()

        def fmt(v) -> str:
            return "oui" if v is True else "non" if v is False else str(v)

        if cle is None:
            lines = [f"`{k:<17}` **{fmt(cfg[k])}** — {CONFIG_SPEC[k][2]}" for k in CONFIG_SPEC]
            await inter.response.send_message("⚙️ **Configuration du traitement de fin de session**\n" + "\n".join(lines),
                                              ephemeral=True)
            return
        key = cle.value
        if valeur is None:
            await inter.response.send_message(f"`{key}` = **{fmt(cfg[key])}** — {CONFIG_SPEC[key][2]}", ephemeral=True)
            return
        if not inter.user.guild_permissions.manage_guild:
            await inter.response.send_message("Il faut la permission « Gérer le serveur » pour modifier la configuration.",
                                              ephemeral=True)
            return
        try:
            cfg[key] = parse_config_value(key, valeur)
        except ValueError as e:
            await inter.response.send_message(f"Valeur invalide pour `{key}` : {e}.", ephemeral=True)
            return
        save_config(cfg)
        if key == "drive" and cfg["drive"]:
            drive.start()                      # sans effet si déjà démarré
        await inter.response.send_message(f"⚙️ `{key}` = **{fmt(cfg[key])}**", ephemeral=True)
