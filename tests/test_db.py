from datetime import date

import sqlite3

import pytest

import db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    return db


def d(**kw):
    base = {"title": "t", "chosen": "c"}
    base.update(kw)
    return base


def p(text="btc > 100k", conf="7.0", review="2099-01-01"):
    return {"text": text, "confidence": conf, "review_at": review}


class TestCreateAndGet:
    def test_roundtrip(self, tmp_db):
        did = tmp_db.create_decision(
            d(title="  Купить ETH  ", chosen="купить", context=" падение 20% ",
              reasoning=" метрики ок ", alternatives=" ждал отскок ", wrong_if=" ниже 1500 ",
              stakes="3", reversibility="one-way", tags=" Money , crypto ,money "),
            [p(text=" ETH > 3000 ", conf="7.5", review="2099-06-01")],
        )
        dec, preds = tmp_db.get_decision(did)
        assert dec["title"] == "Купить ETH"
        assert dec["tags"] == "money,crypto"
        assert dec["stakes"] == 3
        assert dec["reversibility"] == "one-way"
        assert dec["alternatives"] == "ждал отскок"
        assert dec["wrong_if"] == "ниже 1500"
        assert len(preds) == 1
        assert preds[0]["text"] == "ETH > 3000"
        assert preds[0]["confidence"] == 7.5
        assert preds[0]["review_at"] == "2099-06-01"
        assert preds[0]["outcome"] is None

    def test_empty_text_becomes_null(self, tmp_db):
        did = tmp_db.create_decision(d(context=""), [])
        dec, _ = tmp_db.get_decision(did)
        assert dec["context"] is None
        assert dec["stakes"] is None

    def test_missing_title_raises(self, tmp_db):
        with pytest.raises(ValueError):
            tmp_db.create_decision(d(title="  "), [])

    def test_bad_confidence_raises(self, tmp_db):
        with pytest.raises(ValueError):
            tmp_db.create_decision(d(), [p(conf="11")])
        with pytest.raises(ValueError):
            tmp_db.create_decision(d(), [p(conf="abc")])

    def test_bad_review_raises(self, tmp_db):
        with pytest.raises(ValueError):
            tmp_db.create_decision(d(), [p(review="01.02.2099")])

    def test_bad_stakes_raises(self, tmp_db):
        with pytest.raises(ValueError):
            tmp_db.create_decision(d(stakes="5"), [])


class TestList:
    def test_counts_and_order(self, tmp_db):
        tmp_db.create_decision(d(title="старое"), [])
        did2 = tmp_db.create_decision(d(title="новое"), [p(), p(text="второй", review="2098-01-01")])
        pid = tmp_db.open_predictions(did2)[0]["id"]
        tmp_db.close_prediction(pid, "hit")
        rows = tmp_db.list_decisions()
        assert [r["title"] for r in rows] == ["новое", "старое"]
        new = rows[0]
        assert new["pred_count"] == 2
        assert new["closed_count"] == 1
        assert new["next_review"] == "2099-01-01"
        old = rows[1]
        assert old["pred_count"] == 0
        assert old["closed_count"] == 0
        assert old["next_review"] is None

    def test_tag_filter_exact_token(self, tmp_db):
        tmp_db.create_decision(d(tags="work"), [])
        tmp_db.create_decision(d(tags="homework"), [])
        tmp_db.create_decision(d(tags="money,crypto"), [])
        rows = tmp_db.list_decisions(tag="work")
        assert len(rows) == 1
        rows = tmp_db.list_decisions(tag="crypto")
        assert len(rows) == 1
        assert tmp_db.list_decisions(tag="WORK")[0]["tags"] == "work"


class TestDue:
    def test_due_filter_and_sort(self, tmp_db):
        d_low = tmp_db.create_decision(d(stakes="1"), [p(text="низкая", review="2020-06-01")])
        d_high = tmp_db.create_decision(d(stakes="3"), [p(text="высокая", review="2020-01-01")])
        tmp_db.create_decision(d(stakes="3"), [p(text="будущее", review="2099-12-31")])
        rows = tmp_db.due_predictions()
        assert all(r["review_at"] <= date.today().isoformat() for r in rows)
        assert [r["decision_id"] for r in rows] == [d_high, d_low]
        assert rows[0]["stakes"] == 3
        assert rows[1]["stakes"] == 1

    def test_closed_not_due(self, tmp_db):
        did = tmp_db.create_decision(d(), [p(review="2020-01-01")])
        pid = tmp_db.open_predictions(did)[0]["id"]
        tmp_db.close_prediction(pid, "miss")
        assert tmp_db.due_predictions() == []


class TestClose:
    def test_close_flow(self, tmp_db):
        did = tmp_db.create_decision(d(), [p()])
        pid = tmp_db.open_predictions(did)[0]["id"]
        assert tmp_db.close_prediction(pid, "partial", quality="1", notes=" наполовину ")
        pred = tmp_db.get_prediction(pid)
        assert pred["outcome"] == "partial"
        assert pred["outcome_quality"] == 1
        assert pred["outcome_notes"] == "наполовину"
        assert pred["outcome_at"] is not None
        assert tmp_db.open_predictions(did) == []

    def test_double_close_false(self, tmp_db):
        did = tmp_db.create_decision(d(), [p()])
        pid = tmp_db.open_predictions(did)[0]["id"]
        assert tmp_db.close_prediction(pid, "hit")
        assert tmp_db.close_prediction(pid, "miss") is False

    def test_unknown_pid_false(self, tmp_db):
        assert tmp_db.close_prediction(999, "hit") is False

    def test_bad_outcome_raises(self, tmp_db):
        with pytest.raises(ValueError):
            tmp_db.close_prediction(1, "win")
        with pytest.raises(ValueError):
            tmp_db.close_prediction(1, "hit", quality="7")


class TestDelete:
    def test_delete_cascades_predictions(self, tmp_db):
        did = tmp_db.create_decision(d(), [p(), p(text="second")])
        pid = tmp_db.open_predictions(did)[0]["id"]
        assert tmp_db.delete_decision(did)
        assert tmp_db.get_decision(did)[0] is None
        assert tmp_db.get_decision(did)[1] == []
        assert tmp_db.get_prediction(pid) is None
        assert tmp_db.due_predictions() == []

    def test_unknown_false(self, tmp_db):
        assert tmp_db.delete_decision(999) is False


class TestBackup:
    def test_no_backup_on_fresh(self, tmp_db):
        assert not (tmp_db.DB_PATH.parent / "decisions.backup.db").exists()

    def test_backup_on_reinit(self, tmp_db):
        tmp_db.create_decision(d(), [p()])
        tmp_db.init_db()
        backup = tmp_db.DB_PATH.parent / "decisions.backup.db"
        assert backup.exists()
        conn = sqlite3.connect(backup)
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        conn.close()

    def test_wal_mode(self, tmp_db):
        with tmp_db.get_db() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_rotation(self, tmp_db):
        tmp_db.create_decision(d(title="a"), [])
        tmp_db.init_db()
        tmp_db.create_decision(d(title="b"), [])
        tmp_db.init_db()
        conn = sqlite3.connect(tmp_db.DB_PATH.parent / "decisions.backup.db")
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2
        conn.close()
        conn = sqlite3.connect(tmp_db.DB_PATH.parent / "decisions.backup.1.db")
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        conn.close()
        assert not (tmp_db.DB_PATH.parent / "decisions.backup.2.db").exists()


class TestUpdateAndRetrospective:
    def test_update(self, tmp_db):
        did = tmp_db.create_decision(d(), [])
        assert tmp_db.update_decision(did, {"title": "новый заголовок", "stakes": "2", "tags": " a , B "}) == "updated"
        dec, _ = tmp_db.get_decision(did)
        assert dec["title"] == "новый заголовок"
        assert dec["stakes"] == 2
        assert dec["tags"] == "a,b"

    def test_update_unknown_id_not_found(self, tmp_db):
        assert tmp_db.update_decision(999, {"title": "x"}) == "not_found"

    def test_update_empty_title_raises(self, tmp_db):
        did = tmp_db.create_decision(d(), [])
        with pytest.raises(ValueError):
            tmp_db.update_decision(did, {"title": " "})

    def test_update_no_fields(self, tmp_db):
        did = tmp_db.create_decision(d(), [])
        assert tmp_db.update_decision(did, {}) == "no_changes"
        assert tmp_db.update_decision(999, {}) == "not_found"

    def test_retrospective(self, tmp_db):
        did = tmp_db.create_decision(d(), [])
        assert tmp_db.add_retrospective(did, " правило на будущее ")
        dec, _ = tmp_db.get_decision(did)
        assert dec["retrospective"] == "правило на будущее"
        with pytest.raises(ValueError):
            tmp_db.add_retrospective(did, "  ")


class TestCalibration:
    def seed(self, tmp_db, items):
        for i, (conf, outcome) in enumerate(items):
            did = tmp_db.create_decision(d(title=f"d{i}"), [p(text=f"t{i}", conf=conf, review="2020-01-01")])
            pid = tmp_db.open_predictions(did)[0]["id"]
            tmp_db.close_prediction(pid, outcome)

    def test_bins(self, tmp_db):
        self.seed(tmp_db, [
            ("2.0", "hit"), ("2.0", "miss"),
            ("4.0", "hit"), ("4.0", "partial"),
            ("6.0", "hit"),
            ("8.0", "miss"),
        ])
        bins = tmp_db.calibration_bins()
        assert [b["label"] for b in bins] == ["1.0–2.5", "3.0–4.5", "5.0–6.5", "7.0–8.5", "9.0–10.0"]
        assert bins[0]["n"] == 2
        assert bins[0]["hit_rate"] == pytest.approx(0.5)
        assert bins[0]["avg_conf"] == pytest.approx(0.20)
        assert bins[1]["n"] == 2
        assert bins[1]["hit_rate"] == pytest.approx(0.75)
        assert bins[1]["avg_conf"] == pytest.approx(0.40)
        assert bins[2]["n"] == 1
        assert bins[2]["hit_rate"] == 1.0
        assert bins[3]["hit_rate"] == 0.0
        assert bins[4]["n"] == 0
        assert bins[4]["hit_rate"] is None

    def test_bin_boundaries_inclusive_and_disjoint(self, tmp_db):
        self.seed(tmp_db, [("2.5", "hit"), ("3.0", "hit"), ("9.0", "hit"), ("10.0", "hit")])
        bins = tmp_db.calibration_bins()
        assert bins[0]["n"] == 1
        assert bins[1]["n"] == 1
        assert bins[4]["n"] == 2
        assert sum(b["n"] for b in bins) == 4

    def test_boundary_10_in_last_bin(self, tmp_db):
        self.seed(tmp_db, [("10.0", "hit")])
        bins = tmp_db.calibration_bins()
        assert bins[4]["n"] == 1

    def test_empty(self, tmp_db):
        bins = tmp_db.calibration_bins()
        assert all(b["n"] == 0 and b["hit_rate"] is None for b in bins)
        assert tmp_db.brier_score() is None

    def test_brier(self, tmp_db):
        self.seed(tmp_db, [("5.0", "hit"), ("8.0", "partial")])
        brier = tmp_db.brier_score()
        assert brier["value"] == pytest.approx((0.25 + 0.09) / 2)
        assert brier["n"] == 2
        assert brier["reliable"] is False

    def test_brier_reliable_threshold(self, tmp_db):
        self.seed(tmp_db, [("5.0", "hit")] * 20)
        brier = tmp_db.brier_score()
        assert brier["n"] == 20
        assert brier["reliable"] is True


class TestExport:
    def test_rows(self, tmp_db):
        did = tmp_db.create_decision(d(), [p()])
        tmp_db.close_prediction(tmp_db.open_predictions(did)[0]["id"], "hit")
        rows = tmp_db.export_rows()
        assert len(rows) == 1
        assert rows[0]["decision_id"] == did
        assert rows[0]["outcome"] == "hit"
        assert rows[0]["title"] == "t"

    def test_empty_decision_row(self, tmp_db):
        did = tmp_db.create_decision(d(), [])
        rows = tmp_db.export_rows()
        assert len(rows) == 1
        assert rows[0]["decision_id"] == did
        assert rows[0]["prediction_id"] is None
