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
