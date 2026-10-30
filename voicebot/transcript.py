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


def write_final_transcript(rec: "SessionRecorder") -> Path:
    """Fusionne les segments de tous les intervenants, triés par temps, dans transcript_live.txt."""
    jsonl = rec.dir / "transcript_live.jsonl"
    rows = read_rows(jsonl)
    rows.sort(key=lambda r: (r["start"], r["end"]))
    out = [f"# Session {rec.session_id} — démarrée le {rec.t0.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
           "# Timestamps relatifs au début de la session (même origine que les fichiers audio)", ""]
    out += [f"[{fmt_ts(r['start'])} → {fmt_ts(r['end'])}] {r['name']} : {r['text']}" for r in rows]
    if not rows:
        out.append("(aucune parole transcrite)")
    path = rec.dir / "transcript_live.txt"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path
