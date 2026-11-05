"""Transcript final : lecture du JSONL et fusion de tous les intervenants, triés par temps."""
import json
from pathlib import Path

from .utils import fmt_ts


def read_rows(path: Path) -> list:
    """Lit un JSONL en ignorant une éventuelle dernière ligne incomplète (fichier en cours d'écriture)."""
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def render_final_transcript(rows: list, session_id: str, started_at: str) -> str:
    """Texte de transcript_live.txt : `started_at` = date de début lisible, `rows` triées par temps."""
    out = [f"# Session {session_id} — démarrée le {started_at}",
           "# Timestamps relatifs au début de la session (même origine que les fichiers audio)", ""]
    out += [f"[{fmt_ts(r['start'])} → {fmt_ts(r['end'])}] {r['name']} : {r['text']}" for r in rows]
    if not rows:
        out.append("(aucune parole transcrite)")
    return "\n".join(out) + "\n"


def write_final_transcript(rec: "SessionRecorder") -> Path:
    """Fusionne les segments de tous les intervenants, triés par temps, dans transcript_live.txt."""
    rows = read_rows(rec.dir / "transcript_live.jsonl")
    rows.sort(key=lambda r: (r["start"], r["end"]))
    path = rec.dir / "transcript_live.txt"
    path.write_text(render_final_transcript(rows, rec.session_id, rec.t0.astimezone().strftime("%Y-%m-%d %H:%M:%S")),
                    encoding="utf-8")
    return path
