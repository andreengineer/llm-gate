"""Regression tests for the dedup guard in app/cache.py.

Context: DEDUP_MIN_LINES guard. The guard exists (spec §3) to stop agents that
concatenate retrieved documents without dedup. The old implementation
normalised by min(len(A), len(B)) with no minimum block size, so ANY repeated
one-liner scored 1.0 and hard-blocked the request on ENFORCE ports. That false
positive rejected every long Hermes agent loop with HTTP 400 dedup_rejected and
took the whole daily-cron pipeline down for 11 consecutive runs.

These tests pin: (1) trivial blocks are never blocked, (2) genuine doc
concatenation is still blocked, (3) reported message indices are the ORIGINAL
positions in the request.
"""
from app.cache import DEDUP_MIN_LINES, dedup_ratio, reject_if_duplicate_blocks


def _doc(n: int, prefix: str = "retrieved doc line") -> str:
    return "\n".join(f"{prefix} {i}" for i in range(n))


# ---------------------------------------------------------------- false positives

def test_identical_short_tool_acks_not_blocked():
    """Two identical one-line tool results are normal agent-loop traffic."""
    msgs = [
        {"role": "tool", "content": '{"ok": true}'},
        {"role": "assistant", "content": '{"ok": true}'},
    ]
    assert reject_if_duplicate_blocks(msgs) is None
    assert dedup_ratio('{"ok": true}', '{"ok": true}') == 0.0


def test_short_message_subset_of_system_prompt_not_blocked():
    """A one-liner that also occurs in the (large) system prompt is not a dup."""
    system = _doc(50, "system line") + "\nYou are Hermes Agent"
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": "You are Hermes Agent"},
    ]
    assert reject_if_duplicate_blocks(msgs) is None
    assert dedup_ratio(system, "You are Hermes Agent") == 0.0


def test_repeated_short_tool_results_in_long_loop_not_blocked():
    """The real outage: a 40-message loop with repeated small tool acks."""
    msgs = [{"role": "system", "content": _doc(30)}]
    for i in range(40):
        msgs.append({"role": "tool", "content": '{"success": true}'})
        msgs.append({"role": "assistant", "content": f"step {i}"})
    assert reject_if_duplicate_blocks(msgs) is None


def test_blocks_just_under_minimum_are_inert():
    """Blocks of DEDUP_MIN_LINES-1 distinct lines are never compared."""
    small = _doc(DEDUP_MIN_LINES - 1, "a")
    assert dedup_ratio(small, small) == 0.0


def test_blank_lines_do_not_dilute_overlap():
    """Blank/whitespace lines must be ignored, not counted as distinct."""
    doc = _doc(20, "a")
    padded = "\n".join(l + "\n\n" for l in doc.splitlines()) + "\n   \n"
    assert dedup_ratio(doc, padded) == 1.0


# ---------------------------------------------------------------- true positives

def test_genuine_duplicated_document_still_blocked():
    msgs = [
        {"role": "user", "content": _doc(40)},
        {"role": "user", "content": _doc(40) + "\nextra tail line"},
    ]
    verdict = reject_if_duplicate_blocks(msgs)
    assert verdict is not None
    assert ">60% duplicate" in verdict


def test_overlapping_documents_above_threshold_still_blocked():
    """Two 20-line blocks sharing 18 lines -> ratio 0.9 > 0.6 -> blocked."""
    a = _doc(20, "shared")
    b = _doc(18, "shared") + "\n" + _doc(2, "unique")
    assert dedup_ratio(a, b) > 0.6
    assert reject_if_duplicate_blocks([{"role": "user", "content": a},
                                       {"role": "user", "content": b}]) is not None


def test_distinct_documents_not_blocked():
    a, b = _doc(30, "doc-a"), _doc(30, "doc-b")
    assert dedup_ratio(a, b) == 0.0
    assert reject_if_duplicate_blocks([{"role": "user", "content": a},
                                       {"role": "user", "content": b}]) is None


# ---------------------------------------------------------------- reporting accuracy

def test_reported_indices_are_original_request_positions():
    """Non-string contents are skipped for comparison but must not shift indices."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None},          # filtered out
        {"role": "user", "content": _doc(30)},           # original index 2
        {"role": "user", "content": None},               # filtered out
        {"role": "user", "content": ""},                 # filtered out
        {"role": "user", "content": _doc(30)},           # original index 5
    ]
    verdict = reject_if_duplicate_blocks(msgs)
    assert verdict is not None
    assert "messages[2]" in verdict and "messages[5]" in verdict


# ------------------------------------------- ES 2026-09-16 residual false positives
# DEDUP_MIN_LINES alone was NOT sufficient: the guard still compared
# assistant<->assistant / tool<->tool / system<->user pairs, so >=6-line status
# blocks repeated across an agent loop still scored >0.6 and hard-blocked on
# enforce ports. Live casualty: alpha-global-recon-daily, 10 consecutive 400s
# (reason "messages[25] and messages[28] are >60% duplicate") on 2026-09-16T11:01.

def test_repeated_long_assistant_status_blocks_not_blocked():
    """A >=6-line progress table repeated across assistant turns is normal."""
    status = "\n".join(f"- source {i}: ok" for i in range(8))
    msgs = [
        {"role": "user", "content": "run recon"},
        {"role": "assistant", "content": status},
        {"role": "assistant", "content": status + "\n- new line: ok"},
    ]
    assert reject_if_duplicate_blocks(msgs) is None


def test_repeated_long_tool_result_tables_not_blocked():
    """Two near-identical >=6-line tool result tables are agent-loop traffic."""
    table = "\n".join(f"| host{i}.example | open |" for i in range(7))
    msgs = [
        {"role": "user", "content": "scan hosts"},
        {"role": "assistant", "content": "scanning"},
        {"role": "tool", "content": table},
        {"role": "tool", "content": table + "\n| host-note | open |"},
    ]
    assert reject_if_duplicate_blocks(msgs) is None


def test_user_doc_dump_is_still_blocked():
    """The spec §3 failure mode — same retrieved doc in successive user turns."""
    doc = _doc(12)
    msgs = [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": doc},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": doc + "\nretrieved doc line 99"},
    ]
    verdict = reject_if_duplicate_blocks(msgs)
    assert verdict is not None
    assert "messages[1]" in verdict and "messages[3]" in verdict


def test_dedup_guard_log_only_env_escape_hatch(monkeypatch):
    """DEDUP_GUARD_LOG_ONLY=1 must downgrade dedup to observe-and-allow."""
    from app.routes import chat

    seen = []
    monkeypatch.setattr(chat, "record_would_block",
                        lambda *a, **k: seen.append(a))
    monkeypatch.setenv("DEDUP_GUARD_LOG_ONLY", "1")
    chat._tunable_block(False, "run-1", "alpha-global-recon-daily", 400,
                        "dedup_rejected", "messages[25] and messages[28] are >60% duplicate")
    assert seen, "escape hatch did not record a would_block event"

    monkeypatch.delenv("DEDUP_GUARD_LOG_ONLY", raising=False)
    try:
        chat._tunable_block(False, "run-2", "alpha-global-recon-daily", 400,
                            "dedup_rejected", "messages[25] and messages[28] are >60% duplicate")
    except Exception as exc:  # the gate must hard-block again once unset
        assert "dedup_rejected" in str(exc) or "400" in str(exc)
    else:
        raise AssertionError("enforce mode no longer blocks after env unset")
