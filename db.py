import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

if getattr(sys, "frozen", False):
    if sys.platform == "win32":
        _base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        _DEFAULT_DIR = _base / "DecisionJournal"
    elif sys.platform == "darwin":
        _DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "DecisionJournal"
    else:
        _xdg = os.environ.get("XDG_DATA_HOME")
        _DEFAULT_DIR = Path(_xdg or (Path.home() / ".local" / "share")) / "DecisionJournal"
else:
    _DEFAULT_DIR = Path(__file__).parent
_DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("DECISIONS_DB") or (_DEFAULT_DIR / "decisions.db"))

OUTCOMES = ("hit", "miss", "partial")
OUTCOME_SCORE = {"hit": 1.0, "partial": 0.5, "miss": 0.0}
_REVERSIBILITY = ("one-way", "two-way")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    chosen TEXT NOT NULL,
    context TEXT,
    reasoning TEXT,
    alternatives TEXT,
    wrong_if TEXT,
    stakes INTEGER CHECK(stakes BETWEEN 1 AND 3),
    reversibility TEXT CHECK(reversibility IN ('one-way','two-way') OR reversibility IS NULL),
    tags TEXT,
    retrospective TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 1.0 AND 10.0),
    review_at TEXT NOT NULL,
    outcome TEXT CHECK(outcome IN ('hit','miss','partial') OR outcome IS NULL),
    outcome_quality INTEGER CHECK(outcome_quality BETWEEN -2 AND 2 OR outcome_quality IS NULL),
    outcome_at TEXT,
    outcome_notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pred_review ON predictions(review_at) WHERE outcome IS NULL;
CREATE INDEX IF NOT EXISTS idx_pred_decision ON predictions(decision_id);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_BACKUP_NAMES = ("decisions.backup.db", "decisions.backup.1.db", "decisions.backup.2.db")


def _rotate_backups():
    paths = [DB_PATH.parent / name for name in _BACKUP_NAMES]
    if paths[2].exists():
        paths[2].unlink()
    if paths[1].exists():
        paths[1].rename(paths[2])
    if paths[0].exists():
        paths[0].rename(paths[1])
    return paths[0]


def init_db():
    if DB_PATH.exists():
        try:
            with get_db() as conn:
                has_tables = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'"
                ).fetchone()
            if has_tables:
                shutil.copy2(DB_PATH, _rotate_backups())
        except (sqlite3.Error, OSError):
            pass
    with get_db() as db:
        db.executescript(SCHEMA)


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _clean_tags(value):
    if value is None:
        return None
    seen = []
    for part in str(value).split(","):
        tag = part.strip().lower()
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen) or None


def _clean_stakes(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        stakes = int(str(value).strip())
    except ValueError:
        raise ValueError("stakes должен быть целым 1..3")
    if not 1 <= stakes <= 3:
        raise ValueError("stakes должен быть 1..3")
    return stakes


def _clean_reversibility(value):
    if value is None or str(value).strip() == "":
        return None
    if value not in _REVERSIBILITY:
        raise ValueError("reversibility должен быть one-way или two-way")
    return value


def _clean_confidence(value):
    try:
        conf = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("confidence должен быть числом 1.0–10.0")
    if not 1.0 <= conf <= 10.0:
        raise ValueError("confidence должен быть в диапазоне 1.0–10.0")
    return conf


def _clean_review_at(value):
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except (TypeError, ValueError):
        raise ValueError("review_at должен быть датой в формате YYYY-MM-DD")


def create_decision(data: dict, predictions: list[dict]) -> int:
    title = _clean(data.get("title"))
    chosen = _clean(data.get("chosen"))
    if not title:
        raise ValueError("title обязателен")
    if not chosen:
        raise ValueError("chosen обязателен")

    now = _now()
    prepared_preds = []
    for p in predictions:
        text = _clean(p.get("text"))
        if not text:
            raise ValueError("текст прогноза обязателен")
        prepared_preds.append((
            text,
            _clean_confidence(p.get("confidence")),
            _clean_review_at(p.get("review_at")),
            now,
        ))

    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO decisions
                (created_at, title, chosen, context, reasoning, alternatives,
                 wrong_if, stakes, reversibility, tags)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now, title, chosen,
                _clean(data.get("context")),
                _clean(data.get("reasoning")),
                _clean(data.get("alternatives")),
                _clean(data.get("wrong_if")),
                _clean_stakes(data.get("stakes")),
                _clean_reversibility(data.get("reversibility")),
                _clean_tags(data.get("tags")),
            ),
        )
        did = cur.lastrowid
        db.executemany(
            """
            INSERT INTO predictions (decision_id, text, confidence, review_at, created_at)
            VALUES (?,?,?,?,?)
            """,
            [(did, *p) for p in prepared_preds],
        )
        return did


def list_decisions(limit: int = 100, tag: str | None = None) -> list[dict]:
    sql = """
        SELECT d.*,
            COUNT(p.id) AS pred_count,
            COALESCE(SUM(CASE WHEN p.outcome IS NOT NULL THEN 1 ELSE 0 END), 0) AS closed_count,
            MIN(CASE WHEN p.outcome IS NULL THEN p.review_at END) AS next_review
        FROM decisions d
        LEFT JOIN predictions p ON p.decision_id = d.id
    """
    params: list = []
    if tag:
        token = str(tag).strip().lower()
        token = token.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        sql += "WHERE (',' || REPLACE(COALESCE(d.tags, ''), ' ', '') || ',') LIKE ? ESCAPE '\\'\n"
        params.append(f"%,{token},%")
    sql += "GROUP BY d.id ORDER BY d.created_at DESC, d.id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params)]


def get_decision(did: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        if not row:
            return None, []
        preds = db.execute(
            "SELECT * FROM predictions WHERE decision_id=? ORDER BY review_at",
            (did,),
        ).fetchall()
        return dict(row), [dict(p) for p in preds]


_DECISION_CLEANERS = {
    "title": _clean,
    "chosen": _clean,
    "context": _clean,
    "reasoning": _clean,
    "alternatives": _clean,
    "wrong_if": _clean,
    "tags": _clean_tags,
    "stakes": _clean_stakes,
    "reversibility": _clean_reversibility,
}


def update_decision(did: int, fields: dict) -> str:
    sets, params = [], []
    for key, clean in _DECISION_CLEANERS.items():
        if key not in fields:
            continue
        value = clean(fields[key])
        if key in ("title", "chosen") and not value:
            raise ValueError(f"{key} не может быть пустым")
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        with get_db() as db:
            exists = db.execute("SELECT 1 FROM decisions WHERE id=?", (did,)).fetchone()
        return "no_changes" if exists else "not_found"
    params.append(did)
    with get_db() as db:
        cur = db.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id=?", params)
        return "updated" if cur.rowcount > 0 else "not_found"


def delete_decision(did: int) -> bool:
    with get_db() as db:
        cur = db.execute("DELETE FROM decisions WHERE id=?", (did,))
        return cur.rowcount > 0


def add_retrospective(did: int, text: str) -> bool:
    text = _clean(text)
    if not text:
        raise ValueError("ретроспектива не может быть пустой")
    with get_db() as db:
        cur = db.execute("UPDATE decisions SET retrospective=? WHERE id=?", (text, did))
        return cur.rowcount > 0


def due_predictions() -> list[dict]:
    today = date.today().isoformat()
    with get_db() as db:
        return [
            dict(r)
            for r in db.execute(
                """
                SELECT p.*, d.title AS decision_title, d.stakes AS stakes
                FROM predictions p
                JOIN decisions d ON d.id = p.decision_id
                WHERE p.outcome IS NULL AND p.review_at <= ?
                ORDER BY d.stakes DESC, p.review_at ASC
                """,
                (today,),
            )
        ]


def open_predictions(decision_id: int) -> list[dict]:
    with get_db() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM predictions WHERE decision_id=? AND outcome IS NULL ORDER BY review_at",
                (decision_id,),
            )
        ]


def get_prediction(pid: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None


def close_prediction(pid: int, outcome: str, quality=None, notes=None) -> bool:
    if outcome not in OUTCOMES:
        raise ValueError("outcome должен быть hit, miss или partial")
    if quality is not None and str(quality).strip() != "":
        try:
            quality = int(str(quality).strip())
        except ValueError:
            raise ValueError("quality должен быть целым -2..2")
        if not -2 <= quality <= 2:
            raise ValueError("quality должен быть в диапазоне -2..2")
    else:
        quality = None
    with get_db() as db:
        cur = db.execute(
            """
            UPDATE predictions
            SET outcome=?, outcome_quality=?, outcome_at=?, outcome_notes=?
            WHERE id=? AND outcome IS NULL
            """,
            (outcome, quality, _now(), _clean(notes), pid),
        )
        return cur.rowcount > 0


_BINS = (
    (1.0, 2.5, "1.0–2.5"),
    (3.0, 4.5, "3.0–4.5"),
    (5.0, 6.5, "5.0–6.5"),
    (7.0, 8.5, "7.0–8.5"),
    (9.0, 10.0, "9.0–10.0"),
)


def calibration_bins() -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT confidence, outcome FROM predictions WHERE outcome IS NOT NULL"
        ).fetchall()
    out = []
    for lo, hi, label in _BINS:
        sel = [r for r in rows if lo <= r["confidence"] <= hi]
        if not sel:
            out.append({"label": label, "n": 0, "hit_rate": None, "avg_conf": None})
            continue
        out.append({
            "label": label,
            "n": len(sel),
            "hit_rate": sum(OUTCOME_SCORE[r["outcome"]] for r in sel) / len(sel),
            "avg_conf": sum(r["confidence"] for r in sel) / len(sel) / 10.0,
        })
    return out


_BRIER_RELIABLE_N = 20


def brier_score() -> dict | None:
    with get_db() as db:
        rows = db.execute(
            "SELECT confidence, outcome FROM predictions WHERE outcome IS NOT NULL"
        ).fetchall()
    if not rows:
        return None
    n = len(rows)
    total = sum(
        (r["confidence"] / 10.0 - OUTCOME_SCORE[r["outcome"]]) ** 2 for r in rows
    )
    return {"value": total / n, "n": n, "reliable": n >= _BRIER_RELIABLE_N}


def export_rows() -> list[dict]:
    with get_db() as db:
        return [
            dict(r)
            for r in db.execute(
                """
                SELECT d.id AS decision_id, d.created_at AS decision_created_at, d.title,
                       d.chosen, d.context, d.reasoning, d.alternatives, d.wrong_if,
                       d.stakes, d.reversibility, d.tags, d.retrospective,
                       p.id AS prediction_id, p.text AS prediction_text, p.confidence,
                       p.review_at, p.outcome, p.outcome_quality, p.outcome_at,
                       p.outcome_notes, p.created_at AS prediction_created_at
                FROM decisions d
                LEFT JOIN predictions p ON p.decision_id = d.id
                ORDER BY d.created_at DESC, p.review_at
                """
            )
        ]
