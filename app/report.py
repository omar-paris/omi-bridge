"""Compte rendu quotidien : liste brute + synthèse (1 appel hermes) + to-do.

Envoyé sur Telegram. Le média passif (film, TV) est collapsé en une seule
ligne annotable — jamais retranscrit. Seules les conversations où Alex a
participé alimentent la synthèse intelligente.
"""
import asyncio
import datetime as dt
import json

from . import db, dispatch
from .config import CONFIG, resolve_user

MEANINGFUL = ("conversation", "reunion", "solo")

SYNTH_PROMPT = """Voici les conversations captées aujourd'hui ({day}) par le wearable OMI d'Alex.
Produis, en français, sans markdown, sans préambule, DIRECTEMENT le contenu :
- 2-3 phrases max sur la journée (sujets marquants, décisions).
- Puis une liste "À FAIRE" dédupliquée, puces courtes. Chaque ligne : [BIZ] ou [PERSO] + action.
- Si aucun à-faire : "À faire : rien noté".
Pas de titre, pas de remplissage, commence directement.

CONVERSATIONS :
{items}"""


def _fmt_hm(iso: str | None) -> str:
    try:
        return dt.datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return "--:--"


def build_raw_list(topics: list) -> str:
    """Liste brute : une ligne par sujet, média/ambiance regroupés."""
    lines = []
    media = [t for t in topics if t["content_type"] in ("media", "ambiance")]
    real = [t for t in topics if t["content_type"] not in ("media", "ambiance")]

    for t in real:
        persons = ", ".join(json.loads(t["persons"] or "[]"))
        tag = "💼" if t["side"] == "business" else "🏠"
        lines.append(
            f"{_fmt_hm(t['start_time'])}–{_fmt_hm(t['end_time'])} {tag} "
            f"[{t['content_type']}] {t['subject']}"
            + (f" — {persons}" if persons else "")
        )

    if media:
        total = sum(t["duration_min"] or 0 for t in media)
        annotated = [t for t in media if t["annotated"]]
        line = f"🎬 Média/passif : {len(media)} épisode(s), ~{total // 60}h{total % 60:02d} (non retranscrit)"
        for t in annotated:
            extra = " — ".join(x for x in [t["user_label"], t["user_rating"], t["user_note"]] if x)
            line += f"\n   ✏️ {extra}"
        lines.append(line)
    return "\n".join(lines) if lines else "(rien capté aujourd'hui)"


async def generate(uid: str, day: str | None = None) -> str:
    user = resolve_user(uid)
    if user is None:
        return "uid inconnu"
    if day is None:
        day = dt.date.today().isoformat()

    topics = db.topics_for_day(uid, day)
    raw = build_raw_list(topics)

    meaningful = [t for t in topics if t["content_type"] in MEANINGFUL]
    synth = "(aucune conversation à synthétiser aujourd'hui)"
    if meaningful:
        items = "\n".join(
            f"- [{t['side']}] {t['subject']} (overview: {t['omi_title'] or ''}; "
            f"à faire OMI: {', '.join(json.loads(t['todo'] or '[]')) or 'aucun'})"
            for t in meaningful
        )
        prompt = SYNTH_PROMPT.format(day=day, items=items)
        try:
            synth, _ = await dispatch.run_hermes_raw(
                prompt, {"agent": "omar"}, CONFIG["limits"]["hermes_timeout_seconds"],
            )
        except Exception as exc:  # noqa: BLE001
            synth = f"(synthèse indisponible : {exc})"

    # En-tête : 📋 CR Lundi 21/06/26
    try:
        date_obj = dt.date.fromisoformat(day)
        DAY_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        day_label = f"{DAY_FR[date_obj.weekday()]} {date_obj.strftime('%d/%m/%y')}"
    except ValueError:
        day_label = day

    report = f"📋 CR {day_label}\n\n{raw}\n\n{synth}"
    return report


async def send(uid: str, day: str | None = None) -> None:
    user = resolve_user(uid)
    if user is None:
        return
    report = await generate(uid, day)
    await dispatch.send_telegram(user["telegram_chat_id"], report)


def _main() -> None:
    import sys
    uid = sys.argv[1] if len(sys.argv) > 1 else next(
        (u["uid"] for u in CONFIG.get("users", []) if u["uid"] != "*"), "*"
    )
    day = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(send(uid, day))


if __name__ == "__main__":
    _main()
