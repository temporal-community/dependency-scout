import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from models import (
        PackageChecks,
        PRContext,
        PRFilesChecks,
        ActionsUsageChecks,
        RepoConfig,
        TriageResult,
        Verdict,
    )
    from workflows.package_triage_workflow import PackageTriageWorkflow

# color === action: the displayed classification is the action the Scout will take.
_DISPOSITION_COLOR = {
    "block": "red",
    "auto-merge": "green",
    "review": "yellow",
}  # "comment" → keep risk


def _disposition(verdict: "Verdict", signals: "PackageChecks", config: "RepoConfig") -> str:
    """What the Scout will do: 'block' | 'auto-merge' | 'review' | 'comment'.

    Single source of truth for the action decision — the dry-run report and the live path
    both read it so they can't drift."""
    if verdict.classification in config.block_classifications:
        return "block"
    normal_auto_merge = (
        verdict.classification in config.auto_merge_classifications
        and verdict.confidence >= config.auto_merge_min_confidence
        and verdict.merge_recommendation != "hold"
    )
    recommendation_auto_merge = verdict.merge_recommendation == "merge" and bool(
        signals.advisory.fixed_vulnerabilities
    )
    if config.auto_merge_enabled and (normal_auto_merge or recommendation_auto_merge):
        return "auto-merge"
    if config.reviewers:
        return "review"
    return "comment"


def _held_for_review_reason(verdict: "Verdict", config: "RepoConfig") -> str:
    """Explain why a GREEN-risk verdict won't auto-merge (so it's shown YELLOW)."""
    if verdict.merge_recommendation == "hold":
        return "shown yellow (review): the analysis recommended holding for a human"
    if not config.auto_merge_enabled:
        return "shown yellow (review): auto-merge is disabled for this repo"
    if verdict.classification not in config.auto_merge_classifications:
        return (
            f"shown yellow (review): '{verdict.classification}' is not in this repo's "
            f"auto-merge classifications {config.auto_merge_classifications}"
        )
    if verdict.confidence < config.auto_merge_min_confidence:
        return (
            f"shown yellow (review): analysis confidence {verdict.confidence:.0%} is below this "
            f"repo's {config.auto_merge_min_confidence:.0%} auto-merge threshold"
        )
    return "shown yellow (review)"


def _displayed_verdict(
    verdict: "Verdict", signals: "PackageChecks", config: "RepoConfig"
) -> "Verdict":
    """Reclassify so the verdict's color == the action taken: GREEN auto-merges, YELLOW gets
    reviewed, RED is blocked. A GREEN that won't auto-merge (low confidence, auto-merge off,
    hold, …) is shown YELLOW with a note explaining why — no more confusing "green that asks
    for review". Observe-only (no action) keeps the risk color. Flags/reasoning are preserved."""
    disp = _disposition(verdict, signals, config)
    target = _DISPOSITION_COLOR.get(disp)
    if target is None or target == verdict.classification:
        return verdict
    note = (
        _held_for_review_reason(verdict, config)
        if target == "yellow"
        else f"shown {target} to match the action taken ({disp})"
    )
    return verdict.model_copy(update={"classification": target, "flags": verdict.flags + [note]})


@workflow.defn
class PRActionWorkflow:
    """
    Per-PR action workflow. Fetches repo config, runs (or attaches to) PackageTriageWorkflow,
    and acts based on verdict + config: comment, auto-merge, request review, or escalate.

    Workflow ID: pr-action-{repo}-{pr_number}
    """

    def __init__(self) -> None:
        self._human_decision: str | None = None
        self._approver: str = ""

    async def _try_merge(self, pr: "PRContext", opts: dict) -> bool:
        """Attempt merge; if branch is stale, close the PR and return False. Re-raises all other errors."""
        try:
            await workflow.execute_activity("activities.platform.merge_pr", args=[pr], **opts)
            return True
        except ActivityError as e:
            if (
                isinstance(e.cause, ApplicationError)
                and getattr(e.cause, "type", None) == "stale_branch"
            ):
                await workflow.execute_activity(
                    "activities.platform.close_pr",
                    args=[
                        pr,
                        "Branch is behind the base branch — closing so Dependabot can recreate with an updated branch.",
                    ],
                    **opts,
                )
                return False
            raise

    def _dry_run_outcome(
        self, verdict: "Verdict", signals: "PackageChecks", config: "RepoConfig"
    ) -> str:
        """Report the action that would be taken, without performing it.

        Uses the same _disposition() the live path uses, and encodes the *effective* color
        (color === action) so the CLI colors the verdict to match what would happen."""
        disp = _disposition(verdict, signals, config)
        eff = _displayed_verdict(verdict, signals, config)
        return f"dry-run-{eff.classification}-{disp}"

    @workflow.signal
    def submit_decision(self, decision: str, approver: str = "") -> None:
        """Send 'approve' to merge, anything else to reject.

        approver should be the GitHub username of the person making the decision.
        The workflow validates it against config.reviewers before honoring it.
        Setting approver is not cryptographically enforced (anyone who can reach
        Temporal can claim any username) — the proper fix is to source this signal
        exclusively from HMAC-verified GitHub webhook review events.
        """
        self._human_decision = decision
        self._approver = approver

    @workflow.query
    def status(self) -> dict:
        return {
            "awaiting_human": self._human_decision is None,
            "human_decision": self._human_decision,
            "approver": self._approver,
        }

    @workflow.run
    async def run(self, pr: PRContext) -> str:
        retry = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=2))
        opts: dict = dict(
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry,
        )

        config: RepoConfig = await workflow.execute_activity(
            "activities.platform.fetch_repo_config", pr, result_type=RepoConfig, **opts
        )

        # Cross-repo dedup: multiple repos (or sub-projects in a monorepo) seeing the
        # same package bump share one PackageTriageWorkflow. The date suffix gives a
        # 24-hour TTL so stale verdicts don't persist indefinitely.
        #
        # Race condition: when concurrent PRActionWorkflow instances race to start the
        # same child ID, only one wins. The losers catch "already started" and fall back
        # to await_triage_result — an activity that attaches to the running child and
        # waits for its result. Dedup is preserved without races.
        #
        # check_pr_files runs in parallel: it's a fast per-PR check for CI/infra files
        # that should never appear in a routine dep-bump.
        date_key = workflow.now().strftime("%Y-%m-%d")
        triage_id = f"triage-{pr.ecosystem}-{pr.package_name}-{pr.new_version}-{date_key}"

        async def _run_triage() -> TriageResult:
            try:
                return await workflow.execute_child_workflow(  # type: ignore[call-overload]
                    PackageTriageWorkflow.run,
                    args=[
                        pr.ecosystem,
                        pr.package_name,
                        pr.old_version,
                        pr.new_version,
                        config.extra_check_activities,
                    ],
                    id=triage_id,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    parent_close_policy=ParentClosePolicy.ABANDON,
                    execution_timeout=timedelta(minutes=15),
                    result_type=TriageResult,
                )
            except Exception as e:
                if "already started" in str(e).lower():
                    workflow.logger.info(
                        f"Triage {triage_id!r} already running — attaching to existing execution"
                    )
                    return await workflow.execute_activity(  # type: ignore[return-value]
                        "activities.platform.await_triage_result",
                        args=[triage_id],
                        result_type=TriageResult,
                        start_to_close_timeout=timedelta(minutes=20),
                        retry_policy=retry,
                    )
                raise

        triage_result, pr_files, actions_usage = await asyncio.gather(
            _run_triage(),
            workflow.execute_activity(
                "activities.platform.check_pr_files",
                pr,
                result_type=PRFilesChecks,
                **opts,
            ),
            workflow.execute_activity(
                "activities.platform.check_actions_usage",
                pr,
                result_type=ActionsUsageChecks,
                **opts,
            ),
        )
        verdict = triage_result.verdict
        signals = triage_result.signals

        # Hard escalation: unexpected CI/infra/script files in a dep-bump PR override
        # the triage verdict. Package-level analysis can't see the consuming-repo PR diff;
        # this is the only place that check happens.
        if pr_files.unexpected_files:
            verdict = Verdict(
                classification="red",
                confidence=0.95,
                reasoning=(
                    "CRITICAL: Non-dependency files changed alongside this dependency bump — "
                    "possible supply chain attack or repository compromise."
                ),
                flags=[f"unexpected file in PR: {f}" for f in pr_files.unexpected_files]
                + verdict.flags,
                release_age_hours=verdict.release_age_hours,
            )

        # Hard gate: enforce min_release_age_hours per repo policy regardless of LLM verdict.
        # The shared PackageTriageWorkflow verdict may have been produced for a different repo
        # with a different age policy, or the LLM may have ignored the age signal.
        if (
            verdict.release_age_hours is not None
            and verdict.release_age_hours < config.min_release_age_hours
        ):
            updates: dict = {}
            if verdict.classification == "green":
                updates["classification"] = "yellow"
                updates["flags"] = verdict.flags + [
                    f"release too fresh: {verdict.release_age_hours:.0f}h "
                    f"< {config.min_release_age_hours}h minimum for this repo"
                ]
            if verdict.merge_recommendation == "merge":
                # Repo age policy is a hard floor — LLM cannot bypass it.
                updates.setdefault("flags", verdict.flags)
                updates["flags"] = updates["flags"] + [
                    f"LLM merge recommendation suppressed: release is {verdict.release_age_hours:.0f}h old "
                    f"(< {config.min_release_age_hours}h minimum for this repo)"
                ]
                updates["merge_recommendation"] = None
            if updates:
                verdict = verdict.model_copy(update=updates)

        if (
            verdict.classification == "green"
            and verdict.new_dependency_count >= config.max_new_dependencies
        ):
            verdict = verdict.model_copy(
                update={
                    "classification": "yellow",
                    "flags": verdict.flags
                    + [
                        f"{verdict.new_dependency_count} new direct dependencies added "
                        f"(max_new_dependencies: {config.max_new_dependencies} for this repo)"
                    ],
                }
            )

        if actions_usage.flags:
            verdict = verdict.model_copy(update={"flags": verdict.flags + actions_usage.flags})

        # Everything above is read-only (fetch config, run triage, file/actions checks,
        # apply repo gates). Everything below mutates the PR — comment, label, close,
        # merge, request review. In dry-run mode we report the action that *would* be
        # taken and stop here, touching nothing on the platform.
        if pr.dry_run:
            return self._dry_run_outcome(verdict, signals, config)

        # color === action: post the comment with the effective classification (e.g. a green
        # that won't auto-merge is shown yellow + a note saying why) so the badge matches what
        # the Scout actually does. Action decisions below still use the original verdict.
        displayed = _displayed_verdict(verdict, signals, config)
        comment_url: str = await workflow.execute_activity(
            "activities.platform.comment", args=[pr, displayed, signals], result_type=str, **opts
        )
        url_suffix = f"||{comment_url}" if comment_url else ""
        mr_suffix = f"||{verdict.merge_recommendation}" if verdict.merge_recommendation else ""

        if verdict.classification in config.block_classifications:
            if verdict.merge_recommendation == "merge":
                # Repo policy overrides the LLM's recommendation — log but still block.
                workflow.logger.warning(
                    f"LLM set merge_recommendation='merge' for {verdict.classification.upper()} "
                    f"but that classification is in block_classifications — repo policy wins."
                )
            await workflow.execute_activity(
                "activities.platform.label", args=[pr, "supply-chain-suspicious"], **opts
            )
            reason = (
                f"Triage agent classified this as **{verdict.classification.upper()}**. "
                f"Reason: {', '.join(verdict.flags) or verdict.reasoning[:200]}"
            )
            await workflow.execute_activity(
                "activities.platform.close_pr", args=[pr, reason, True], **opts
            )
            return f"blocked-{displayed.classification}{url_suffix}{mr_suffix}"

        # Auto-merge logic with merge_recommendation support:
        # - merge_recommendation="hold" suppresses auto-merge even for green classifications
        # - merge_recommendation="merge" enables auto-merge for non-auto_merge_classifications
        #   only when there are confirmed fixed CVEs — this is a CVE-urgency override, not a
        #   general "LLM thinks it looks fine" override
        normal_auto_merge = (
            verdict.classification in config.auto_merge_classifications
            and verdict.confidence >= config.auto_merge_min_confidence
            and verdict.merge_recommendation != "hold"
        )
        recommendation_auto_merge = verdict.merge_recommendation == "merge" and bool(
            signals.advisory.fixed_vulnerabilities
        )
        if config.auto_merge_enabled and (normal_auto_merge or recommendation_auto_merge):
            merged = await self._try_merge(pr, opts)
            if not merged:
                return f"closed-stale-branch{url_suffix}{mr_suffix}"
            if recommendation_auto_merge and not normal_auto_merge:
                return f"auto-merged-security-context{url_suffix}{mr_suffix}"
            return f"auto-merged{url_suffix}{mr_suffix}"

        if config.reviewers:
            await workflow.execute_activity(
                "activities.platform.request_review", args=[pr, config.reviewers], **opts
            )

            # Wait for a decision from an authorized reviewer (max 7 days).
            # Re-check authorization each time a signal arrives in case an
            # unauthorized signal arrived first.
            while True:
                try:
                    await workflow.wait_condition(
                        lambda: self._human_decision is not None,
                        timeout=timedelta(days=7),
                    )
                except asyncio.TimeoutError:
                    workflow.logger.warning("Human review wait timed out after 7 days")
                    return f"timed-out-awaiting-review{url_suffix}{mr_suffix}"
                if not config.reviewers or self._approver in config.reviewers:
                    break
                # Unauthorized signal — log and keep waiting
                workflow.logger.warning(
                    f"submit_decision from '{self._approver}' who is not in "
                    f"config.reviewers {config.reviewers} — ignoring"
                )
                self._human_decision = None
                self._approver = ""

            if self._human_decision == "approve":
                merged = await self._try_merge(pr, opts)
                if not merged:
                    return f"closed-stale-branch{url_suffix}{mr_suffix}"
                return f"human-approved-merged{url_suffix}{mr_suffix}"
            return f"human-rejected{url_suffix}{mr_suffix}"

        # Default observe-only: comment posted, no further action.
        return f"observe-only-{displayed.classification}{url_suffix}{mr_suffix}"
