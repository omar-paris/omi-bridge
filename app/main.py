"""omi-bridge — pont entre le wearable OMI et les agents Hermes OA.

Flux : app OMI (Developer Settings webhooks) → POST ici → détection mot-clé
("Allo Omar") → contexte de session → `hermes chat` → réponse Telegram.

Tous les endpoints vivent sous /{WEBHOOK_SECRET}/ : Caddy expose ce service en
public, la connaissance du secret fait office d'authentification (OMI ne
permet pas de headers custom sur ses webhooks).
"""
import asyncio
import logging
import re

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import db, dispatch, extractor
from .config import CONFIG, WEBHOOK_SECRET, resolve_user
from .detector import build_patterns, find_trigger, normalize
from .ui import router as ui_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("omi-bridge")

app = FastAPI(title="omi-bridge", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(ui_router, prefix="/{secret}/ui")

# Commandes vocales en cours d'accumulation : session_id -> état
# Après détection du mot-clé, on accumule les segments suivants jusqu'à
# `command_silence_seconds` sans nouveau segment, puis on dispatche.
pending: dict[str, dict] = {}

# Mode conversation actif : uid -> état (« Allo Omar conversation »)
# Chaque pause de `pause_seconds` envoie ce qui vient d'être dit à hermes
# (même session), jusqu'à « c'est parti » (exécution) ou « fin de conversation ».
conversations: dict[str, dict] = {}


def voice_ok(seg: dict) -> bool:
    """Si require_user_voice est actif, seuls les segments de la voix
    d'Alex (is_user=1, profil vocal OMI) peuvent déclencher/alimenter."""
    if not CONFIG["security"]["require_user_voice"]:
        return True
    return bool(seg.get("is_user"))


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
    # 0. Mode conversation actif → tout ce que dit Alex alimente la conversation
    conv = conversations.get(uid)
    if conv is not None:
        texts = [s["text"] for s in segments if voice_ok(s)]
        if texts:
            conv["parts"].extend(texts)
            conv["timer"].cancel()
            conv["timer"] = asyncio.create_task(conversation_tick(uid, user))
            reset_idle_timer(uid, user)
        return

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
    history = db.recent_segments(session_id, limit=6)
    # Fenêtre glissante : on concatène les 3 segments précédents pour attraper
    # les keywords tronqués par Deepgram sur plusieurs webhooks consécutifs.
    pre_batch = history[:-len(segments)] if len(history) > len(segments) else []
    previous_text = " ".join(s["text"] for s in pre_batch[-3:])

    for i, seg in enumerate(segments):
        if not voice_ok(seg):
            continue
        prev_parts = [s["text"] for s in pre_batch[-2:]] + [segments[j]["text"] for j in range(i)]
        prev = " ".join(prev_parts[-3:])
        hit = find_trigger(patterns, seg["text"], prev)
        if hit is None:
            continue
        trigger, after = hit
        cmd_id = db.create_command(session_id, uid, trigger.get("agent", "omar"))
        log.info("Trigger '%s' détecté (session %s, cmd #%d)", trigger.get("agent"), session_id, cmd_id)
        remaining = [s["text"] for s in segments[i + 1:]]
        pending[session_id] = {
            "uid": uid,
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

    # Entrée en mode conversation : « Allo Omar conversation … »
    words = normalize(command_text).split()
    if words and words[0] == "conversation":
        await enter_conversation(uid, user, trigger, cmd_id, " ".join(words[1:]), context_text)
        return

    # Annotation manuelle : « Allo Omar annote/note le film … »
    if words and words[0] in ("annote", "annoter") or words[:2] == ["note", "le"]:
        await annotate_recent(uid, user, command_text, cmd_id)
        return

    if not command_text:
        await dispatch.send_telegram(chat_id, "OMI : j'ai entendu le mot-clé mais aucune demande derrière. Reformule ?")
        db.complete_command(cmd_id, "empty", "")
        return

    try:
        # Routage vers H-Omar via webhook — H-Omar répond directement sur Telegram
        # avec son contexte complet (Kanban, mémoire, outils).
        await dispatch.run_hermes(
            command_text, context_text, trigger,
            timeout=CONFIG["limits"]["hermes_timeout_seconds"],
        )
        db.complete_command(cmd_id, "done", "webhook→h-omar")
    except Exception as exc:  # noqa: BLE001
        log.exception("Échec dispatch cmd #%d", cmd_id)
        db.complete_command(cmd_id, "error", str(exc))
        await dispatch.send_telegram(chat_id, f"OMI : échec du traitement ({exc}). Réessaie ou vérifie omi-bridge.")


# ----- Annotation manuelle d'un sujet média ------------------------------

async def annotate_recent(uid: str, user: dict, command_text: str, cmd_id: int) -> None:
    """« Allo Omar annote le film d'hier c'était Inception, 8 sur 10 »
    → attache label/note/rating au dernier sujet média non annoté."""
    chat_id = user["telegram_chat_id"]
    topic = db.recent_annotatable_topic(uid)
    if topic is None:
        await dispatch.send_telegram(chat_id, "OMI : aucun épisode média récent à annoter.")
        db.complete_command(cmd_id, "done", "no topic")
        return

    # Note chiffrée : « 8 sur 10 », « 8/10 », « note de 8 »
    rating = None
    m = re.search(r"(\d{1,2})\s*(?:sur|/)\s*(\d{1,2})", command_text)
    if m:
        rating = f"{m.group(1)}/{m.group(2)}"
    else:
        m = re.search(r"note\s+(?:de\s+)?(\d{1,2})", normalize(command_text))
        if m:
            rating = f"{m.group(1)}/10"

    # Le reste de la phrase = label + commentaire (on retire le verbe d'amorce)
    body = re.sub(r"^\s*(annote[rz]?|note le|note la)\s+", "", command_text, flags=re.I).strip()
    db.annotate_topic(topic["id"], label=body or None, note=body or None, rating=rating)
    when = topic["day"]
    await dispatch.send_telegram(
        chat_id,
        f"OMI · noté pour le média du {when} (~{topic['duration_min']} min) :\n"
        f"« {body} »" + (f"\n⭐ {rating}" if rating else ""),
    )
    db.complete_command(cmd_id, "done", f"annotated topic {topic['id']}")


# ----- Mode conversation -------------------------------------------------

def reset_idle_timer(uid: str, user: dict) -> None:
    conv = conversations.get(uid)
    if conv is None:
        return
    if conv.get("idle"):
        conv["idle"].cancel()
    conv["idle"] = asyncio.create_task(conversation_idle(uid, user))


async def enter_conversation(
    uid: str, user: dict, trigger: dict, cmd_id: int, first_words: str, context: str,
) -> None:
    chat_id = user["telegram_chat_id"]
    agent = trigger.get("agent", "omar")
    pause = CONFIG["conversation"]["pause_seconds"]
    await dispatch.send_telegram(
        chat_id,
        f"OMI · mode conversation ouvert ({trigger.get('label', agent)}). "
        f"Pause de {pause}s = réponse. « c'est parti » = exécution du plan. "
        f"« fin de conversation » = clôture.",
    )
    conversations[uid] = {
        "trigger": trigger,
        "agent": agent,
        "parts": [],
        "timer": asyncio.create_task(asyncio.sleep(0)),  # placeholder annulable
        "idle": None,
        "lock": asyncio.Lock(),
    }
    reset_idle_timer(uid, user)
    db.complete_command(cmd_id, "conversation_opened", "")

    prompt = dispatch.CONV_OPEN_TEMPLATE.format(
        keyword_label=trigger.get("label", "Allo Omar"),
        command=first_words or "(il n'a encore rien dit, salue-le brièvement et écoute)",
        context=context or "(pas de contexte)",
    )
    await conversation_hermes_reply(uid, user, prompt)


async def conversation_tick(uid: str, user: dict) -> None:
    """Une pause de pause_seconds vient de s'écouler → traiter ce qui a été dit."""
    await asyncio.sleep(CONFIG["conversation"]["pause_seconds"])
    conv = conversations.get(uid)
    if conv is None:
        return
    # Le sleep est passé : on se détache de conv["timer"] pour que l'arrivée
    # de nouveaux segments n'annule pas un traitement déjà en cours
    if conv["timer"] is asyncio.current_task():
        conv["timer"] = asyncio.create_task(asyncio.sleep(0))
    async with conv["lock"]:
        parts, conv["parts"] = conv["parts"], []
        if not parts:
            return
        utterance = " ".join(parts)
        norm = normalize(utterance)

        if any(normalize(kw) in norm for kw in CONFIG["conversation"]["launch_keywords"]):
            # SEUL chemin qui active les outils : exécution explicite « c'est parti »
            await exit_conversation(uid, user, dispatch.CONV_LAUNCH_TEMPLATE.format(command=utterance),
                                    ack="OMI · GO reçu — exécution du plan.", allow_actions=True)
            return
        if any(normalize(kw) in norm for kw in CONFIG["conversation"]["end_keywords"]):
            await exit_conversation(uid, user, dispatch.CONV_END_TEMPLATE,
                                    ack="OMI · conversation close — récap en cours.")
            return

        await conversation_hermes_reply(uid, user, dispatch.CONV_TURN_TEMPLATE.format(command=utterance))


async def conversation_hermes_reply(uid: str, user: dict, prompt: str, allow_actions: bool = False) -> None:
    conv = conversations.get(uid)
    if conv is None:
        return
    trigger, agent = conv["trigger"], conv["agent"]
    chat_id = user["telegram_chat_id"]
    try:
        await dispatch.run_hermes_raw(
            prompt, trigger, CONFIG["limits"]["hermes_timeout_seconds"],
            allow_actions=allow_actions,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Échec tour de conversation (uid %s)", uid)
        await dispatch.send_telegram(chat_id, f"OMI : raté sur ce tour ({exc}) — continue, je suis toujours là.")


async def exit_conversation(uid: str, user: dict, final_prompt: str, ack: str,
                            allow_actions: bool = False) -> None:
    conv = conversations.pop(uid, None)
    if conv is None:
        return
    conv["timer"].cancel()
    if conv.get("idle"):
        conv["idle"].cancel()
    await dispatch.send_telegram(user["telegram_chat_id"], ack)
    # La conversation n'existe plus dans `conversations`, mais la session
    # hermes est la même : on envoie le prompt final dessus
    conversations[uid] = conv  # réinsertion temporaire pour conversation_hermes_reply
    try:
        await conversation_hermes_reply(uid, user, final_prompt, allow_actions=allow_actions)
    finally:
        conversations.pop(uid, None)


async def conversation_idle(uid: str, user: dict) -> None:
    await asyncio.sleep(CONFIG["conversation"]["idle_timeout_minutes"] * 60)
    if uid in conversations:
        await exit_conversation(
            uid, user, dispatch.CONV_END_TEMPLATE,
            ack=f"OMI · {CONFIG['conversation']['idle_timeout_minutes']} min de silence — je clos la conversation, récap :",
        )


@app.post("/{secret}/webhook/memory")
async def webhook_memory(secret: str, request: Request, uid: str = Query(default="")):
    if not check_secret(secret):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    payload = await request.json()
    if isinstance(payload, dict):
        db.insert_memory(uid, payload)
        log.info("Memory reçue: %s", (payload.get("structured") or {}).get("title"))
        try:
            import json as _json
            topic = extractor.extract_one(_json.dumps(payload), uid)
            log.info("Sujet extrait: [%s/%s] %s", topic["content_type"], topic["side"], topic["subject"])
        except Exception:  # noqa: BLE001
            log.exception("Échec extraction sujet")
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
