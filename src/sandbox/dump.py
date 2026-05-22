from __future__ import annotations

import hashlib
from pathlib import Path

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
