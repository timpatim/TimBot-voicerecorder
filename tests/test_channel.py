"""Salon dédié aux messages du bot (discord.canal_messages)."""
import asyncio
import types

import pytest

from voicebot import commands
from voicebot.config import SET


class Chan:
    def __init__(self, cid, can_send=True, fail=False):
        self.id, self.sent, self.fail = cid, [], fail
        self.mention = f"<#{cid}>"
        self._perms = types.SimpleNamespace(view_channel=can_send, send_messages=can_send)

    def permissions_for(self, member): return self._perms

    async def send(self, **kw):
        if self.fail:
            raise RuntimeError("Missing Permissions")
        self.sent.append(kw)


class Inter:
    def __init__(self, channels, channel_id=1):
        self.guild = types.SimpleNamespace(get_channel=lambda i: channels.get(i), me=object())
        self.channel_id, self.out, self.follow, self._done = channel_id, [], [], False
        self.response = types.SimpleNamespace(send_message=self._send, is_done=lambda: self._done)
        self.followup = types.SimpleNamespace(send=self._follow)

    async def _send(self, content=None, **kw): self.out.append((content, kw)); self._done = True
    async def _follow(self, content=None, **kw): self.follow.append((content, kw))


@pytest.fixture(autouse=True)
def reset(cfg_file):
    commands._warned_channels.clear()


def configure(monkeypatch, value):
    monkeypatch.setitem(SET.values, "discord.canal_messages", value)


def test_sans_reglage_on_repond_la_ou_la_commande_est_lancee(monkeypatch):
    inter = Inter({})
    assert commands.out_channel(inter) is None
    asyncio.run(commands.reply(inter, None, content="salut"))
    assert inter.out == [("salut", {})]


def test_salon_dedie_et_confirmation_privee(monkeypatch):
    dedie = Chan(99)
    configure(monkeypatch, "99")
    inter = Inter({99: dedie})
    ch = commands.out_channel(inter)
    assert ch is dedie
    asyncio.run(commands.reply(inter, ch, content="résultat"))
    assert dedie.sent == [{"content": "résultat"}]
    content, kw = inter.out[0]
    assert "<#99>" in content and kw["ephemeral"] is True


def test_confirmation_en_followup_apres_defer(monkeypatch):
    dedie = Chan(99)
    configure(monkeypatch, "99")
    inter = Inter({99: dedie})
    inter._done = True
    asyncio.run(commands.reply(inter, dedie, embed="E"))
    assert dedie.sent == [{"embed": "E"}] and inter.follow[0][1]["ephemeral"] is True


def test_deja_dans_le_bon_salon(monkeypatch):
    configure(monkeypatch, "99")
    assert commands.out_channel(Inter({99: Chan(99)}, channel_id=99)) is None


@pytest.mark.parametrize("channels", [{}, {99: Chan(99, can_send=False)}])
def test_salon_introuvable_ou_inaccessible_avertit_une_fois(monkeypatch, capsys, channels):
    configure(monkeypatch, "99")
    inter = Inter(channels)
    assert commands.out_channel(inter) is None and commands.out_channel(inter) is None
    assert capsys.readouterr().out.count("salon d'origine") == 1


def test_echec_d_envoi_repli_sur_le_salon_d_origine(monkeypatch):
    dedie = Chan(99, fail=True)
    configure(monkeypatch, "99")
    inter = Inter({99: dedie})
    asyncio.run(commands.reply(inter, dedie, content="x"))
    assert inter.out == [("x", {})]


def test_identifiant_invalide_refuse():
    from voicebot.config import parse_setting
    with pytest.raises(ValueError):
        parse_setting("discord.canal_messages", "#general")
    assert parse_setting("discord.canal_messages", " 123456 ") == "123456"
    assert parse_setting("discord.canal_messages", "") == ""


# --- répartition : demandes d'utilisateurs / messages système --------------------------------------

class Member:
    display_name = "Couette"


def test_ligne_de_commande_sans_contenu_de_fichier():
    inter = Inter({})
    inter.user, inter.command = Member(), types.SimpleNamespace(name="campagne", qualified_name="campagne")
    inter.namespace = [("action", "resumer"), ("session", "2026-10-24"), ("fichier", types.SimpleNamespace(filename="t.txt")),
                       ("mj", Member()), ("rubrique", None), ("quoi", types.SimpleNamespace(value="resume"))]
    assert commands.command_line(inter) == "/campagne action:resumer session:2026-10-24 fichier:t.txt mj:Couette quoi:resume"


def test_trace_dans_le_salon_systeme_seulement(monkeypatch):
    systeme = Chan(99)
    configure(monkeypatch, "99")
    inter = Inter({99: systeme})
    inter.user, inter.command, inter.namespace = Member(), types.SimpleNamespace(name="regle"), [("question", "Un jet de dés ?")]
    asyncio.run(commands.trace_command(inter))
    assert systeme.sent[0]["content"] == "▶️ **Couette** : `/regle question:Un jet de dés ?`" and inter.out == []
    # dans le salon système lui-même, ou sans salon configuré : pas de trace
    systeme.sent.clear()
    inter2 = Inter({99: systeme}, channel_id=99)
    inter2.user, inter2.command, inter2.namespace = Member(), types.SimpleNamespace(name="regle"), []
    asyncio.run(commands.trace_command(inter2))
    configure(monkeypatch, "")
    asyncio.run(commands.trace_command(inter))
    assert systeme.sent == []


def test_echec_de_trace_ne_bloque_pas(monkeypatch, capsys):
    configure(monkeypatch, "99")
    inter = Inter({99: Chan(99, fail=True)})
    inter.user, inter.command, inter.namespace = Member(), types.SimpleNamespace(name="stats"), []
    asyncio.run(commands.trace_command(inter))
    assert "trace de la commande impossible" in capsys.readouterr().out


def test_l_arbre_des_commandes_trace_avant_chaque_commande(monkeypatch):
    from tests.test_commands import build
    bot, _ = build()
    systeme = Chan(99)
    configure(monkeypatch, "99")
    inter = Inter({99: systeme})
    inter.user, inter.command, inter.namespace = Member(), types.SimpleNamespace(name="status"), []
    assert asyncio.run(bot.tree.interaction_check(inter)) is True
    assert len(systeme.sent) == 1 and "/status" in systeme.sent[0]["content"]


def test_reponses_aux_demandes_dans_le_salon_de_la_commande(monkeypatch, tmp_path):
    """/campagne afficher, rollback et /config ne passent plus par le salon des messages système."""
    from voicebot import campagne
    from tests.test_campagne import FakeInter, MJ, P1, make_bot
    monkeypatch.setitem(SET.values, "campagnes.dossier", str(tmp_path / "camp"))
    c = campagne.CampaignStore(tmp_path / "camp").create("Camp", "Pathfinder", MJ, [MJ, P1])
    campagne.add_version(c, "# v1", "un")
    campagne.add_version(c, "# v2", "deux")
    systeme = Chan(99)
    configure(monkeypatch, "99")
    bot = make_bot()
    sent_in_command_channel = []

    class I(FakeInter):
        async def _send(self, content=None, **kw):
            sent_in_command_channel.append((content, kw))
            await super()._send(content, **kw)
    inter = I()
    inter.guild = types.SimpleNamespace(get_channel=lambda i: {99: systeme}.get(i), me=object())
    asyncio.run(bot.tree.commands["campagne"](inter, types.SimpleNamespace(value="afficher"),
                                               quoi=types.SimpleNamespace(value="resume")))
    asyncio.run(bot.tree.commands["campagne"](inter, types.SimpleNamespace(value="rollback")))
    assert systeme.sent == [] and len(sent_in_command_channel) == 2
    assert sent_in_command_channel[1][1].get("ephemeral") is True and "annulée" in sent_in_command_channel[1][0]
    asyncio.run(bot.tree.commands["config"](inter, types.SimpleNamespace(value="mp3"), "non"))
    assert systeme.sent == [] and sent_in_command_channel[-1][1].get("ephemeral") is True
