import subprocess
from pathlib import Path

import pytest

from sandbox import repo


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


@pytest.fixture
def src_repo(tmp_path):
    r = tmp_path / "src"
    r.mkdir()
    git(r, "init", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "hello.txt").write_text("hi\n")
    git(r, "add", ".")
    git(r, "commit", "-m", "init")
    return r


def test_bundle_host_repo_creates_clonable_bundle(src_repo, tmp_path):
    bundle = tmp_path / "repo.bundle"
    repo.bundle_host_repo(src_repo, bundle)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bundle), str(clone)], check=True)
    assert (clone / "hello.txt").read_text() == "hi\n"


def test_init_bare_repo(tmp_path):
    bare = tmp_path / "bare.git"
    repo.init_bare_repo(bare)
    assert (bare / "HEAD").exists()
    # bare repos do not contain a working tree
    assert not (bare / "hello.txt").exists()


def test_fetch_bundle_into_bare(src_repo, tmp_path):
    # Make a branch in src and bundle it
    git(src_repo, "checkout", "-b", "feature")
    (src_repo / "more.txt").write_text("more\n")
    git(src_repo, "add", ".")
    git(src_repo, "commit", "-m", "feature")
    branch_bundle = tmp_path / "branch.bundle"
    subprocess.run(
        ["git", "-C", str(src_repo), "bundle", "create", str(branch_bundle), "feature"],
        check=True,
    )

    bare = tmp_path / "bare.git"
    repo.init_bare_repo(bare)
    repo.fetch_bundle_into_bare(bundle=branch_bundle, bare=bare, branch="feature")

    out = git(bare, "log", "--format=%s", "feature")
    assert "feature" in out


def test_format_patch_returns_non_empty(src_repo, tmp_path):
    git(src_repo, "checkout", "-b", "feature")
    (src_repo / "more.txt").write_text("more\n")
    git(src_repo, "add", ".")
    git(src_repo, "commit", "-m", "feature")
    branch_bundle = tmp_path / "branch.bundle"
    subprocess.run(
        ["git", "-C", str(src_repo), "bundle", "create", str(branch_bundle), "feature"],
        check=True,
    )
    bare = tmp_path / "bare.git"
    repo.init_bare_repo(bare)
    repo.fetch_bundle_into_bare(bundle=branch_bundle, bare=bare, branch="feature")

    patch = repo.format_patch(bare=bare, branch="feature", base="main")
    assert "more.txt" in patch
    assert "diff --git" in patch
