"""Questions de règles : envoie les dernières phrases du transcript à Google Gemini.

Module indépendant de Discord : `explain_rule(lines, jeu)` renvoie le texte de la réponse ou lève `RulesError`
(= GeminiError) avec un message lisible. L'appel à Gemini lui-même est dans `gemini.py`.
"""
from .config import SET
from .gemini import GeminiError, api_key, generate

RulesError = GeminiError          # nom historique, conservé pour les appelants et les tests
__all__ = ["RulesError", "api_key", "transcript_lines", "build_instruction", "build_contents", "explain_rule", "explain_question"]


def transcript_lines(rows: list, n: int) -> list:
    """Les n dernières phrases (ordre chronologique), au format « Nom : texte »."""
    rows = [r for r in sorted(rows, key=lambda r: r.get("start", 0.0)) if str(r.get("text", "")).strip()]
    return [f"{r.get('name', '?')} : {str(r['text']).strip()}" for r in rows[-n:]]


def build_instruction(jeu: str) -> str:
    return SET["regles.instruction"].replace("{jeu}", jeu)


def build_contents(lines: list, jeu: str) -> str:
    return (f"Jeu : {jeu}\n\nDernières phrases prononcées à la table (la plus récente en dernier) :\n"
            + "\n".join(lines))


def explain_rule(lines: list, jeu: str) -> str:
    """Appel bloquant à Gemini (à lancer dans un thread). Renvoie la réponse en texte brut."""
    if not api_key():
        generate("", "")                       # lève l'erreur « clé manquante » (message unique)
    if not lines:
        raise RulesError("Aucune phrase transcrite à analyser pour l'instant.")
    return generate(build_instruction(jeu), build_contents(lines, jeu))


def explain_question(question: str, jeu: str) -> str:
    """Question libre posée à Gemini, sans transcript (appel bloquant, à lancer dans un thread)."""
    if not api_key():
        generate("", "")
    if not question.strip():
        raise RulesError("Question vide.")
    return generate(SET["regles.instruction_question"].replace("{jeu}", jeu), f"Jeu : {jeu}\n\nQuestion : {question.strip()}")
