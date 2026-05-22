import os

import anthropic
from temporalio import activity
from temporalio.exceptions import ApplicationError

from activities.models import PackageSignals, Verdict
from helpers.prompts import CLASSIFIER_SYSTEM


@activity.defn(name="activities.classifier.classify")
async def classify(signals: PackageSignals) -> Verdict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        activity.logger.info("No ANTHROPIC_API_KEY — using rule-based classifier")
        return _rule_based(signals)

    client = anthropic.AsyncAnthropic()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": CLASSIFIER_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": f"Classify this dependency bump:\n\n{signals.model_dump_json(indent=2)}",
            }],
            tools=[{
                "name": "submit_verdict",
                "description": "Submit your supply chain risk classification",
                "input_schema": Verdict.model_json_schema(),
            }],
            tool_choice={"type": "tool", "name": "submit_verdict"},
        )
    except anthropic.AuthenticationError as exc:
        raise ApplicationError(str(exc), type="AuthenticationError", non_retryable=True) from exc
    except anthropic.BadRequestError as exc:
        raise ApplicationError(str(exc), type="BadRequestError", non_retryable=True) from exc

    tool_use = next(b for b in response.content if b.type == "tool_use")
    activity.logger.info(
        f"Classified {signals.package_name} {signals.new_version} as "
        f"{tool_use.input['classification']} ({tool_use.input['confidence']:.0%})"
    )
    return Verdict(**tool_use.input)


def _rule_based(signals: PackageSignals) -> Verdict:
    """Threshold-based fallback used when no ANTHROPIC_API_KEY is set."""
    flags: list[str] = []

    # Hard RED: known CVEs
    if signals.osv_vulnerabilities:
        return Verdict(
            classification="red",
            confidence=0.95,
            reasoning=f"Known vulnerabilities: {', '.join(signals.osv_vulnerabilities)}",
            flags=[f"CVE: {v}" for v in signals.osv_vulnerabilities],
        )

    # Collect yellow signals
    if signals.is_major_bump:
        flags.append("major version bump")
    if signals.release_age_hours < 24:
        flags.append(f"very fresh release ({signals.release_age_hours:.0f}h old)")
    elif signals.release_age_hours < 168:
        flags.append(f"recent release ({signals.release_age_hours:.0f}h old)")
    if signals.maintainer_changed:
        flags.append("maintainer changed")
    if signals.socket_alerts:
        flags.extend(signals.socket_alerts)
    if signals.socket_score is not None and signals.socket_score < 50:
        flags.append(f"low socket score ({signals.socket_score}/100)")
    if signals.weekly_downloads is not None and signals.weekly_downloads < 1_000:
        flags.append(f"low download count ({signals.weekly_downloads:,}/week)")

    if flags:
        return Verdict(
            classification="yellow",
            confidence=0.75,
            reasoning=f"[rule-based] Flagged: {', '.join(flags)}.",
            flags=flags,
        )

    downloads = f"{signals.weekly_downloads:,}" if signals.weekly_downloads else "unknown"
    return Verdict(
        classification="green",
        confidence=0.80,
        reasoning=(
            f"[rule-based] {signals.package_name} {signals.old_version}→{signals.new_version}: "
            f"patch/minor bump, {signals.release_age_hours:.0f}h old, no CVEs, "
            f"no maintainer changes, {downloads} weekly downloads."
        ),
        flags=[],
    )
