"""SCANNER-HYGIENE (legacy_estate_audit P1-d/P1-e for cot):
  * add a shared scan_runs heartbeat so an empty/no-op cot run is distinguishable
    from a dead scanner in the cross-scanner monitor;
  * make the per-row COT store failure loud instead of a silent except: pass.
"""
import sqlite3


def test_log_scan_run_writes_one_row(scanner, tmp_path):
    db = tmp_path / "intel.db"
    scanner.log_scan_run("COT", "OK", 4, 2, note="2026-07-17", db_path=str(db))
    rows = sqlite3.connect(str(db)).execute(
        "SELECT scanner, source_status, n_evaluated, n_fired, note FROM scan_runs").fetchall()
    assert rows == [("COT", "OK", 4, 2, "2026-07-17")]


def test_log_scan_run_loud_on_bad_path(scanner, capsys):
    scanner.log_scan_run("COT", "OK", 1, 0, db_path="/does/not/exist/nope/intel.db")
    assert "[SCAN_RUN_FAIL]" in capsys.readouterr().out


def test_store_cot_rows_loud_on_insert_failure(scanner, monkeypatch, capsys):
    code = next(iter(scanner.CONTRACT_CODES))
    monkeypatch.setattr(scanner, "parse_cot_row", lambda row: {
        "contract_code": code, "report_date": "2026-07-17",
        "net_commercial": 1, "long_commercial": 1, "short_commercial": 1,
        "open_interest": 1})

    class BoomCursor:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("boom")

    class BoomConn:
        def cursor(self):
            return BoomCursor()
        def commit(self):
            pass

    scanner.store_cot_rows(BoomConn(), [{"raw": "row"}])
    assert "[COT_STORE_FAIL]" in capsys.readouterr().out


def test_run_scan_heartbeats_on_already_processed(scanner, monkeypatch):
    calls = []
    monkeypatch.setattr(scanner, "log_scan_run", lambda *a, **k: calls.append(a))

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(scanner, "init_db", lambda: FakeConn())
    monkeypatch.setattr(scanner, "update_cot_db", lambda conn, backfill=False: None)
    monkeypatch.setattr(scanner, "get_latest_report_date", lambda conn: "2026-07-17")
    monkeypatch.setattr(scanner, "is_week_processed", lambda conn, rd: True)

    scanner.run_scan(force=False, dry_run=True)
    assert any(a[:2] == ("COT", "ALREADY_PROCESSED") for a in calls)
