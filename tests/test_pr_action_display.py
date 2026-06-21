"""color === action: PRActionWorkflow reclassifies the displayed verdict to match what the
Scout will actually do, so there's no confusing "green that asks for review"."""

from models import RepoConfig, Verdict
from workflows.pr_action_workflow import _disposition, _displayed_verdict


def _v(classification, confidence=0.95, merge_recommendation=None):
    return Verdict(
        classification=classification,
        confidence=confidence,
        reasoning="...",
        flags=[],
        merge_recommendation=merge_recommendation,
    )


def test_green_below_confidence_threshold_shown_yellow(base_signals):
    cfg = RepoConfig(auto_merge_enabled=True, reviewers=["alice"], auto_merge_min_confidence=0.90)
    out = _displayed_verdict(_v("green", confidence=0.82), base_signals, cfg)
    assert _disposition(_v("green", confidence=0.82), base_signals, cfg) == "review"
    assert out.classification == "yellow"
    assert any("82%" in f and "90%" in f for f in out.flags)  # why: confidence < threshold


def test_green_that_auto_merges_stays_green(base_signals):
    cfg = RepoConfig(auto_merge_enabled=True, reviewers=["alice"], auto_merge_min_confidence=0.90)
    out = _displayed_verdict(_v("green", confidence=0.95), base_signals, cfg)
    assert out.classification == "green"
    assert out.flags == []  # no reclassification note


def test_green_hold_shown_yellow(base_signals):
    cfg = RepoConfig(auto_merge_enabled=True, reviewers=["alice"])
    out = _displayed_verdict(_v("green", merge_recommendation="hold"), base_signals, cfg)
    assert out.classification == "yellow"
    assert any("holding" in f for f in out.flags)


def test_red_block_stays_red(base_signals):
    cfg = RepoConfig(block_classifications=["red"])
    out = _displayed_verdict(_v("red"), base_signals, cfg)
    assert out.classification == "red"


def test_observe_only_keeps_risk_color(base_signals):
    out = _displayed_verdict(_v("green", confidence=0.5), base_signals, RepoConfig())
    assert _disposition(_v("green", confidence=0.5), base_signals, RepoConfig()) == "comment"
    assert out.classification == "green"  # no action → no remap


# ---------------------------------------------------------------------------
# Outcome labels (helpers/display.py) — the human-facing one-liners.
# ---------------------------------------------------------------------------


def test_outcome_label_security_escalation():
    from helpers.display import _outcome_label

    # live escalation outcome names the recommended fix version
    label = _outcome_label("escalated-security-1.3.1", None)
    assert "security escalation" in label
    assert "1.3.1" in label


def test_outcome_label_dry_run_escalation():
    from helpers.display import _outcome_label

    label = _outcome_label("dry-run-red-escalate-security", None)
    assert "escalate to security" in label


def test_verdict_from_result_maps_escalation_to_red():
    from scout import _verdict_from_result

    assert _verdict_from_result("escalated-security-1.3.1||https://x||hold") == "red"
