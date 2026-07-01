"""UI web minimaliste pour consulter et rechercher les données omi-bridge.

Accessible sur : https://omi.omar.paris/{SECRET}/ui[/segments|/topics|/commands|/memories]
Protégé par le même webhook secret que tous les autres endpoints.
"""
import json
import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from . import db

router = APIRouter()

# ── CSS + layout commun ──────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #111; color: #e0e0e0; font-size: 14px; }
a { color: #7eb8f7; text-decoration: none; }
a:hover { text-decoration: underline; }

nav { background: #1a1a2e; padding: 12px 20px; display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
nav .brand { color: #fff; font-weight: bold; font-size: 15px; margin-right: 10px; }
nav a { color: #aaa; padding: 4px 10px; border-radius: 4px; }
nav a.active, nav a:hover { background: #2a2a4a; color: #fff; }

.container { max-width: 1100px; margin: 0 auto; padding: 20px; }
h2 { margin-bottom: 16px; color: #fff; font-size: 16px; }

.search-bar { display: flex; gap: 8px; margin-bottom: 16px; }
.search-bar input { flex: 1; background: #1e1e1e; border: 1px solid #333; color: #e0e0e0;
  padding: 8px 12px; border-radius: 4px; font-size: 14px; }
.search-bar input:focus { outline: none; border-color: #7eb8f7; }
.search-bar button { background: #2a4a7f; color: #fff; border: none; padding: 8px 16px;
  border-radius: 4px; cursor: pointer; }
.search-bar button:hover { background: #3a5a9f; }
.search-bar select { background: #1e1e1e; border: 1px solid #333; color: #e0e0e0;
  padding: 8px 10px; border-radius: 4px; }

.card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 6px;
  padding: 14px 16px; margin-bottom: 10px; }
.card .meta { color: #666; font-size: 12px; margin-bottom: 4px; }
.card .text { line-height: 1.5; }
.card.alex { border-left: 3px solid #4caf50; }
.card.trigger { border-left: 3px solid #ff9800; }

.tag { display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 10px; margin: 0 3px 3px 0; }
.tag.business { background: #1a3a5c; color: #7eb8f7; }
.tag.perso { background: #2a1a3c; color: #b07ef7; }
.tag.conversation { background: #1a3c1a; color: #7ef77e; }
.tag.reunion { background: #3c2a1a; color: #f7c07e; }
.tag.media { background: #2a2a2a; color: #aaa; }
.tag.ambiance { background: #2a2a2a; color: #888; }

.day-header { color: #fff; font-size: 15px; font-weight: bold; margin: 20px 0 10px;
  padding-bottom: 6px; border-bottom: 1px solid #2a2a2a; }

.pagination { display: flex; gap: 8px; justify-content: center; margin-top: 20px; }
.pagination a { background: #1e1e1e; border: 1px solid #333; padding: 5px 12px; border-radius: 4px; }
.pagination a.active { background: #2a4a7f; border-color: #2a4a7f; color: #fff; }

.stat { display: inline-block; background: #1e1e1e; border: 1px solid #2a2a2a;
  padding: 10px 18px; border-radius: 6px; margin: 0 8px 8px 0; text-align: center; }
.stat .n { font-size: 22px; font-weight: bold; color: #7eb8f7; }
.stat .l { font-size: 11px; color: #666; margin-top: 2px; }

.highlight { background: #ff9800; color: #111; border-radius: 2px; padding: 0 2px; }
.empty { color: #555; padding: 30px; text-align: center; }
"""


def _page(title: str, nav_active: str, content: str, secret: str) -> str:
    base = f"/{secret}/ui"
    links = [
        ("topics", "Topics"),
        ("segments", "Segments"),
        ("commands", "Commandes"),
        ("memories", "Memories"),
    ]
    nav_html = "".join(
        f'<a href="{base}/{k}" class="{"active" if k == nav_active else ""}">{label}</a>'
        for k, label in links
    )
    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OMI · {title}</title>
<style>{_CSS}</style>
</head>
<body>
<nav>
  <span class="brand">📋 OMI</span>
  {nav_html}
</nav>
<div class="container">
{content}
</div>
</body>
</html>"""


def _highlight(text: str, q: str) -> str:
    if not q or not text:
        return text or ""
    import re
    return re.sub(f"({re.escape(q)})", r'<span class="highlight">\1</span>', text, flags=re.IGNORECASE)


# ── Topics ───────────────────────────────────────────────────────────────────

@router.get("/topics")
async def ui_topics(
    secret: str,
    day: str = Query(default=""),
    side: str = Query(default=""),
    ctype: str = Query(default=""),
):
    with db.connect() as conn:
        where = ["1=1"]
        params: list = []
        if day:
            where.append("day=?"); params.append(day)
        if side:
            where.append("side=?"); params.append(side)
        if ctype:
            where.append("content_type=?"); params.append(ctype)
        rows = conn.execute(
            f"SELECT * FROM topics WHERE {' AND '.join(where)} ORDER BY day DESC, start_time DESC",
            params
        ).fetchall()

        days_available = [r[0] for r in conn.execute(
            "SELECT DISTINCT day FROM topics ORDER BY day DESC LIMIT 60"
        ).fetchall()]

    # Groupe par jour
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)

    TYPE_ICONS = {"conversation": "💬", "reunion": "👥", "solo": "🧍", "media": "🎬", "ambiance": "🌫", "autre": "•"}

    cards_html = ""
    for d in sorted(by_day.keys(), reverse=True):
        cards_html += f'<div class="day-header">{d}</div>'
        for r in by_day[d]:
            persons = json.loads(r["person"] if "person" in r.keys() else (r["persons"] or "[]"))
            todo = json.loads(r["todo"] or "[]")
            icon = TYPE_ICONS.get(r["content_type"], "•")
            tags = f'<span class="tag {r["content_type"]}">{icon} {r["content_type"]}</span>'
            tags += f'<span class="tag {r["side"]}">{r["side"]}</span>'
            dur = f'{r["duration_min"]} min' if r["duration_min"] else ""
            persons_str = ", ".join(persons[:4]) if persons else ""
            ann = ""
            if r["annotated"]:
                ann = f'<br><small>✏️ {r["user_label"] or ""} {r["user_rating"] or ""}</small>'
            todo_html = ""
            if todo:
                todo_html = "<ul style='margin-top:6px;padding-left:16px;color:#aaa;font-size:12px'>"
                for t in todo:
                    todo_html += f"<li>{t}</li>"
                todo_html += "</ul>"
            cards_html += f"""<div class="card">
  <div class="meta">{tags} {dur} {("— " + persons_str) if persons_str else ""}</div>
  <div class="text">{r["subject"] or "(sans titre)"}{ann}</div>
  {todo_html}
</div>"""

    if not cards_html:
        cards_html = '<div class="empty">Aucun topic correspondant.</div>'

    # Filtres
    day_opts = ''.join(f'<option value="{d}" {"selected" if d==day else ""}>{d}</option>' for d in days_available)
    filter_html = f"""
<form method="get" style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
  <select name="day"><option value="">Tous les jours</option>{day_opts}</select>
  <select name="side">
    <option value="">Tous</option>
    <option value="business" {"selected" if side=="business" else ""}>Business</option>
    <option value="perso" {"selected" if side=="perso" else ""}>Perso</option>
  </select>
  <select name="ctype">
    <option value="">Tous types</option>
    {"".join(f'<option value="{t}" {"selected" if ctype==t else ""}>{t}</option>' for t in ["conversation","reunion","solo","media","ambiance"])}
  </select>
  <button type="submit">Filtrer</button>
</form>"""

    content = f"<h2>Topics ({len(rows)})</h2>{filter_html}{cards_html}"
    return HTMLResponse(_page("Topics", "topics", content, secret))


# ── Segments ─────────────────────────────────────────────────────────────────

PAGE_SIZE = 50


@router.get("/segments")
async def ui_segments(
    secret: str,
    q: str = Query(default=""),
    alex_only: str = Query(default=""),
    day: str = Query(default=""),
    page: int = Query(default=1),
):
    with db.connect() as conn:
        where = ["1=1"]
        params: list = []
        if q:
            where.append("text LIKE ?"); params.append(f"%{q}%")
        if alex_only == "1":
            where.append("is_user=1")
        if day:
            where.append("date(received_at)=?"); params.append(day)

        total = conn.execute(
            f"SELECT COUNT(*) FROM segments WHERE {' AND '.join(where)}", params
        ).fetchone()[0]

        offset = (page - 1) * PAGE_SIZE
        rows = conn.execute(
            f"SELECT * FROM segments WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, offset]
        ).fetchall()

    cards_html = ""
    for r in rows:
        is_alex = r["is_user"]
        cls = "alex" if is_alex else ""
        who = "Alex" if is_alex else (r["speaker"] or "?")
        text_hl = _highlight(r["text"], q)
        ts = str(r["received_at"] or "")[:16]
        cards_html += f"""<div class="card {cls}">
  <div class="meta">{ts} · {who}</div>
  <div class="text">{text_hl}</div>
</div>"""

    if not cards_html:
        cards_html = '<div class="empty">Aucun segment trouvé.</div>'

    # Pagination
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    pages_html = '<div class="pagination">'
    for p in range(max(1, page - 3), min(total_pages, page + 3) + 1):
        qs = f"?q={q}&alex_only={alex_only}&day={day}&page={p}"
        cls = "active" if p == page else ""
        pages_html += f'<a href="{qs}" class="{cls}">{p}</a>'
    pages_html += "</div>"

    search_form = f"""
<form method="get" class="search-bar">
  <input name="q" value="{q}" placeholder="Rechercher dans les segments…">
  <input name="day" value="{day}" placeholder="Jour YYYY-MM-DD" style="max-width:140px">
  <label style="align-self:center;color:#aaa;font-size:13px">
    <input type="checkbox" name="alex_only" value="1" {"checked" if alex_only=="1" else ""}> Alex seulement
  </label>
  <button type="submit">Chercher</button>
</form>"""

    content = f"<h2>Segments · {total} résultats</h2>{search_form}{cards_html}{pages_html}"
    return HTMLResponse(_page("Segments", "segments", content, secret))


# ── Commandes ────────────────────────────────────────────────────────────────

@router.get("/commands")
async def ui_commands(secret: str, q: str = Query(default="")):
    with db.connect() as conn:
        params: list = []
        where = "1=1"
        if q:
            where = "(command_text LIKE ? OR response_text LIKE ?)"
            params = [f"%{q}%", f"%{q}%"]
        rows = conn.execute(
            f"SELECT * FROM commands WHERE {where} ORDER BY id DESC LIMIT 100",
            params
        ).fetchall()

    STATUS_ICONS = {"done": "✅", "error": "❌", "cancelled": "🚫", "pending": "⏳", "dispatched": "📤", "conversation_opened": "💬"}
    cards = ""
    for r in rows:
        icon = STATUS_ICONS.get(r["status"], "•")
        ts = str(r["created_at"] or "")[:16]
        cmd_hl = _highlight(r["command_text"] or "(vide)", q)
        resp = r["response_text"] or ""
        resp_hl = _highlight(resp[:300] + ("…" if len(resp) > 300 else ""), q)
        cards += f"""<div class="card trigger">
  <div class="meta">{ts} · {icon} {r["status"]} · agent: {r["agent"] or "omar"}</div>
  <div class="text" style="margin-bottom:6px"><strong>Commande :</strong> {cmd_hl}</div>
  {"<div class='text' style='color:#aaa'><strong>Réponse :</strong> " + resp_hl + "</div>" if resp_hl else ""}
</div>"""

    if not cards:
        cards = '<div class="empty">Aucune commande.</div>'

    form = f"""<form method="get" class="search-bar">
  <input name="q" value="{q}" placeholder="Rechercher dans les commandes…">
  <button type="submit">Chercher</button>
</form>"""

    content = f"<h2>Commandes Allo Omar</h2>{form}{cards}"
    return HTMLResponse(_page("Commandes", "commands", content, secret))


# ── Memories ─────────────────────────────────────────────────────────────────

@router.get("/memories")
async def ui_memories(secret: str, q: str = Query(default=""), page: int = Query(default=1)):
    with db.connect() as conn:
        where = "1=1"
        params: list = []
        if q:
            where = "(title LIKE ? OR overview LIKE ?)"
            params = [f"%{q}%", f"%{q}%"]
        total = conn.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
        ).fetchall()

    cards = ""
    for r in rows:
        ts = str(r["received_at"] or "")[:16]
        title_hl = _highlight(r["title"] or "(sans titre)", q)
        overview_hl = _highlight((r["overview"] or "")[:300], q)
        cards += f"""<div class="card">
  <div class="meta">{ts} · {r["category"] or ""}</div>
  <div class="text" style="margin-bottom:4px"><strong>{title_hl}</strong></div>
  {"<div class='text' style='color:#aaa'>" + overview_hl + "</div>" if overview_hl else ""}
</div>"""

    if not cards:
        cards = '<div class="empty">Aucune memory.</div>'

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    pages_html = '<div class="pagination">'
    for p in range(max(1, page - 3), min(total_pages, page + 3) + 1):
        qs = f"?q={q}&page={p}"
        cls = "active" if p == page else ""
        pages_html += f'<a href="{qs}" class="{cls}">{p}</a>'
    pages_html += "</div>"

    form = f"""<form method="get" class="search-bar">
  <input name="q" value="{q}" placeholder="Rechercher dans les memories…">
  <button type="submit">Chercher</button>
</form>"""
    content = f"<h2>Memories · {total}</h2>{form}{cards}{pages_html}"
    return HTMLResponse(_page("Memories", "memories", content, secret))


# ── Redirect racine → topics ──────────────────────────────────────────────────

@router.get("")
@router.get("/")
async def ui_root(secret: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/{secret}/ui/topics")
