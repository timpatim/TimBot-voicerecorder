"""Commandes slash du bot : /record, /stop, /transcript, /stats, /status, /config."""
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import voice_recv

from .audio import SessionRecorder
from .config import CONFIG_SPEC, SET, load_config, parse_config_value, save_config
from .constants import RATE
from .live import LiveTranscriber
from .pipeline import PIPELINE_STEPS, StatusBoard, run_pipeline
from .stats import render_sections, session_stats
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


async def _say(inter: discord.Interaction, **kwargs):
    """Message dans le salon (pas d'expiration) ; si le bot ne peut pas y écrire, repli sur la réponse de l'interaction."""
    try:
        return await inter.channel.send(**kwargs)
    except Exception as e:
        print(f"[bot] envoi dans le salon impossible ({e!r}), repli sur la réponse de la commande")
        return await inter.followup.send(wait=True, **kwargs)


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
        await inter.followup.send(SET["discord.message_consentement"].replace("{canal}", channel.mention))


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
        await inter.response.send_message(text[-1900:], allowed_mentions=discord.AllowedMentions.none())


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
        await inter.followup.send(
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
        await inter.followup.send("⏹️ Enregistrement arrêté. Traitement en cours, suivi ci-dessous.")

        no_mentions = discord.AllowedMentions.none()
        board = StatusBoard(inter.channel, asyncio.get_running_loop(),
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
        await _say(inter, **kwargs)


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
        await inter.response.send_message(f"⚙️ {inter.user.display_name} a défini `{key}` = **{fmt(cfg[key])}**",
                                          allowed_mentions=discord.AllowedMentions.none())
