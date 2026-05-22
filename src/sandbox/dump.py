from __future__ import annotations

from pathlib import Path

import boto3

from sandbox.session import sandbox_home


def cache_dir() -> Path:
    d = sandbox_home() / "cache" / "dumps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client():
    return boto3.client("s3")


def _safe(key: str) -> str:
    return key.replace("/", "_").replace(":", "_")


def fetch(bucket: str, key: str) -> tuple[Path, str]:
    """Download s3://bucket/key to the host cache. Skip if a file matching the
    current ETag is already cached. Return (local_path, etag)."""
    client = _client()
    head = client.head_object(Bucket=bucket, Key=key)
    etag = head["ETag"].strip('"')
    local = cache_dir() / f"{_safe(key)}.{etag}.dump"
    if local.exists():
        return local, etag
    tmp = local.with_suffix(local.suffix + ".part")
    client.download_file(Bucket=bucket, Key=key, Filename=str(tmp))
    tmp.rename(local)
    return local, etag
