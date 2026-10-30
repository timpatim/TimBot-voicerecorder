"""
Bot Discord : enregistre un canal vocal (un fichier WAV par utilisateur), transcrit en quasi temps réel avec
WhisperX, puis produit en fin de session MP3, rapport PDF, dépôt Google Drive et statistiques.

Point d'entrée : python bot_v4.py [--config chemin/vers/config.ini]

Le code est organisé en composants dans le paquet voicebot/ (voir voicebot/__init__.py). Tous les paramètres
sont dans config.ini (créé au premier lancement). Le jeton Discord se passe de préférence par la variable
d'environnement DISCORD_TOKEN. Commandes : /record, /stop, /transcript, /stats, /status, /config.
"""
import atexit
import sys

from voicebot import __version__
from voicebot.client import RecorderBot
from voicebot.commands import register
from voicebot.config import SET, load_config
from voicebot.drive import DriveClient
from voicebot.whisper_engine import WhisperEngine


def main():
    engine = WhisperEngine()
    drive = DriveClient()
    bot = RecorderBot(engine)
    register(bot, engine, drive)
    atexit.register(bot.close_all)      # ferme proprement les fichiers audio si le programme s'arrête en REC

    print(f"[bot] version {__version__} — configuration : {SET.path.resolve()}")
    engine.start_loading()              # le modèle se charge dès le démarrage, en parallèle de la connexion à Discord
    if load_config()["drive"]:
        drive.start()                   # connexion Google Drive en arrière-plan (navigateur au tout premier lancement)

    token = SET["discord.token"]
    if not token:
        sys.exit("Aucun jeton Discord : définis la variable d'environnement DISCORD_TOKEN "
                 f"(ou [discord] token dans {SET.path}).")
    bot.run(token)


if __name__ == "__main__":
    main()
