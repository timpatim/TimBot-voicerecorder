"""Client Discord : sessions en cours, statut de présence et suivi console."""
import asyncio
import time

import discord
from discord import app_commands

from .config import SET
from .constants import RATE
from .utils import fmt_ts


class RecorderBot(discord.Client):
    def __init__(self, engine):
        super().__init__(intents=discord.Intents.default())   # inclut voice_states
        self.tree = app_commands.CommandTree(self)
        self.sessions: dict[int, tuple] = {}                  # guild_id -> (rec, live, vc)
        self.engine = engine
        self._presence_task = None

    async def setup_hook(self):
        guild_id = SET["discord.guild_id"]
        if guild_id:   # sync instantané sur un serveur (les commandes globales peuvent être lentes à apparaître)
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        print(f"[bot] commandes synchronisées : {[c.name for c in synced]}")

    async def on_ready(self):
        print(f"[bot] connecté en tant que {self.user}, serveurs : {[g.name for g in self.guilds]}")
        if self._presence_task is None:
            self._presence_task = asyncio.create_task(self._presence_loop())

    def _presence(self):
        """(couleur, texte) du statut du bot : état de Whisper, enregistrement et file ASR."""
        if self.engine.failed:
            return discord.Status.dnd, "❌ Whisper indisponible (voir console)"
        if self.sessions:
            rec = next(iter(self.sessions.values()))[0]
            minutes = int(rec.now_samples() / RATE // 60)
            text = f"🔴 REC {minutes} min"
            if len(self.sessions) > 1:
                text += f" ({len(self.sessions)} serveurs)"
            if not self.engine.ready:
                return discord.Status.dnd, f"{text} · Whisper {self.engine.state_text}"
            pend = max(live.pending_seconds() for _, live, _ in self.sessions.values())
            warn = SET["live.retard_alerte_s"]
            return discord.Status.dnd, f"{text} · file ASR {pend:.0f} s" + (" ⚠" if pend > warn else "")
        if not self.engine.ready:
            return discord.Status.idle, f"⏳ Whisper {self.engine.state_text}"
        return discord.Status.online, "✅ Whisper prêt · /record pour démarrer"

    def _console_status(self, text: str, key: tuple, state: dict):
        """Console : une ligne à chaque changement d'état (Whisper prêt, début/fin de REC...),
        puis, pendant un enregistrement, un point régulier avec la file ASR."""
        interval = SET["live.statut_console_s"]                       # 0 = seulement les changements d'état
        now = time.monotonic()
        if key != state.get("key"):
            print(f"[bot] statut : {text}")
            state["key"], state["t"] = key, now
        elif self.sessions and interval > 0 and now - state["t"] >= interval:
            for rec, live, _vc in self.sessions.values():
                print(f"[bot] REC {fmt_ts(rec.now_samples() / RATE)[:8]} · {len(rec.names)} participant(s) · "
                      f"{live.status_line()}")
            state["t"] = now

    async def _presence_loop(self):
        last_text = last_status = None
        last_t = 0.0
        console_state: dict = {}
        while not self.is_closed():
            status, text = self._presence()
            now = time.monotonic()
            self._console_status(text, (status, self.engine.state, bool(self.sessions)), console_state)
            # changement de couleur = tout de suite ; simple changement de texte = au plus toutes les N s
            if text != last_text and (status != last_status or now - last_t >= SET["live.statut_discord_texte_s"]):
                try:
                    await self.change_presence(status=status, activity=discord.Game(name=text))
                    last_text, last_status, last_t = text, status, now
                except Exception as e:
                    print(f"[bot] statut non mis à jour : {e!r}")
            await asyncio.sleep(SET["live.statut_scrutation_s"])

    def close_all(self):
        """Fermeture propre des fichiers audio si le programme s'arrête pendant un enregistrement."""
        for rec, live, _vc in list(self.sessions.values()):
            rec.stop()
            live.stop()
