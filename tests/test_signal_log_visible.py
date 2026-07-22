"""A signal-log write failure must be VISIBLE, not silently swallowed.

Reconciled from PA production drift (2026-07-22): the shared ``log_signal_intelligence``
helper in cot_scanner swallowed every write exception with a bare ``pass``. Production
had replaced it with a ``[SIGNAL_LOG_FAIL]`` diagnostic; adopt that (the VACUOUS-PASS
class), matching the si-squeeze reconciliation.
"""


def test_signal_log_failure_is_printed(scanner, capsys, monkeypatch):
    # The autouse fixture stubs log_signal_intelligence to a no-op; drop that so we
    # exercise the real implementation. (config injection is not via monkeypatch, so
    # undo() leaves it intact.)
    monkeypatch.undo()
    monkeypatch.setattr(scanner.os.path, "expanduser",
                        lambda p: "/does/not/exist/nope/signal_intelligence.db")
    scanner.log_signal_intelligence("2026-07-22", "COT_BULL", "USO", "BUY", 1)
    out = capsys.readouterr().out
    assert "[SIGNAL_LOG_FAIL]" in out
    assert "COT_BULL" in out and "USO" in out
