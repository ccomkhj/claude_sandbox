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
    calls = []
    orig = s3_bucket.download_file

    def spy(Bucket, Key, Filename):
        calls.append((Bucket, Key))
        return orig(Bucket=Bucket, Key=Key, Filename=Filename)

    # Patch the boto3 client used by `dump.fetch`
    monkeypatch.setattr(dump, "_client", lambda: type("C", (), {
        "head_object": s3_bucket.head_object,
        "download_file": spy,
    })())
    dump.fetch("dumps", "prod/latest.dump")
    assert calls == []  # not re-downloaded


def test_fetch_redownloads_when_etag_changes(s3_bucket, sandbox_home):
    local1, etag1 = dump.fetch("dumps", "prod/latest.dump")
    s3_bucket.put_object(Bucket="dumps", Key="prod/latest.dump", Body=b"DUMPDATA-v2")
    local2, etag2 = dump.fetch("dumps", "prod/latest.dump")
    assert etag1 != etag2
    assert local2.read_bytes() == b"DUMPDATA-v2"
