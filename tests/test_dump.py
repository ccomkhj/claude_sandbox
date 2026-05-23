import subprocess as _sp
import time
from datetime import timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from sandbox import dump


@pytest.fixture
def s3_bucket(monkeypatch, sandbox_home):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="dumps")
        s3.put_object(Bucket="dumps", Key="prod/latest.dump", Body=b"DUMPDATA-v1")
        yield s3


def test_fetch_downloads_first_time(s3_bucket, sandbox_home):
    local, etag = dump.fetch("dumps", "prod/latest.dump")
    assert local.is_file()
    assert local.read_bytes() == b"DUMPDATA-v1"
    assert etag


def test_fetch_skips_when_cached(s3_bucket, sandbox_home, monkeypatch):
    dump.fetch("dumps", "prod/latest.dump")
    download_calls = []
    head_calls = []
    orig_download = s3_bucket.download_file
    orig_head = s3_bucket.head_object

    def download_spy(Bucket, Key, Filename):
        download_calls.append((Bucket, Key))
        return orig_download(Bucket=Bucket, Key=Key, Filename=Filename)

    def head_spy(Bucket, Key):
        head_calls.append((Bucket, Key))
        return orig_head(Bucket=Bucket, Key=Key)

    monkeypatch.setattr(dump, "_client", lambda: type("C", (), {
        "head_object": staticmethod(head_spy),
        "download_file": staticmethod(download_spy),
    })())
    dump.fetch("dumps", "prod/latest.dump")
    assert download_calls == []          # cache hit — no re-download
    assert head_calls == [("dumps", "prod/latest.dump")]  # but ETag is still checked


def test_fetch_redownloads_when_etag_changes(s3_bucket, sandbox_home):
    local1, etag1 = dump.fetch("dumps", "prod/latest.dump")
    s3_bucket.put_object(Bucket="dumps", Key="prod/latest.dump", Body=b"DUMPDATA-v2")
    local2, etag2 = dump.fetch("dumps", "prod/latest.dump")
    assert etag1 != etag2
    assert local2.read_bytes() == b"DUMPDATA-v2"


def test_fetch_separates_cache_by_bucket(s3_bucket, sandbox_home):
    # Create a second bucket with identical content (same ETag)
    s3_bucket.create_bucket(Bucket="dumps-staging")
    s3_bucket.put_object(Bucket="dumps-staging", Key="prod/latest.dump", Body=b"DUMPDATA-v1")

    p1, e1 = dump.fetch("dumps", "prod/latest.dump")
    p2, e2 = dump.fetch("dumps-staging", "prod/latest.dump")

    # Same content, so same ETag — but different cache files because bucket is in the key
    assert e1 == e2
    assert p1 != p2


def test_fetch_raises_on_missing_key(s3_bucket, sandbox_home):
    import botocore.exceptions
    with pytest.raises(botocore.exceptions.ClientError):
        dump.fetch("dumps", "does/not/exist.dump")


def test_fetch_from_postgres_url_runs_pg_dump_in_one_shot_container(sandbox_home, monkeypatch, tmp_path):
    """Mock docker run; verify pg_dump command argv."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        # Simulate pg_dump writing a file in the mounted volume
        # Find the -v mount in argv
        for i, a in enumerate(argv):
            if a == "-v":
                host_path, container_path = argv[i+1].split(":")
                Path(host_path).mkdir(parents=True, exist_ok=True)
                Path(host_path, "dump.dump").write_bytes(b"FAKE PG DUMP CONTENT")
                break
        return _sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("sandbox.dump.subprocess.run", fake_run)

    url = "postgres://test_user:test_pass@my-rds.example.com:5432/appdb"
    path, etag = dump.fetch_from_postgres_url(url, max_age=timedelta(hours=1))

    assert path.is_file()
    assert path.read_bytes() == b"FAKE PG DUMP CONTENT"
    assert etag  # non-empty string

    argv = captured["argv"]
    assert "docker" in argv[0]
    assert "run" in argv
    assert "--rm" in argv
    assert "postgres:16" in argv
    assert "pg_dump" in argv
    assert "--format=custom" in argv
    # URL is on argv but WITHOUT the password
    assert any("my-rds.example.com" in a for a in argv)
    # Password must not appear outside of the explicit -e PGPASSWORD=... pair
    non_env_args = [a for i, a in enumerate(argv) if a != "-e" and (i == 0 or argv[i-1] != "-e")]
    assert not any("test_pass" in a for a in non_env_args), \
        f"password leaked into non-env argv: {non_env_args}"


def test_fetch_from_postgres_url_passes_password_via_env_not_argv(sandbox_home, monkeypatch, tmp_path):
    captured_argv = []

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        for i, a in enumerate(argv):
            if a == "-v":
                host_path = argv[i+1].split(":")[0]
                Path(host_path).mkdir(parents=True, exist_ok=True)
                Path(host_path, "dump.dump").write_bytes(b"x")
                break
        return _sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("sandbox.dump.subprocess.run", fake_run)

    url = "postgres://u:secretpw@host/db"
    dump.fetch_from_postgres_url(url, max_age=timedelta(hours=1))

    # Password must not appear outside of the -e PGPASSWORD=... pair
    non_env_args = [a for i, a in enumerate(captured_argv)
                    if a != "-e" and (i == 0 or captured_argv[i-1] != "-e")]
    assert not any("secretpw" in a for a in non_env_args), f"password in non-env argv: {non_env_args}"
    # But PGPASSWORD env should have been passed via -e
    e_indexes = [i for i, a in enumerate(captured_argv) if a == "-e"]
    pg_env_args = [captured_argv[i+1] for i in e_indexes]
    assert any(a.startswith("PGPASSWORD=") and a.endswith("=secretpw") for a in pg_env_args), \
        f"PGPASSWORD env not passed: {pg_env_args}"


def test_fetch_from_postgres_url_caches_within_max_age(sandbox_home, monkeypatch):
    call_count = {"n": 0}

    def fake_run(argv, **kwargs):
        call_count["n"] += 1
        for i, a in enumerate(argv):
            if a == "-v":
                host_path = argv[i+1].split(":")[0]
                Path(host_path).mkdir(parents=True, exist_ok=True)
                Path(host_path, "dump.dump").write_bytes(b"x")
                break
        return _sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("sandbox.dump.subprocess.run", fake_run)

    url = "postgres://u@h/db"
    p1, e1 = dump.fetch_from_postgres_url(url, max_age=timedelta(hours=1))
    p2, e2 = dump.fetch_from_postgres_url(url, max_age=timedelta(hours=1))

    assert call_count["n"] == 1, "second call should hit the cache"
    assert p1 == p2
    assert e1 == e2


def test_fetch_from_postgres_url_strips_password_from_cache_key(sandbox_home, monkeypatch):
    call_count = {"n": 0}

    def fake_run(argv, **kwargs):
        call_count["n"] += 1
        for i, a in enumerate(argv):
            if a == "-v":
                host_path = argv[i+1].split(":")[0]
                Path(host_path).mkdir(parents=True, exist_ok=True)
                Path(host_path, "dump.dump").write_bytes(b"x")
                break
        return _sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("sandbox.dump.subprocess.run", fake_run)

    # Same URL, different password — should be the same cache key
    p1, _ = dump.fetch_from_postgres_url("postgres://u:pw1@h/db", max_age=timedelta(hours=1))
    p2, _ = dump.fetch_from_postgres_url("postgres://u:pw2@h/db", max_age=timedelta(hours=1))

    assert call_count["n"] == 1, "different passwords on the same URL should share the cache entry"
    assert p1 == p2


def test_fetch_from_postgres_url_refetches_when_cache_expired(sandbox_home, monkeypatch):
    call_count = {"n": 0}

    def fake_run(argv, **kwargs):
        call_count["n"] += 1
        for i, a in enumerate(argv):
            if a == "-v":
                host_path = argv[i+1].split(":")[0]
                Path(host_path).mkdir(parents=True, exist_ok=True)
                Path(host_path, "dump.dump").write_bytes(b"x")
                break
        return _sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("sandbox.dump.subprocess.run", fake_run)

    url = "postgres://u@h/db"
    # First fetch
    p1, _ = dump.fetch_from_postgres_url(url, max_age=timedelta(hours=1))

    # Backdate the cached file to "2 hours ago"
    backdated = time.time() - 2 * 3600
    import os
    os.utime(p1, (backdated, backdated))

    # Second fetch with 1h max_age should re-run
    p2, _ = dump.fetch_from_postgres_url(url, max_age=timedelta(hours=1))
    assert call_count["n"] == 2, "expired cache should trigger a re-fetch"


def test_fetch_from_postgres_url_uses_pgpassword_env_when_url_has_no_password(sandbox_home, monkeypatch):
    captured_argv = []

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        for i, a in enumerate(argv):
            if a == "-v":
                host_path = argv[i+1].split(":")[0]
                Path(host_path).mkdir(parents=True, exist_ok=True)
                Path(host_path, "dump.dump").write_bytes(b"x")
                break
        return _sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("sandbox.dump.subprocess.run", fake_run)
    monkeypatch.setenv("PGPASSWORD", "env-password-value")

    dump.fetch_from_postgres_url("postgres://u@host/db", max_age=timedelta(hours=1))

    e_indexes = [i for i, a in enumerate(captured_argv) if a == "-e"]
    pg_env_args = [captured_argv[i+1] for i in e_indexes]
    assert any(a == "PGPASSWORD=env-password-value" for a in pg_env_args)


def test_fetch_from_postgres_url_raises_when_pg_dump_fails(sandbox_home, monkeypatch):
    def fake_run(argv, **kwargs):
        return _sp.CompletedProcess(
            argv, 1, "", "pg_dump: error: connection to server failed"
        )

    monkeypatch.setattr("sandbox.dump.subprocess.run", fake_run)

    with pytest.raises(_sp.CalledProcessError):
        dump.fetch_from_postgres_url("postgres://u@h/db", max_age=timedelta(hours=1))
