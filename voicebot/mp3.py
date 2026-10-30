"""Conversion des fichiers WAV en MP3 avec ffmpeg."""
import os
import shutil
import subprocess

from .config import SET


def convert_to_mp3(rec: "SessionRecorder", kbps: int, progress=None) -> list:
    """WAV -> MP3 mono avec ffmpeg, dans <session>/audio_mp3/. Les WAV sont conservés."""
    ffmpeg = shutil.which(SET["mp3.ffmpeg"])
    if not ffmpeg:
        raise RuntimeError("ffmpeg introuvable (installe-le, ou renseigne [mp3] ffmpeg dans config.ini)")
    wavs = sorted((rec.dir / "audio").glob("*.wav"))
    if not wavs:
        raise RuntimeError("aucun fichier audio à convertir")
    out_dir = rec.dir / "audio_mp3"
    out_dir.mkdir(exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    out = []
    for i, wav in enumerate(wavs, 1):
        if progress:
            progress(i, len(wavs), wav.stem)
        mp3 = out_dir / f"{wav.stem}.mp3"
        r = subprocess.run(
            [ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", str(wav),
             "-codec:a", "libmp3lame", "-b:a", f"{kbps}k", "-ac", "1", str(mp3)],
            capture_output=True, text=True, creationflags=flags, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg a échoué sur {wav.name} : {r.stderr.strip()[-200:]}")
        out.append(mp3)
    return out
