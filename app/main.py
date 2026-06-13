"""omi-bridge — pont entre le wearable OMI et les agents Hermes OA.

Flux : app OMI (Developer Settings webhooks) → POST ici → détection mot-clé
("Allo Omar") → contexte de session → `hermes chat` → réponse Telegram.

Tous les endpoints vivent sous /{WEBHOOK_SECRET}/ : Caddy expose ce service en
public, la connaissance du secret fait office d'authentification (OMI ne
permet pas de headers custom sur ses webhooks).
"""
import asyncio
import logging

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import db, dispatch
from .config import CONFIG, WEBHOOK_SECRET, resolve_user
from .detector import build_patterns, find_trigger, normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("omi-bridge")

app = FastAPI(title="omi-bridge", docs_url=None, redoc_url=None, openapi_url=None)

# Commandes vocales en cours d'accumulation : session_id -> état
# Après détection du mot-clé, on accumule les segments suivants jusqu'à
# `command_silence_seconds` sans nouveau segment, puis on dispatche.
pending: dict[str, dict] = {}


@app.on_event("startup")
async def startup() -> None:
    db.init_db()
    log.info("omi-bridge démarré — %d user(s) configurés", len(CONFIG.get("users", [])))


def check_secret(secret: str) -> bool:
    return secret == WEBHOOK_SECRET


@app.get("/{secret}/health")
async def health(secret: str):
    if not check_secret(secret):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return {"status": "ok", **db.stats()}


@app.post("/{secret}/webhook/transcript")
async def webhook_transcript(
    secret: str,
    request: Request,
    session_id: str = Query(default=""),
    uid: str = Query(default=""),
):
    if not check_secret(secret):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    payload = await request.json()
    # OMI envoie soit {"segments": [...]}, soit un tableau nu selon les versions
    segments = payload.get("segments", []) if isinstance(payload, dict) else payload
    if not isinstance(segments, list):
        segments = []

    user = resolve_user(uid)
    if user is None:
        log.warning("uid inconnu, ignoré: %s", uid)
        return {"status": "ignored"}

    new_segments = []
    for seg in segments:
        if not isinstance(seg, dict) or not seg.get("text", "").strip():
            continue
        if db.insert_segment(session_id, uid, seg):
            new_segments.append(seg)

    if new_segments:
        # Traitement async : OMI attend une réponse rapide
        asyncio.create_task(
            process_segments(session_id, uid, user, new_segments)
        )
    return {"status": "ok", "new": len(new_segments)}


async def process_segments(session_id: str, uid: str, user: dict, segments: list[dict]) -> None:
    state = pending.get(session_id)

    # 1. Une commande est en cours d'accumulation → on ajoute et on repousse le timer
    if state is not None:
        state["parts"].extend(s["text"] for s in segments)
        state["timer"].cancel()
        state["timer"] = asyncio.create_task(
            finalize_after_silence(session_id, uid, user)
        )
        return

    # 2. Sinon, détection de mot-clé sur les nouveaux segments
    patterns = build_patterns(user.get("triggers", []))
    history = db.recent_segments(session_id, limit=3)
    previous_text = history[-len(segments) - 1]["text"] if len(history) > len(segments) else ""

    for i, seg in enumerate(segments):
        prev = segments[i - 1]["text"] if i > 0 else previous_text
        hit = find_trigger(patterns, seg["text"], prev)
        if hit is None:
            continue
        trigger, after = hit
        cmd_id = db.create_command(session_id, uid, trigger.get("agent", "omar"))
        log.info("Trigger '%s' détecté (session %s, cmd #%d)", trigger.get("agent"), session_id, cmd_id)
        remaining = [s["text"] for s in segments[i + 1:]]
        pending[session_id] = {
            "cmd_id": cmd_id,
            "trigger": trigger,
            "parts": ([after] if after else []) + remaining,
            "timer": asyncio.create_task(
                finalize_after_silence(session_id, uid, user)
            ),
        }
        return


async def finalize_after_silence(session_id: str, uid: str, user: dict) -> None:
    """Attend le silence post-commande puis dispatche vers Hermes."""
    await asyncio.sleep(CONFIG["context"]["command_silence_seconds"])
    state = pending.pop(session_id, None)
    if state is None:
        return

    command_text = " ".join(p for p in state["parts"] if p).strip()
    trigger = state["trigger"]
    cmd_id = state["cmd_id"]
    chat_id = user["telegram_chat_id"]

    # Contexte : les N derniers segments avant la commande
    n_ctx = CONFIG["context"]["pre_trigger_segments"]
    rows = db.recent_segments(session_id, limit=n_ctx + len(state["parts"]) + 2)
    cmd_norm = normalize(command_text)[:40]
    context_lines = []
    for r in rows:
        line = f"[{r['speaker'] or '?'}{' (Alex)' if r['is_user'] else ''}] {r['text']}"
        # On exclut du contexte les segments qui font partie de la commande elle-même
        if cmd_norm and cmd_norm in normalize(r["text"]):
            continue
        context_lines.append(line)
    context_text = "\n".join(context_lines[-n_ctx:])

    db.finalize_command(cmd_id, command_text, context_text)

    if not command_text:
        await dispatch.send_telegram(chat_id, "OMI : j'ai entendu le mot-clé mais aucune demande derrière. Reformule ?")
        db.complete_command(cmd_id, "empty", "")
        return

    await dispatch.send_telegram(chat_id, f"OMI · reçu : « {command_text} » — je traite.")
    try:
        # Conversation continue : on reprend la session hermes précédente
        # du même agent si elle est assez récente
        agent = trigger.get("agent", "omar")
        resume = db.get_hermes_session(uid, agent, CONFIG["context"]["session_resume_hours"])
        response, hermes_sid = await dispatch.run_hermes(
            command_text, context_text, trigger,
            timeout=CONFIG["limits"]["hermes_timeout_seconds"],
            resume_session=resume,
        )
        if hermes_sid:
            db.save_hermes_session(uid, agent, hermes_sid)
        db.complete_command(cmd_id, "done", response)
        await dispatch.send_telegram(chat_id, response or "(réponse vide de l'agent)")
    except Exception as exc:  # noqa: BLE001
        log.exception("Échec dispatch cmd #%d", cmd_id)
        db.complete_command(cmd_id, "error", str(exc))
        await dispatch.send_telegram(chat_id, f"OMI : échec du traitement ({exc}). Réessaie ou vérifie omi-bridge.")


@app.post("/{secret}/webhook/memory")
async def webhook_memory(secret: str, request: Request, uid: str = Query(default="")):
    if not check_secret(secret):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    payload = await request.json()
    if isinstance(payload, dict):
        db.insert_memory(uid, payload)
        log.info("Memory reçue: %s", (payload.get("structured") or {}).get("title"))
    return {"status": "ok"}


@app.post("/{secret}/webhook/day-summary")
async def webhook_day_summary(secret: str, request: Request, uid: str = Query(default="")):
    if not check_secret(secret):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    payload = await request.json()
    if isinstance(payload, dict):
        db.insert_day_summary(uid, payload)
    return {"status": "ok"}


# OMI peut sonder l'URL en GET pour la valider dans Developer Settings
@app.get("/{secret}/webhook/{kind}")
async def webhook_probe(secret: str, kind: str):
    if not check_secret(secret):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return {"status": "ok"}
