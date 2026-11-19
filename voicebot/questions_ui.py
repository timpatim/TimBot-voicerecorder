"""Questions de l'analyse posées dans Discord : un menu déroulant par question, réponse libre possible, jamais bloquant.

`Collecteur` (pur, testable) tient les réponses ; `poser` affiche les questions et attend les réponses de toute personne ayant la
permission « Gérer le serveur » jusqu'au bouton « Valider les réponses » (ou au délai écoulé). Tant que rien n'est validé, chaque réponse peut être changée :
même si tout est répondu, rien n'est envoyé à Claude avant la validation. Une question sans réponse reste « non précisé »
dans la version finale.
"""
import asyncio

import discord

from .analyse import JE_NE_SAIS_PAS, Question
from .memoire import hms

NSP, AUTRE = "__nsp", "__autre"
BOUTON_VALIDER = "Valider les réponses"


class Collecteur:
    """Réponses recueillies : {id de question: {'reponse', 'par'}}. Aucun accès à Discord."""

    def __init__(self, questions: list, deja: dict | None = None):
        self.questions = questions
        self.reponses: dict = {k: v for k, v in (deja or {}).items() if any(q.id == k for q in questions)}
        self.fini = asyncio.Event()                # posé par la validation seulement, jamais par la dernière réponse

    def repondre(self, qid: str, reponse: str, par: str) -> bool:
        if not any(q.id == qid for q in self.questions) or not reponse.strip():
            return False
        if self.fini.is_set():
            return False                           # déjà validé : trop tard pour changer
        self.reponses[qid] = {"reponse": reponse.strip()[:500], "par": par}
        return True

    def manquantes(self) -> list:
        return [q for q in self.questions if q.id not in self.reponses]

    def valider(self) -> None:
        self.fini.set()

    async def attendre(self, delai_s: float) -> bool:
        """True si les réponses ont été validées (bouton), False si le délai est écoulé."""
        try:
            await asyncio.wait_for(self.fini.wait(), delai_s)
            return True
        except asyncio.TimeoutError:
            return False


def reponse_du_choix(q: Question, value: str) -> str | None:
    """Texte de la réponse pour la valeur choisie dans le menu ; None pour « Autre réponse… » (fenêtre de saisie)."""
    if value == NSP:
        return JE_NE_SAIS_PAS
    if value == AUTRE:
        return None
    return q.choix[int(value)]


def texte_question(q: Question, rang: int, total: int) -> str:
    marks = " ".join(f"`{hms(t)}`" for t in q.reperes)
    out = f"**Question {rang}/{total}** — {q.texte}"
    if q.pourquoi:
        out += f"\n*Pourquoi : {q.pourquoi}*"
    if marks:
        out += f"\n*Moments de la séance : {marks}*"
    return out[:1900]


def peut_repondre(inter) -> bool:
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def _nom(inter) -> str:
    return getattr(inter.user, "display_name", None) or getattr(inter.user, "name", "joueur")


def _vue_question(col: Collecteur, q: Question, rang: int, total: int, message_ref: list, maj):
    ui = discord.ui

    async def refuse(inter):
        await inter.response.send_message("Seules les personnes ayant la permission « Gérer le serveur » peuvent répondre.", ephemeral=True)

    async def enregistrer(inter, texte: str):
        if not col.repondre(q.id, texte, _nom(inter)):
            return await inter.followup.send("Les réponses sont déjà validées.", ephemeral=True)
        if message_ref:
            await message_ref[0].edit(content=texte_question(q, rang, total) + f"\n✅ **{texte[:200]}** — {_nom(inter)} *(modifiable avant validation)*")
        await maj()

    class Saisie(ui.Modal, title="Votre réponse"):
        champ = ui.TextInput(label=q.texte[:45], style=discord.TextStyle.paragraph, max_length=400)

        async def on_submit(self, inter):
            if not peut_repondre(inter):
                return await refuse(inter)
            await inter.response.defer()
            await enregistrer(inter, str(self.champ.value))

    class Menu(ui.Select):
        def __init__(self):
            options = [discord.SelectOption(label=c[:100], value=str(i)) for i, c in enumerate(q.choix)]
            options += [discord.SelectOption(label=JE_NE_SAIS_PAS, value=NSP), discord.SelectOption(label="Autre réponse…", value=AUTRE)]
            super().__init__(placeholder="Choisir une réponse", options=options)

        async def callback(self, inter):
            if not peut_repondre(inter):
                return await refuse(inter)
            texte = reponse_du_choix(q, self.values[0])
            if texte is None:
                return await inter.response.send_modal(Saisie())
            await inter.response.defer()
            await enregistrer(inter, texte)

    view = ui.View(timeout=None)
    view.add_item(Menu())
    return view


def _vue_controle(col: Collecteur):
    ui = discord.ui

    class Valider(ui.Button):
        def __init__(self):
            super().__init__(label=BOUTON_VALIDER, style=discord.ButtonStyle.success, emoji="✅")

        async def callback(self, inter):
            if not peut_repondre(inter):
                return await inter.response.send_message("Réservé aux personnes ayant la permission « Gérer le serveur ».", ephemeral=True)
            await inter.response.defer()
            col.valider()

    view = ui.View(timeout=None)
    view.add_item(Valider())
    return view


async def poser(channel, questions: list, delai_s: float, deja: dict | None = None) -> dict:
    """Affiche les questions dans `channel`, attend la validation (ou le délai, en secondes), retourne les réponses.
    Ne lève pas d'exception."""
    col = Collecteur(questions, deja)
    n, minutes = len(questions), max(1, round(delai_s / 60))
    base = (f"❓ **{n} question{'s' if n > 1 else ''} sur la séance.** Les personnes ayant la permission « Gérer le serveur » répondent via "
            f"les menus ci-dessous (« Autre réponse… » pour écrire) et peuvent **changer d'avis tant que vous n'avez pas validé**. "
            f"Quand tout est bon, cliquez sur « {BOUTON_VALIDER} » : les questions sans réponse resteront « non précisé ». "
            f"Sans validation après {minutes} min, les réponses données seront utilisées telles quelles.")
    ctl: list = []

    async def maj():
        try:
            await ctl[0].edit(content=base + f"\n**Réponses : {len(col.reponses)}/{n}**")
        except Exception as e:
            print(f"[bot] mise à jour du message de questions impossible : {e!r}")

    ctl.append(await channel.send(base + f"\n**Réponses : {len(col.reponses)}/{n}**", view=_vue_controle(col)))
    shown: list = []
    for rang, q in enumerate(questions, 1):
        ref: list = []
        text = texte_question(q, rang, n)
        if q.id in col.reponses:
            text += f"\n✅ **{col.reponses[q.id]['reponse'][:200]}** — {col.reponses[q.id]['par']} *(réponse déjà recueillie)*"
        ref.append(await channel.send(text, view=_vue_question(col, q, rang, n, ref, maj)))
        shown.append(ref[0])
    done = await col.attendre(delai_s)
    col.valider()                                   # plus aucun changement possible
    for message in shown:                           # les menus disparaissent : les réponses sont figées
        try:
            await message.edit(view=None)
        except Exception as e:
            print(f"[bot] menu de question non retiré : {e!r}")
    try:
        await ctl[0].edit(content=("✅ Réponses validées." if done else "⌛ Délai écoulé, réponses données utilisées.")
                          + f" {len(col.reponses)}/{n} réponse(s) : finalisation en cours.", view=None)
    except Exception as e:
        print(f"[bot] mise à jour du message de questions impossible : {e!r}")
    return col.reponses
