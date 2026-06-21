"""Shared ANSI color helpers and verdict display utilities for CLI scripts."""

from __future__ import annotations

import os

_G = "\033[32m"
_Y = "\033[33m"
_R = "\033[31m"
_C = "\033[36m"
_DIM = "\033[2m"
_B = "\033[1m"
_RST = "\033[0m"


def _g(s: str) -> str:
    return f"{_G}{s}{_RST}"


def _y(s: str) -> str:
    return f"{_Y}{s}{_RST}"


def _r(s: str) -> str:
    return f"{_R}{s}{_RST}"


def _dim(s: str) -> str:
    return f"{_DIM}{s}{_RST}"


def _bold(s: str) -> str:
    return f"{_B}{s}{_RST}"


def _info(s: str) -> str:
    return f"{_C}{s}{_RST}"


def _color_verdict(verdict: str) -> str:
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}[verdict]
    color = {"green": _G, "yellow": _Y, "red": _R}[verdict]
    return f"{emoji} {color}{_B}{verdict.upper()}{_RST}"


def _merge_rec_label(merge_rec: str | None) -> str:
    if merge_rec == "merge":
        return f"  {_G}⚡ merge recommended{_RST}"
    if merge_rec == "hold":
        return f"  {_Y}⏸ hold recommended{_RST}"
    return ""


def _outcome_label(result_str: str, merge_rec: str | None) -> str:
    """Show what the workflow actually did, falling back to recommendation for observe-only."""
    if result_str.startswith("dry-run-"):
        if result_str.endswith("-auto-merge"):
            return f"  {_DIM}🔍 would auto-merge{_RST}"
        if result_str.endswith("-escalate-security"):
            return f"  {_DIM}🔍 would escalate to security review{_RST}"
        if result_str.endswith("-block"):
            return f"  {_DIM}🔍 would close — suspicious{_RST}"
        if result_str.endswith("-review"):
            return f"  {_DIM}🔍 would request review{_RST}"
        return f"  {_DIM}🔍 would comment only{_RST}"
    if result_str.startswith("auto-merged"):
        return f"  {_G}✅ auto-merged{_RST}"
    if result_str == "human-approved-merged":
        return f"  {_G}✅ merged (approved){_RST}"
    if result_str == "closed-stale-branch":
        return f"  {_DIM}↩ closed — stale branch{_RST}"
    if result_str.startswith("escalated-security-"):
        fix = result_str.removeprefix("escalated-security-")
        return f"  {_R}🔐 closed — security escalation (→{fix}){_RST}"
    if result_str.startswith("blocked-"):
        return f"  {_R}🚫 closed — suspicious{_RST}"
    if result_str == "human-rejected":
        return f"  {_R}✗ rejected{_RST}"
    if result_str == "timed-out-awaiting-review":
        return f"  {_Y}⏱ review timed out{_RST}"
    return _merge_rec_label(merge_rec)


def _clf_name() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "Claude"
    if os.environ.get("OPENAI_API_KEY"):
        return "OpenAI"
    if os.environ.get("OLLAMA_HOST"):
        return "Ollama"
    if os.environ.get("CLASSIFIER"):
        return os.environ["CLASSIFIER"]
    return None
