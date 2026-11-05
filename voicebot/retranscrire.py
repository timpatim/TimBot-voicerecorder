"""Refait le transcript d'une session à partir des fichiers audio complets (un WAV par joueur).

Utile après un changement de réglages (VAD, mots ignorés, modèle Whisper...) ou quand la transcription live avait pris du
retard : les WAV sont redécoupés avec les réglages ACTUELS de config.ini puis passés à WhisperX, comme en direct.
L'ancien transcript n'est jamais écrasé : il est copié dans un sous-dossier `avant_retranscription_<date>/`.

Module indépendant de Discord (voir la commande /retranscrire).
"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from . import rejeu
from .constants import RATE
from .memoire import write_clean_transcript
from .pipeline import _active_campaign
from .stats import compute_stats, render_text
from .transcript import render_final_transcript

# fichiers dérivés du transcript, sauvegardés avant d'être remplacés
SAUVEGARDES = ("transcript_live.jsonl", "transcript_live_rejected.jsonl", "transcript_live.txt", "transcript_nettoye.txt",
               "stats.json", "stats.txt")


class RetranscribeError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def sessions_with_audio(root: Path) -> list:
    """Identifiants des sessions qui ont des fichiers audio, de la plus récente à la plus ancienne."""
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if any((p / "audio").glob("*.wav"))), reverse=True)


def _meta(session: Path) -> dict:
    try:
        return json.loads((session / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def audio_seconds(tracks: list) -> float:
    """Durée de la session = celle du plus long fichier audio."""
    longest = 0
    for _uid, _name, wav in tracks:
        try:
            longest = max(longest, (wav.stat().st_size - 44) // 2)
        except OSError:
            pass
    return longest / RATE


def backup(session: Path, stamp: str) -> Path | None:
    """Copie l'ancien transcript et ses dérivés dans `avant_retranscription_<stamp>/`. -> le dossier, ou None s'il n'y avait rien."""
    existing = [session / n for n in SAUVEGARDES if (session / n).exists()]
    if not existing:
        return None
    folder = session / f"avant_retranscription_{stamp}"
    folder.mkdir(exist_ok=True)
    for f in existing:
        shutil.copy2(f, folder / f.name)
    return folder


def retranscribe(session: Path, engine, progress=None, params: rejeu.Params | None = None, now: datetime | None = None) -> dict:
    """Retranscrit `session` (dossier) avec `engine` (WhisperX prêt). Appel bloquant : à lancer dans un thread.

    `progress(texte)` est appelé au fil du travail. -> {'lignes', 'mots', 'rejetes', 'duree_s', 'sauvegarde', 'ancien_nb_lignes',
    'asr_s', 'audio_s'}. Rien n'est modifié si la transcription échoue."""
    say = progress or (lambda _t: None)
    tracks = rejeu.session_tracks(session)
    if not tracks:
        raise RetranscribeError(f"Aucun fichier audio dans `{session / 'audio'}`.")
    params = params or rejeu.from_config()
    meta = _meta(session)
    names = {str(uid): name for uid, name, _wav in tracks}
    old_rows = _read_jsonl(session / "transcript_live.jsonl")

    res = rejeu.transcribe_variant(params, tracks, engine, progress=say)               # long ; ne touche à aucun fichier
    # --- tout est prêt : on sauvegarde l'ancien puis on remplace ---
    say("Écriture du transcript")
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    saved = backup(session, stamp)
    uid_of = {name: str(uid) for uid, name, _wav in tracks}
    rows = [{"user": uid_of.get(r["name"], r["name"]), "name": r["name"], "start": r["start"], "end": r["end"],
             "text": r["text"], "source": "rejeu"} for r in res["rows"]]
    rejected = [{"user": uid_of.get(r["name"], r["name"]), "name": r["name"], "start": r["start"], "end": r["end"],
                 "text": r["text"], "source": "rejeu", "reason": r["reason"]} for r in res["rejetes"]]
    _write_jsonl(session / "transcript_live.jsonl", rows)
    _write_jsonl(session / "transcript_live_rejected.jsonl", rejected)
    session_id = meta.get("sessionId") or session.name
    started = meta.get("startedAt") or ""
    try:
        started = datetime.fromisoformat(started).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    (session / "transcript_live.txt").write_text(render_final_transcript(rows, session_id, started), encoding="utf-8")
    write_clean_transcript(rows, session / "transcript_nettoye.txt", f"Session {session_id}",
                           _active_campaign(SimpleNamespace(dir=session)))
    samples = meta.get("durationSamples") or 0
    duration = samples / RATE if samples > 0 else audio_seconds(tracks)
    stats = compute_stats(rows, duration, names)
    (session / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    (session / "stats.txt").write_text(render_text(stats), encoding="utf-8")
    return {"lignes": len(rows), "mots": stats["words"], "rejetes": len(rejected), "duree_s": duration,
            "sauvegarde": saved, "ancien_nb_lignes": len(old_rows), "asr_s": res["stats_asr"]["asr_s"],
            "audio_s": res["stats_asr"]["audio_s"], "stats": stats}


def _read_jsonl(path: Path) -> list:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
