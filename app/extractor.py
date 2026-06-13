"""Extracteur de sujets : transforme une memory OMI en une ligne structurée.

Tout est heuristique (gratuit, déterministe) : on s'appuie sur le résumé que
OMI a déjà produit (title, overview, action_items, category) + les
métadonnées (durée, locuteurs, ratio is_user). AUCUN appel LLM ici — la
synthèse intelligente se fait une seule fois par jour dans report.py.
"""
import datetime as dt
import json

from . import db
from .detector import normalize

# Seuil (minutes) au-delà duquel un épisode où Alex ne parle pas est
# considéré comme du média passif (film, série, TV) plutôt qu'un bruit ambiant.
MEDIA_MIN_DURATION = 40

# Indices business (sinon perso par défaut) — mots normalisés
BUSINESS_HINTS = {
    "client", "facture", "facturation", "pennylane", "jab", "maryse", "medhi",
    "devis", "projet", "reunion", "business", "vps", "agent", "deploiement",
    "marketing", "rendez vous pro", "contrat", "prospect", "chiffre", "boulot",
    "travail", "bureau", "equipe", "livrable", "site", "audit",
}


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


def classify_content_type(is_user: int, duration_min: int, n_speakers: int) -> str:
    """Type de contenu d'après la participation d'Alex et la forme."""
    if is_user == 0:
        if duration_min >= MEDIA_MIN_DURATION:
            return "media"      # long + Alex silencieux → film/série/TV
        return "ambiance"        # court + Alex silencieux → bruit de fond
    # Alex a parlé
    if n_speakers >= 3:
        return "reunion"
    if n_speakers <= 1:
        return "solo"            # Alex seul qui réfléchit/dicte
    return "conversation"


def guess_side(text: str, category: str | None) -> str:
    norm = normalize(text + " " + (category or ""))
    if any(h in norm for h in BUSINESS_HINTS):
        return "business"
    return "perso"


def extract_memory(raw_json: str, uid: str) -> dict:
    d = json.loads(raw_json)
    st = d.get("structured") or {}
    segs = d.get("transcript_segments") or []

    start = _parse(d.get("started_at"))
    end = _parse(d.get("finished_at"))
    duration_min = int((end - start).total_seconds() / 60) if (start and end) else 0

    n_speakers = len({s.get("speaker") for s in segs if s.get("speaker")})
    is_user = sum(1 for s in segs if s.get("is_user"))
    content_type = classify_content_type(is_user, duration_min, n_speakers)

    # Personnes présentes : noms si OMI les a, sinon nb de voix
    persons = sorted({s.get("speaker_name") for s in segs if s.get("speaker_name")})
    if not persons and n_speakers:
        persons = [f"{n_speakers} voix"]

    title = st.get("title") or ""
    overview = st.get("overview") or ""
    todo = [a.get("description", a) if isinstance(a, dict) else a
            for a in (st.get("action_items") or [])]

    # On ne tire ni sujet ni todo du contenu passif
    if content_type in ("media", "ambiance"):
        subject = title or f"{content_type} (~{duration_min} min)"
        todo = []
        side = "perso"
    else:
        subject = title or overview[:80] or "(sans titre)"
        side = guess_side(f"{title} {overview}", st.get("category"))

    return {
        "uid": uid,
        "memory_omi_id": str(d.get("id", "")),
        "day": start.date().isoformat() if start else None,
        "start_time": d.get("started_at"),
        "end_time": d.get("finished_at"),
        "duration_min": duration_min,
        "content_type": content_type,
        "side": side,
        "persons": json.dumps(persons, ensure_ascii=False),
        "subject": subject,
        "todo": json.dumps(todo, ensure_ascii=False),
        "omi_title": title,
        "omi_category": st.get("category"),
        "created_at": db.now_iso(),
    }


def extract_one(raw_json: str, uid: str) -> dict:
    topic = extract_memory(raw_json, uid)
    if topic["memory_omi_id"]:
        db.upsert_topic(topic)
    return topic


def backfill_all() -> int:
    """(Ré)extrait tous les sujets depuis les memories stockées."""
    n = 0
    for m in db.all_memories():
        try:
            extract_one(m["raw"], m["uid"])
            n += 1
        except Exception:  # noqa: BLE001
            continue
    return n
