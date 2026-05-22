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
