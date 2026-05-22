from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=capture,
        text=True,
    )
    return res.stdout if capture else ""


def bundle_host_repo(src: Path, out_bundle: Path) -> None:
    """Create a git bundle of `src` at `out_bundle`. Includes all refs."""
    out_bundle.parent.mkdir(parents=True, exist_ok=True)
    _git("bundle", "create", str(out_bundle), "--all", cwd=src)


def init_bare_repo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", "-b", "main", str(path))


def fetch_bundle_into_bare(*, bundle: Path, bare: Path, branch: str) -> None:
    _git("fetch", str(bundle), f"{branch}:{branch}", cwd=bare)


def _ref_exists(bare: Path, ref: str) -> bool:
    """Return True iff `ref` is a valid revision in the bare repo."""
    res = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=bare,
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def format_patch(*, bare: Path, branch: str, base: str = "main") -> str:
    if _ref_exists(bare, base):
        return _git(
            "format-patch", "--stdout", f"{base}..{branch}", cwd=bare, capture=True
        )
    return _git("format-patch", "--stdout", "--root", branch, cwd=bare, capture=True)
