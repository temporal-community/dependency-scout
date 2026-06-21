"""Tests for the uv remediator (remediators/uv.py) — lock parsing + the reach/blocker decision.

The actual `uv lock` subprocess is mocked; we drive the lockfile content before/after to exercise
the three outcomes: reached the fix, moved-but-still-vulnerable, and capped-by-a-parent.
"""

from pathlib import Path

from remediators import get_remediator
from remediators.uv import UvRemediator, _ge, _locked_version


def _lock(version: str, name: str = "starlette") -> str:
    return f"""
version = 1

[[package]]
name = "anyio"
version = "4.0.0"

[[package]]
name = "{name}"
version = "{version}"
source = {{ registry = "https://pypi.org/simple" }}
"""


def test_locked_version_parses_toml():
    assert _locked_version(_lock("0.50.0"), "starlette") == "0.50.0"
    assert _locked_version(_lock("0.50.0"), "anyio") == "4.0.0"
    assert _locked_version(_lock("0.50.0"), "absent") == ""


def test_locked_version_tolerates_garbage():
    assert _locked_version("not valid toml [[[", "starlette") == ""


def test_ge_handles_pep440_and_fallback():
    assert _ge("1.3.1", "1.3.1") is True
    assert _ge("1.3.2", "1.3.1") is True
    assert _ge("1.0.1", "1.3.1") is False
    assert _ge("", "1.3.1") is False
    assert _ge("weird", "1.3.1") is True  # non-PEP440 → string compare ('w' > '1')


def test_affects_detects_package_in_lock():
    r = UvRemediator()
    assert r.affects(_lock("0.50.0"), "starlette") is True
    assert r.affects(_lock("0.50.0"), "fastapi") is False


def test_get_remediator_pip_and_uv():
    assert isinstance(get_remediator("pip"), UvRemediator)
    assert isinstance(get_remediator("uv"), UvRemediator)
    assert get_remediator("npm") is None  # not supported yet → escalate-only


def _patch_lock_run(monkeypatch, tmp_path: Path, after_version: str, returncode: int = 0):
    """Write a starting lock, then make `uv lock` rewrite it to `after_version`."""
    lock = tmp_path / "uv.lock"
    lock.write_text(_lock("0.50.0"))

    captured: dict = {}

    def fake_run(cmd, cwd, capture_output, text):  # noqa: ANN001
        captured["cmd"] = cmd
        if returncode == 0:
            lock.write_text(_lock(after_version))

        class P:
            pass

        p = P()
        p.returncode = returncode
        p.stdout = ""
        p.stderr = "boom" if returncode else ""
        return p

    monkeypatch.setattr("remediators.uv.subprocess.run", fake_run)
    return captured


def test_remediate_reaches_target(monkeypatch, tmp_path):
    captured = _patch_lock_run(monkeypatch, tmp_path, after_version="1.3.1")
    res = UvRemediator().remediate(tmp_path, "starlette", "1.3.1")
    assert res.old_version == "0.50.0"
    assert res.new_version == "1.3.1"
    assert res.changed is True
    assert res.reached_target is True
    assert "clears the advisory" in res.message
    # Surgical, cooldown-aware command: modern uv, target pinned exactly, freshness cooldown
    # overridden via --exclude-newer (a security fix is the cooldown's exception), everything
    # else kept pinned by --upgrade-package.
    cmd = captured["cmd"]
    assert cmd[:3] == ["uvx", "uv@latest", "lock"]
    assert "--upgrade-package" in cmd and "starlette==1.3.1" in cmd
    assert "--exclude-newer" in cmd


def test_remediate_moved_but_still_below_target(monkeypatch, tmp_path):
    # A parent allows 1.0.1 but not 1.3.1 — moved, still vulnerable.
    _patch_lock_run(monkeypatch, tmp_path, after_version="1.0.1")
    res = UvRemediator().remediate(tmp_path, "starlette", "1.3.1")
    assert res.changed is True
    assert res.reached_target is False
    assert "still below the safe 1.3.1" in res.message


def test_remediate_capped_by_parent_no_change(monkeypatch, tmp_path):
    # Resolver couldn't move it at all (parent pins it) — unchanged.
    _patch_lock_run(monkeypatch, tmp_path, after_version="0.50.0")
    res = UvRemediator().remediate(tmp_path, "starlette", "1.3.1")
    assert res.changed is False
    assert res.reached_target is False
    assert "constrains it" in res.message


def test_remediate_uv_failure_is_reported(monkeypatch, tmp_path):
    _patch_lock_run(monkeypatch, tmp_path, after_version="1.3.1", returncode=1)
    res = UvRemediator().remediate(tmp_path, "starlette", "1.3.1")
    assert res.changed is False
    assert res.reached_target is False
    assert "uv lock` failed" in res.message


def test_find_projects_discovers_affected_lockfiles(tmp_path):
    from remediators.runner import _find_projects

    r = UvRemediator()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "uv.lock").write_text(_lock("0.50.0"))
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "uv.lock").write_text(_lock("1.0.0", name="fastapi"))  # no starlette
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "uv.lock").write_text(_lock("0.50.0"))  # must be skipped

    found = _find_projects(tmp_path, r.lockfile_name, r.affects, "starlette")
    assert found == [tmp_path / "a"]  # only the affected project, .git skipped
