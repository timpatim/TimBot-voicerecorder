"""Doublures minimales de discord.py / discord-ext-voice-recv, pour tester sans les installer."""
import sys
import types


def install():
    d = types.ModuleType("discord")

    class Client:
        def __init__(self, intents=None):
            self.intents = intents

        def is_closed(self):
            return False

    d.Client = Client
    d.Intents = types.SimpleNamespace(default=lambda: object())
    d.Interaction = type("Interaction", (), {})
    d.Attachment = type("Attachment", (), {})
    d.Member = type("Member", (), {})
    d.File = lambda path, **kw: ("file", str(path))
    d.FFmpegPCMAudio = lambda path, **kw: ("ffmpeg", str(path))
    d.Embed = type("Embed", (), {
        "__init__": lambda s, **k: [s.__dict__.update(k), setattr(s, "fields", [])] and None,
        "add_field": lambda s, **k: s.fields.append(k),
        "set_footer": lambda s, **k: None})
    d.Colour = types.SimpleNamespace(blurple=lambda: 0)
    d.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    d.Status = types.SimpleNamespace(idle="idle", dnd="dnd", online="online")
    d.Game = lambda name: name
    d.Object = lambda id: id
    d.utils = types.SimpleNamespace(escape_markdown=lambda s: s)

    a = types.ModuleType("discord.app_commands")

    class Tree:
        def __init__(self, client):
            self.commands, self.error_handler = {}, None

        def command(self, name, description=""):
            def deco(fn):
                fn.description = description
                fn.autocompletes = {}
                fn.autocomplete = lambda param: (lambda cb: (fn.autocompletes.__setitem__(param, cb), cb)[1])
                self.commands[name] = fn
                return fn
            return deco

        def error(self, fn):
            self.error_handler = fn
            return fn

    class Generic:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, *args, **kwargs):
            pass

    a.CommandTree, a.AppCommandError = Tree, Exception
    a.Range, a.Choice = Generic, Generic
    def describe(**kwargs):
        def deco(fn):
            fn.param_descriptions = kwargs
            return fn
        return deco

    a.describe = describe
    a.choices = a.guild_only = lambda *args, **kwargs: (lambda fn: fn)
    d.app_commands = a

    ext = types.ModuleType("discord.ext")
    vr = types.ModuleType("discord.ext.voice_recv")
    vr.AudioSink = type("AudioSink", (), {"__init__": lambda s: None})
    vr.VoiceRecvClient = object
    ext.voice_recv = vr
    d.ext = ext
    sys.modules.update({"discord": d, "discord.app_commands": a, "discord.ext": ext,
                        "discord.ext.voice_recv": vr})
