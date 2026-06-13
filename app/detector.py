"""Détection des mots-clés déclencheurs ("Allo Omar") dans les transcripts.

Deepgram produit des variantes en français : "Allo", "Allô", "à l'eau", "Hello".
On normalise (minuscules, sans accents ni ponctuation) puis on cherche les
keywords de chaque trigger dans la fenêtre courante (segment courant + fin du
segment précédent, pour couvrir un mot-clé à cheval sur deux segments).
"""
import re
import unicodedata


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def build_patterns(triggers: list[dict]) -> list[tuple[dict, list[str]]]:
    """Pré-normalise les keywords de chaque trigger de la config."""
    return [
        (trig, [normalize(kw) for kw in trig.get("keywords", [])])
        for trig in triggers
    ]


def find_trigger(
    patterns: list[tuple[dict, list[str]]],
    current_text: str,
    previous_text: str = "",
) -> tuple[dict, str] | None:
    """Cherche un mot-clé. Retourne (trigger_config, texte_apres_keyword) ou None.

    La recherche se fait sur previous_tail + current pour attraper un keyword
    coupé entre deux segments ; le texte retourné est ce qui suit le keyword.
    """
    prev_tail = normalize(previous_text)[-60:] if previous_text else ""
    window = (prev_tail + " " + normalize(current_text)).strip()
    for trig, keywords in patterns:
        for kw in keywords:
            idx = window.find(kw)
            if idx >= 0:
                after = window[idx + len(kw):].strip(" ,.")
                return trig, after
    return None
