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


def format_patch(*, bare: Path, branch: str, base: str = "main") -> str:
    # Check whether the base ref exists in the bare repo; if not, use --root
    # so that all commits on the branch are included in the patch.
    ref_check = subprocess.run(
        ["git", "rev-parse", "--verify", base],
        cwd=bare,
        capture_output=True,
        text=True,
    )
    if ref_check.returncode == 0:
        range_arg = f"{base}..{branch}"
        return _git("format-patch", "--stdout", range_arg, cwd=bare, capture=True)
    else:
        return _git(
            "format-patch", "--stdout", "--root", branch, cwd=bare, capture=True
        )
