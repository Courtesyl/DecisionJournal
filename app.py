import csv
import io
import json
import mimetypes
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import auth
import db


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent


BASE_DIR = _base_dir()

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/woff2", ".woff2")

STAKES_WORDS = {1: "мелкая", 2: "средняя", 3: "крупная"}

CSV_FIELDS = [
    "decision_id", "decision_created_at", "title", "chosen", "context",
    "reasoning", "alternatives", "wrong_if", "stakes", "reversibility",
    "tags", "retrospective",
    "prediction_id", "prediction_text", "confidence", "review_at",
    "outcome", "outcome_quality", "outcome_at", "outcome_notes",
    "prediction_created_at",
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
tpl = Jinja2Templates(directory=str(BASE_DIR / "templates"))
tpl.env.globals["can_exit"] = False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/setup", "/logout", "/healthz"):
        return await call_next(request)
    if not auth.load_auth():
        if path == "/setup":
            return await call_next(request)
        return RedirectResponse("/setup", status_code=303)
    if not request.session.get("auth"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    same_site="lax",
    https_only=False,
    max_age=30 * 24 * 3600,
)


def _add_months(base: date, months: int) -> date:
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days_in_month = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(base.day, days_in_month))


def _presets(today: date) -> dict:
    return {
        "week": (today + timedelta(days=7)).isoformat(),
        "month": _add_months(today, 1).isoformat(),
        "quarter": _add_months(today, 3).isoformat(),
        "year": _add_months(today, 12).isoformat(),
    }


@app.get("/healthz")
def healthz():
    return {"app": "decision-journal", "db": str(db.DB_PATH)}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, tag: str = ""):
    tag = tag.strip().lower() or None
    decisions = db.list_decisions(limit=500, tag=tag)
    chips = sorted({
        t for d in db.list_decisions(limit=500)
        for t in (d["tags"] or "").split(",") if t
    })
    return tpl.TemplateResponse(request, "index.html", {
        "decisions": decisions,
        "due": db.due_predictions(),
        "tags": chips,
        "tag": tag,
        "stakes_words": STAKES_WORDS,
    })


@app.get("/new", response_class=HTMLResponse)
def new_form(request: Request, wizard: bool = False):
    today = date.today()
    return tpl.TemplateResponse(request, "new.html", {
        "wizard": wizard,
        "today": today.isoformat(),
        "presets": _presets(today),
    })


@app.post("/new")
def new_post(
    title: str = Form(""),
    chosen: str = Form(""),
    context: str = Form(""),
    reasoning: str = Form(""),
    alternatives: str = Form(""),
    wrong_if: str = Form(""),
    stakes: str = Form(""),
    reversibility: str = Form(""),
    tags: str = Form(""),
    pred_text: list[str] = Form(default=[], alias="pred_text[]"),
    pred_conf: list[str] = Form(default=[], alias="pred_conf[]"),
    pred_review: list[str] = Form(default=[], alias="pred_review[]"),
):
    predictions = [
        {"text": t, "confidence": c, "review_at": r}
        for t, c, r in zip(pred_text, pred_conf, pred_review)
        if t.strip()
    ]
    data = {
        "title": title,
        "chosen": chosen,
        "context": context,
        "reasoning": reasoning,
        "alternatives": alternatives,
        "wrong_if": wrong_if,
        "stakes": stakes,
        "reversibility": reversibility,
        "tags": tags,
    }
    try:
        did = db.create_decision(data, predictions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(f"/decision/{did}", status_code=303)


@app.get("/decision/{did}", response_class=HTMLResponse)
def view_decision(request: Request, did: int):
    decision, preds = db.get_decision(did)
    if decision is None:
        raise HTTPException(status_code=404, detail="Решение не найдено")
    open_count = sum(1 for p in preds if p["outcome"] is None)
    return tpl.TemplateResponse(request, "view.html", {
        "d": decision,
        "preds": preds,
        "open_count": open_count,
        "stakes_words": STAKES_WORDS,
        "qualities": range(-2, 3),
    })


@app.post("/prediction/{pid}/close")
def close_prediction(pid: int, outcome: str = Form(...), quality: str = Form(""), notes: str = Form("")):
    try:
        closed = db.close_prediction(pid, outcome, quality, notes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not closed:
        raise HTTPException(status_code=404, detail="Прогноз не найден или уже закрыт")
    pred = db.get_prediction(pid)
    return RedirectResponse(f"/decision/{pred['decision_id']}", status_code=303)


@app.get("/decision/{did}/edit", response_class=HTMLResponse)
def edit_form(request: Request, did: int):
    decision, _ = db.get_decision(did)
    if decision is None:
        raise HTTPException(status_code=404, detail="Решение не найдено")
    return tpl.TemplateResponse(request, "edit.html", {
        "d": decision,
        "stakes_words": STAKES_WORDS,
    })


@app.post("/decision/{did}/edit")
def edit_post(
    did: int,
    title: str = Form(""),
    chosen: str = Form(""),
    context: str = Form(""),
    reasoning: str = Form(""),
    alternatives: str = Form(""),
    wrong_if: str = Form(""),
    stakes: str = Form(""),
    reversibility: str = Form(""),
    tags: str = Form(""),
):
    fields = {
        "title": title,
        "chosen": chosen,
        "context": context,
        "reasoning": reasoning,
        "alternatives": alternatives,
        "wrong_if": wrong_if,
        "stakes": stakes,
        "reversibility": reversibility,
        "tags": tags,
    }
    try:
        result = db.update_decision(did, fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result == "not_found":
        raise HTTPException(status_code=404, detail="Решение не найдено")
    return RedirectResponse(f"/decision/{did}", status_code=303)


@app.post("/decision/{did}/retrospective")
def retrospective_post(did: int, retrospective: str = Form(...)):
    try:
        saved = db.add_retrospective(did, retrospective)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not saved:
        raise HTTPException(status_code=404, detail="Решение не найдено")
    return RedirectResponse(f"/decision/{did}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if not auth.load_auth():
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("auth"):
        return RedirectResponse("/", status_code=303)
    return tpl.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
def login_post(request: Request, password: str = Form("")):
    if auth.verify_password(password):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    time.sleep(0.5)
    return tpl.TemplateResponse(request, "login.html", {"error": "Неверный пароль"}, status_code=401)


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    if auth.load_auth():
        return RedirectResponse("/login", status_code=303)
    return tpl.TemplateResponse(request, "setup.html", {"error": ""})


@app.post("/setup")
def setup_post(request: Request, password: str = Form(""), confirm: str = Form("")):
    if auth.load_auth():
        return RedirectResponse("/login", status_code=303)
    if password != confirm:
        return tpl.TemplateResponse(
            request, "setup.html", {"error": "Пароли не совпадают"}, status_code=400
        )
    try:
        auth.save_password(password)
    except ValueError as e:
        return tpl.TemplateResponse(request, "setup.html", {"error": str(e)}, status_code=400)
    request.session["auth"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/exit")
def exit_app():
    server = getattr(app.state, "server", None)
    if server is None:
        return HTMLResponse("Dev-режим: останови сервер через Ctrl+C в консоли.", status_code=400)
    server.should_exit = True
    return HTMLResponse("Завершаю работу — вкладку можно закрыть.")


@app.post("/decision/{did}/delete")
def delete_decision(did: int):
    if not db.delete_decision(did):
        raise HTTPException(status_code=404, detail="Решение не найдено")
    return RedirectResponse("/", status_code=303)


@app.get("/calibration", response_class=HTMLResponse)
def calibration(request: Request):
    return tpl.TemplateResponse(request, "calibration.html", {
        "bins": db.calibration_bins(),
        "brier": db.brier_score(),
    })


@app.get("/export.csv")
def export_csv():
    rows = db.export_rows()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    content = chr(0xFEFF) + buf.getvalue()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="decisions.csv"'},
    )


@app.get("/export.json")
def export_json():
    content = json.dumps(db.export_rows(), ensure_ascii=False, indent=1)
    return Response(
        content,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="decisions.json"'},
    )
