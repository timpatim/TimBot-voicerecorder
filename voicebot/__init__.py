"""Bot Discord d'enregistrement vocal : un composant par fichier.

Dépendances (chaque module ne dépend que de ceux qui le précèdent) :

    config        paramètres (config.ini)
    constants     format audio (16 kHz mono, trames de 20 ms)
    utils         formats de durée, niveau sonore
    audio         archivage WAV par utilisateur (chemin prioritaire)
    vad           détection de voix
    whisper_engine  modèle WhisperX partagé
    live          transcription en direct (VAD + WhisperX)
    transcript    transcript final fusionné
    stats         statistiques de session
    report        rapport PDF
    mp3           conversion MP3 (ffmpeg)
    drive         Google Drive
    rules         questions de règles (Google Gemini)
    pipeline      traitement de fin de session + suivi dans le chat
    client        client Discord (sessions, statut de présence)
    commands      commandes slash
"""

__version__ = "4"
