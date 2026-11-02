"""Synthèse vocale avec Chatterbox (modèle multilingue), lancée par le bot dans un processus séparé.

Ce script tourne dans l'environnement conda dédié créé par install_voix.bat (celui où chatterbox-tts est installé),
pas dans celui du bot : leurs versions de PyTorch peuvent différer. Au démarrage il ne dépend que de la bibliothèque
standard ; torch et chatterbox ne sont importés que dans main().

Usage : python tools/tts_chatterbox.py --in texte.txt --out sortie.wav [--lang fr] [--voice echantillon.wav] [--device auto|cuda|cpu]
"""
import argparse
import re
import sys

PAUSE_S = 0.35          # silence entre deux morceaux
TAIL_S = 0.5            # silence final : le dernier mot ne doit jamais être avalé
MAX_CHARS = 220         # Chatterbox est plus stable sur des phrases courtes


def split_text(text: str, max_chars: int = MAX_CHARS) -> list:
    """Découpe en morceaux d'au plus `max_chars` : paragraphes, puis phrases, puis virgules / mots."""
    chunks = []
    for para in re.split(r"\n\s*\n|\n", text.strip()):
        para = para.strip()
        if not para:
            continue
        current = ""
        for sentence in re.split(r"(?<=[.!?…])\s+", para):
            for piece in _hard_split(sentence, max_chars):
                if current and len(current) + 1 + len(piece) > max_chars:
                    chunks.append(current)
                    current = piece
                else:
                    current = f"{current} {piece}".strip()
        if current:
            chunks.append(current)
    return chunks


def _hard_split(sentence: str, max_chars: int) -> list:
    if len(sentence) <= max_chars:
        return [sentence]
    out, current = [], ""
    for part in re.split(r"(?<=[,;:])\s+|\s+", sentence):
        if current and len(current) + 1 + len(part) > max_chars:
            out.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        out.append(current)
    return out


def check_perth():
    """Chatterbox signe l'audio avec le filigrane « perth ». Si perth ne s'importe pas (cas typique : setuptools trop
    récent, sans pkg_resources), perth.PerthImplicitWatermarker vaut None et Chatterbox plante avec un message obscur.
    On affiche ici la vraie cause."""
    import perth
    if getattr(perth, "PerthImplicitWatermarker", None) is None:
        try:
            import perth.perth_net  # noqa: F401
        except Exception as e:
            print(f"PerthImplicitWatermarker indisponible : {e!r}. "
                  'Essayez : pip install "setuptools<81" (dans l\'environnement de la voix).', file=sys.stderr)
        else:
            print("PerthImplicitWatermarker indisponible (cause inconnue).", file=sys.stderr)
        raise SystemExit(3)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="src", required=True, help="fichier texte à lire")
    ap.add_argument("--out", required=True, help="fichier WAV à écrire")
    ap.add_argument("--lang", default="fr")
    ap.add_argument("--voice", default="", help="échantillon audio (~10 s) de la voix à imiter ; vide = voix par défaut")
    ap.add_argument("--pause", type=float, default=PAUSE_S, help="silence (s) entre deux morceaux")
    ap.add_argument("--temperature", type=float, default=0.8, help="0.8 = défaut de Chatterbox ; plus bas = plus stable")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    args = ap.parse_args(argv)

    with open(args.src, encoding="utf-8") as f:
        chunks = split_text(f.read())
    if not chunks:
        print("Texte vide.", file=sys.stderr)
        return 2

    import torch
    import torchaudio
    check_perth()
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Chargement de Chatterbox ({device})...", flush=True)
    model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    pause = torch.zeros(1, int(model.sr * args.pause))
    pieces = []
    for i, chunk in enumerate(chunks, 1):
        print(f"MORCEAU {i}/{len(chunks)}", flush=True)
        kwargs = {"language_id": args.lang, "temperature": args.temperature}
        if args.voice:
            kwargs["audio_prompt_path"] = args.voice
        wav = model.generate(chunk, **kwargs).detach().cpu()
        pieces += [wav if wav.dim() == 2 else wav.unsqueeze(0), pause]
        print(f"FAIT {i}/{len(chunks)}", flush=True)
    pieces[-1] = torch.zeros(1, int(model.sr * TAIL_S))
    torchaudio.save(args.out, torch.cat(pieces, dim=1), model.sr)
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
