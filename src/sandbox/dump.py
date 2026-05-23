from __future__ import annotations

import hashlib
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import boto3

from sandbox.session import sandbox_home


def cache_dir() -> Path:
    d = sandbox_home() / "cache" / "dumps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client():
    return boto3.client("s3")


def _cache_path(bucket: str, key: str, etag: str) -> Path:
    digest = hashlib.sha256(f"{bucket}/{key}".encode()).hexdigest()[:16]
    return cache_dir() / f"{digest}.{etag}.dump"


def fetch(bucket: str, key: str) -> tuple[Path, str]:
    """Download s3://bucket/key to host cache, keyed by bucket+key+ETag.

    Skips the download if a file matching the current ETag is already cached.
    Returns (local_path, etag).

    Raises:
        botocore.exceptions.ClientError: on S3 head_object or download
            failure (missing key, AccessDenied, NoSuchBucket, etc.). The
            CLI layer wraps this with a credential hint.
        botocore.exceptions.NoCredentialsError: when no AWS credentials
            are available in env, ~/.aws, or instance role.
    """
    client = _client()
    head = client.head_object(Bucket=bucket, Key=key)
    etag = head["ETag"].strip('"')
    local = _cache_path(bucket, key, etag)
    if local.exists():
        return local, etag
    tmp = local.with_suffix(local.suffix + ".part")
    try:
        client.download_file(Bucket=bucket, Key=key, Filename=str(tmp))
        tmp.rename(local)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return local, etag


def _strip_url_password(url: str) -> tuple[str, str]:
    """Return (url_without_password, password_or_empty_string)."""
    parts = urlsplit(url)
    if not parts.password:
        return url, ""
    # Rebuild netloc without password
    user = parts.username or ""
    host = parts.hostname or ""
    port_part = f":{parts.port}" if parts.port else ""
    new_netloc = f"{user}@{host}{port_part}" if user else f"{host}{port_part}"
    rebuilt = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    return rebuilt, parts.password


def fetch_from_postgres_url(url: str, *, max_age: timedelta = timedelta(hours=1)) -> tuple[Path, str]:
    """Run pg_dump in a one-shot postgres:16 container against a live URL.

    Caches the dump file by URL hash (password stripped). Returns the cached
    file if it's younger than `max_age`; otherwise re-runs pg_dump.

    The password is read in this priority:
      1. embedded in the URL (e.g. postgres://u:p@h/db)
      2. PGPASSWORD env var on the host

    Password is passed to the container as `-e PGPASSWORD=...`, never on argv.
    The URL passed to pg_dump on argv has its password stripped.

    Returns (local_path, etag_like_string) matching the existing S3 fetch contract.

    Raises:
        subprocess.CalledProcessError: if pg_dump exits non-zero (connection
            failed, auth failed, etc.). stderr surfaces the pg_dump error.
    """
    url_no_password, embedded_password = _strip_url_password(url)
    url_hash = hashlib.sha256(url_no_password.encode()).hexdigest()[:16]

    cache_root = cache_dir() / "postgres-source"
    cache_root.mkdir(parents=True, exist_ok=True)

    cached_path = cache_root / f"pg-{url_hash}.dump"

    # Cache hit if file exists AND is younger than max_age
    if cached_path.exists():
        age = time.time() - cached_path.stat().st_mtime
        if age < max_age.total_seconds():
            return cached_path, f"{url_hash}.{int(cached_path.stat().st_mtime)}"

    # Cache miss: run pg_dump in a one-shot container
    password = embedded_password or os.environ.get("PGPASSWORD", "")

    # Use a per-call tmpdir so concurrent runs don't collide
    tmpdir = cache_root / f".tmp-{url_hash}-{int(time.time() * 1000)}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        env_args = ["-e", f"PGPASSWORD={password}"] if password else []
        argv = [
            "docker", "run", "--rm",
            "-v", f"{tmpdir}:/out",
            *env_args,
            "postgres:16",
            "pg_dump",
            "--format=custom",
            "--no-owner", "--no-acl", "--no-comments",
            "--quote-all-identifiers",
            "--file=/out/dump.dump",
            url_no_password,
        ]
        result = subprocess.run(argv, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                returncode=result.returncode, cmd=argv,
                output=result.stdout, stderr=result.stderr,
            )

        produced = tmpdir / "dump.dump"
        if not produced.is_file():
            raise RuntimeError(
                f"pg_dump exited 0 but produced no file at {produced}. "
                f"stderr: {result.stderr}"
            )
        produced.replace(cached_path)
    finally:
        # Best-effort cleanup of the tmp dir (may already be empty after replace)
        try:
            tmpdir.rmdir()
        except OSError:
            pass

    return cached_path, f"{url_hash}.{int(cached_path.stat().st_mtime)}"
