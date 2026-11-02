"""Commandes slash du bot : /record, /stop, /transcript, /regle, /stats, /status, /config."""
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
from . import memoire, pack as packs, voix
from .transcript import read_rows as _read_rows
from .campagne import CampaignError, CampaignStore, generate_contexte
from .config import CONFIG_SPEC, SET, load_config, parse_config_value, save_config
from .constants import RATE
from .live import LiveTranscriber
from .pipeline import PIPELINE_STEPS, StatusBoard, run_pipeline
from .gemini import GeminiError, api_key as gemini_api_key
from .rules import RulesError, api_key, explain_rule, transcript_lines
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
    """Salon dédié aux messages du bot (discord.canal_messages), ou None pour répondre là où la commande est lancée.

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
    lines.append("Relis la réponse de la dernière partie, enregistre-la dans un fichier texte puis `/campagne integrer fichier:…`.")
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


AUDIO_NON_PRET = ("Audio du « précédemment » non prêt. Prépare-le avec `/campagne audio` (hors enregistrement) ; "
                  "il l'est automatiquement après `/campagne integrer`.")


async def speak_recap(bot, guild_id, campaign, vc, channel):
    """Au démarrage d'un enregistrement : lit l'audio préparé du « précédemment » (jamais de synthèse ici)."""
    try:
        sid, text = memoire.seance_text(campaign, "precedemment.md")
    except CampaignError:
        return                                          # pas encore de « précédemment » : rien à dire
    wav = voix.cached_audio(campaign, sid, text)
    if wav is None:
        try:
            await channel.send(f"🔇 {AUDIO_NON_PRET}")
        except Exception:
            pass
        return
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


def audio_busy() -> bool:
    return _preparing["on"] or voix.is_busy()


async def prepare_audio(engine, campaign, session_id, text, force: bool = False, progress=None) -> Path:
    """Prépare l'audio du « précédemment » sans jamais avoir Whisper et le moteur vocal chargés ensemble :
    Whisper est libéré de la mémoire, le moteur vocal synthétise, puis Whisper est rechargé.
    À n'appeler que hors enregistrement (les appelants le vérifient)."""
    ready = None if force else voix.cached_audio(campaign, session_id, text)
    if ready is not None:
        return ready
    if audio_busy():
        raise voix.VoixError("Une synthèse vocale est déjà en cours.")
    _preparing["on"] = True
    try:
        if progress:
            progress("Libération de Whisper", 0, 0)
        await asyncio.to_thread(engine.release)
        return await asyncio.to_thread(voix.ensure_audio, campaign, session_id, text, force, progress)
    finally:
        engine.start_loading()                           # recharge Whisper dans tous les cas
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


async def pregenerate_audio(engine, campaign, session_id, text):
    """Prépare l'audio du « précédemment » en arrière-plan (silencieux en cas d'échec)."""
    try:
        await prepare_audio(engine, campaign, session_id, text, progress=_console_progress)
        print(f"[bot] voix : audio du « précédemment » prêt pour {session_id}")
    except Exception as e:
        print(f"[bot] voix : préparation de l'audio impossible ({e})")


def limit_bytes() -> int:
    return SET["campagnes.taille_max_brut_ko"] * 1024


AFFICHER_CHOIX = [("compte rendu", "compte_rendu"), ("précédemment", "precedemment"), ("résumé global", "resume")]


CAMPAGNE_ACTIONS = ["liste", "nouvelle", "choisir", "info", "contexte", "mj", "journal", "rollback", "resumer", "joueur", "pack", "integrer", "afficher", "dicter", "audio", "audio force"]


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

        if audio_busy():
            await inter.response.send_message("La voix est en cours de préparation (synthèse vocale) : réessaie dans un instant, "
                                              "pour ne pas charger deux modèles en même temps.", ephemeral=True)
            return
        await inter.response.defer()
        channel = member.voice.channel
        engine.start_loading()              # sans effet si le modèle est déjà chargé (ou en cours)

        rec = SessionRecorder(datetime.now().strftime("%Y-%m-%d_%H%M%S"), Path(SET["sessions.dossier"]))
        live = LiveTranscriber(engine, rec.dir / "transcript_live.jsonl", rec.now_samples, rec.name_of)
        rec.on_chunk = live.offer
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
        await reply(inter, out_channel(inter), content=text[-1900:], allowed_mentions=discord.AllowedMentions.none())


    last_rule_call: dict = {}                 # serveur -> instant du dernier /regle (anti-abus)

    @bot.tree.command(name="regle", description="Demande à Gemini d'expliquer la règle évoquée dans les dernières phrases")
    @app_commands.guild_only()
    @app_commands.describe(jeu="Jeu concerné (défaut : celui défini dans config.ini, Pathfinder première édition)",
                           phrases="Nombre de dernières phrases à analyser (défaut : config.ini)")
    async def regle(inter: discord.Interaction, jeu: Optional[str] = None,
                    phrases: Optional[app_commands.Range[int, 1, 50]] = None):
        if not SET["regles.activer"]:
            await inter.response.send_message("La commande /regle est désactivée (`activer = non` dans config.ini).",
                                              ephemeral=True)
            return
        if not api_key():
            await inter.response.send_message(
                "Aucune clé d'API Gemini configurée : définis `GEMINI_API_KEY` (voir le README).", ephemeral=True)
            return
        session = bot.sessions.get(inter.guild_id)
        if session is None:
            await inter.response.send_message("Aucun enregistrement en cours.", ephemeral=True)
            return
        wait = SET["regles.delai_min_s"] - (time.monotonic() - last_rule_call.get(inter.guild_id, -1e9))
        if wait > 0:
            await inter.response.send_message(f"Patiente encore {wait:.0f} s avant de redemander une règle.",
                                              ephemeral=True)
            return
        rec, live, _vc = session
        game = (jeu or "").strip() or campaign_game() or SET["regles.jeu_par_defaut"]
        rows = await asyncio.to_thread(read_rows, rec.dir / "transcript_live.jsonl")
        lines = transcript_lines(rows, phrases or SET["regles.nb_phrases"])
        if not lines:
            await inter.response.send_message(
                f"Rien de transcrit pour l'instant.\n`{live.status_line()}`", ephemeral=True)
            return
        last_rule_call[inter.guild_id] = time.monotonic()
        target = out_channel(inter)
        await inter.response.defer(thinking=True, ephemeral=target is not None)
        try:
            answer = await asyncio.to_thread(explain_rule, lines, game)
        except RulesError as e:
            last_rule_call.pop(inter.guild_id, None)
            await inter.followup.send(f"❌ {e}", ephemeral=target is not None,
                                      allowed_mentions=discord.AllowedMentions.none())
            return
        embed = discord.Embed(title=f"🎲 Règle — {game}"[:256], description=answer[:4000], colour=discord.Colour.blurple())
        quoted = "\n".join(lines)
        embed.add_field(name="Passages analysés", value=discord.utils.escape_markdown(quoted)[-1000:], inline=False)
        embed.set_footer(text="Réponse générée par Gemini à partir d'une transcription automatique : "
                              "à vérifier dans le livre de règles.")
        await reply(inter, target, embed=embed, allowed_mentions=discord.AllowedMentions.none())


    @bot.tree.command(name="stats", description="Statistiques de la session en cours")
    @app_commands.guild_only()
    async def stats_command(inter: discord.Interaction):
        session = bot.sessions.get(inter.guild_id)
        if session is None:
            await inter.response.send_message("Aucun enregistrement en cours.", ephemeral=True)
            return
        rec, live, _vc = session
        target = out_channel(inter)
        await inter.response.defer(ephemeral=target is not None)
        stats = await asyncio.to_thread(session_stats, rec)
        await reply(inter, target,
                    embed=stats_embed(stats, f"📊 Stats en direct — session {rec.session_id}", live.pending_seconds()),
                    allowed_mentions=discord.AllowedMentions.none())


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
        if res["pack"]:
            await _say(inter, target, allowed_mentions=no_mentions, **pack_message(res["pack"]))
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
                           fichier="Fichier texte : personnages (contexte), journal existant (journal) ou transcript (resumer)")
    @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in CAMPAGNE_ACTIONS],
                          quoi=[app_commands.Choice(name=n, value=v) for n, v in AFFICHER_CHOIX])
    @app_commands.guild_only()
    async def campagne(inter: discord.Interaction, action: app_commands.Choice[str], nom: Optional[str] = None,
                       jeu: Optional[str] = None, mj: Optional[discord.Member] = None,
                       membre: Optional[discord.Member] = None, personnage: Optional[str] = None,
                       quoi: Optional[app_commands.Choice[str]] = None, rubrique: Optional[str] = None,
                       minutes: Optional[app_commands.Range[int, 0, 240]] = None,
                       session: Optional[str] = None, fichier: Optional[discord.Attachment] = None):
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
            await reply(inter, out_channel(inter), content=f"📖 Campagne active : **{c.titre}**",
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
            await reply(inter, out_channel(inter),
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
                            await reply(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(),
                                        embed=discord.Embed(title=f"📚 {title} — {c.titre}"[:256], description=body,
                                                            colour=discord.Colour.blurple()))
                        else:
                            await reply(inter, out_channel(inter), content=f"📚 {title} — **{c.titre}**",
                                        files=[discord.File(io.BytesIO(body.encode("utf-8")),
                                                            filename=f"{camp.slugify(title) or 'rubrique'}.md")],
                                        allowed_mentions=discord.AllowedMentions.none())
                        return
                    await reply(inter, out_channel(inter), content=f"📚 Résumé global — **{c.titre}**",
                                files=[discord.File(path)], allowed_mentions=discord.AllowedMentions.none())
                    return
                label, fname = ("Compte rendu", "compte_rendu.md") if what == "compte_rendu" else ("Précédemment", "precedemment.md")
                sid, text = memoire.seance_text(c, fname, (session or "").strip() or None)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            if len(text) <= 3900:
                await reply(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(), embed=discord.Embed(
                    title=f"{label} — {c.titre} ({sid})"[:256], description=text, colour=discord.Colour.blurple()))
            else:
                await reply(inter, out_channel(inter), content=f"{label} — **{c.titre}** ({sid})",
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
            wav = voix.cached_audio(c, sid, text)
            if wav is None:
                await inter.response.send_message(f"🔇 {AUDIO_NON_PRET}", **eph)
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
                await inter.followup.send(f"🎙️ Lecture du « précédemment » ({sid})…", ephemeral=True)
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
        if act in ("audio", "audio force"):
            regenerer = act == "audio force"
            if not SET["voix.activer"]:
                await inter.response.send_message("La voix est désactivée (`activer = non` dans [voix] de config.ini).", **eph)
                return
            if bot.sessions:
                await inter.response.send_message("Impossible pendant un enregistrement : Whisper et le moteur vocal ne doivent pas "
                                                  "être chargés en même temps.", **eph)
                return
            try:
                sid, text = memoire.seance_text(c, "precedemment.md", (session or "").strip() or None)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            if not regenerer and voix.cached_audio(c, sid, text) is not None:
                await inter.response.send_message(f"✅ L'audio du « précédemment » ({sid}) est déjà prêt. "
                                                  "Pour en refaire un autre : `audio force`.", **eph)
                return
            if audio_busy():
                await inter.response.send_message("Une synthèse vocale est déjà en cours.", **eph)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            try:
                progress = make_progress(inter, asyncio.get_running_loop(), f"Synthèse du « précédemment » ({sid})")
                await prepare_audio(engine, c, sid, text, force=regenerer, progress=progress)
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
            await reply(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(),
                        content=f"↩️ **{c.titre}** : version `{undone}` annulée (archivée dans `resume_global/annulees/`), "
                                f"résumé global = `{now}`." + (f" Séance `{gone}` retirée de la mémoire "
                                                               f"(`seances/_annulees/`)." if gone else ""))
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
            if not gemini_api_key():
                await inter.response.send_message("Aucune clé d'API Gemini configurée.", **eph)
                return
            if fichier is not None and fichier.size > SET["campagnes.taille_max_transcript_ko"] * 1024:
                await inter.response.send_message(
                    f"Fichier trop gros (max {SET['campagnes.taille_max_transcript_ko']} Ko, réglable dans [campagnes]).", **eph)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            try:
                if fichier is not None:
                    rows = memoire.parse_transcript_text((await fichier.read()).decode("utf-8", errors="replace"))
                    sid = (session or "").strip()
                    if not packs.RE_SESSION.match(sid):
                        await inter.followup.send("Avec un fichier joint, indique la date de la séance dans `session` "
                                                  "(ex. `2026-10-24` ou `2026-10-24_190000`).", ephemeral=True)
                        return
                else:
                    root = Path(SET["sessions.dossier"])
                    found = sorted(p.name for p in root.iterdir() if (p / "transcript_live.jsonl").exists()) \
                        if root.is_dir() else []
                    sid = (session or (found[-1] if found else "")).strip()
                    if not sid or not (root / sid / "transcript_live.jsonl").exists():
                        await inter.followup.send("Session introuvable : précise `session` ou joins un transcript.",
                                                  ephemeral=True)
                        return
                    rows = await asyncio.to_thread(_read_rows, root / sid / "transcript_live.jsonl")
                if not rows:
                    await inter.followup.send("Transcript vide ou format non reconnu.", ephemeral=True)
                    return
                out = await asyncio.to_thread(memoire.summarize_session, c, rows, sid)
            except (CampaignError, GeminiError) as e:
                await inter.followup.send(f"❌ {e}", ephemeral=True)
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
            await inter.response.defer(thinking=True, ephemeral=out_channel(inter) is not None)
            try:
                if fichier is not None:
                    rows = memoire.parse_transcript_text((await fichier.read()).decode("utf-8", errors="replace"))
                    sid = (session or "").strip()
                    if not packs.RE_SESSION.match(sid):
                        await inter.followup.send("Avec un fichier joint, indique la date de la séance dans `session` "
                                                  "(ex. `2026-10-24` ou `2026-10-24_190000`).", ephemeral=True)
                        return
                else:
                    root = Path(SET["sessions.dossier"])
                    found = sorted(p.name for p in root.iterdir() if (p / "transcript_live.jsonl").exists()) \
                        if root.is_dir() else []
                    sid = (session or (found[-1] if found else "")).strip()
                    if not sid or not (root / sid / "transcript_live.jsonl").exists():
                        await inter.followup.send("Session introuvable : précise `session` ou joins un transcript.",
                                                  ephemeral=True)
                        return
                    rows = await asyncio.to_thread(_read_rows, root / sid / "transcript_live.jsonl")
                if (c.path / "seances" / sid / "meta.json").exists():
                    await inter.followup.send(f"La séance {sid} est déjà intégrée (`rollback` pour la refaire).", ephemeral=True)
                    return
                made = await asyncio.to_thread(packs.build_packs, c, rows, sid, minutes)
            except CampaignError as e:
                await inter.followup.send(f"❌ {e}", ephemeral=True)
                return
            await reply(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(),
                        **pack_message({**made, "titre": c.titre}))
            return
        if act == "integrer":
            if fichier is None:
                await inter.response.send_message("Joins la réponse du modèle (fichier texte, paramètre `fichier`).", **eph)
                return
            if fichier.size > limit_bytes():
                await inter.response.send_message(f"Fichier trop gros (max {SET['campagnes.taille_max_brut_ko']} Ko).", **eph)
                return
            if inter.guild_id in bot.sessions:
                await inter.response.send_message("Impossible pendant un enregistrement.", **eph)
                return
            try:
                answer = (await fichier.read()).decode("utf-8", errors="replace")
                out = packs.integrate(c, answer, (session or "").strip() or None)
            except CampaignError as e:
                await inter.response.send_message(f"❌ {e}", **eph)
                return
            audio_note = ""
            if SET["voix.activer"]:
                if bot.sessions or audio_busy():
                    audio_note = "\n🔇 Audio du « précédemment » non préparé (enregistrement ou synthèse en cours) : `/campagne audio` plus tard."
                else:
                    background(pregenerate_audio(engine, c, out["seance"].name, out["precedemment"]))
                    audio_note = "\n🎙️ Audio du « précédemment » en préparation."
            note = f"\n⚠️ {out['avertissement']}" if out["avertissement"] else ""
            await inter.response.send_message(f"✅ Séance intégrée à **{c.titre}** (`{out['version']}`).{note}{audio_note}", **eph)
            await _say(inter, out_channel(inter), allowed_mentions=discord.AllowedMentions.none(), embed=discord.Embed(
                title=f"📜 Compte rendu — {c.titre}"[:256], description=out["compte_rendu"][:4000],
                colour=discord.Colour.blurple()))
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
        await reply(inter, out_channel(inter), content=f"⚙️ {inter.user.display_name} a défini `{key}` = **{fmt(cfg[key])}**",
                    allowed_mentions=discord.AllowedMentions.none())
