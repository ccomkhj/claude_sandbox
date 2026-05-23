import json

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from sandbox.aws import StsUnavailable, mint_scoped_s3_token, _session_policy


def test_session_policy_grants_get_object_and_list_bucket_only():
    policy = json.loads(_session_policy(["bucket-one", "bucket-two"]))
    statements = policy["Statement"]
    actions = sorted({a for stmt in statements for a in stmt["Action"]})
    assert actions == ["s3:GetObject", "s3:ListBucket"]
    resources = sorted({r for stmt in statements for r in stmt["Resource"]})
    assert resources == [
        "arn:aws:s3:::bucket-one",
        "arn:aws:s3:::bucket-one/*",
        "arn:aws:s3:::bucket-two",
        "arn:aws:s3:::bucket-two/*",
    ]


def test_mint_scoped_s3_token_returns_credentials_dict(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        creds = mint_scoped_s3_token(profile=None, buckets=["b1"])
    assert "access_key_id" in creds
    assert "secret_access_key" in creds
    assert "session_token" in creds
    assert "region" in creds
    assert creds["region"] == "us-east-1"


def test_mint_scoped_s3_token_passes_policy_to_sts(monkeypatch):
    """Capture the Policy argument passed to STS so we can verify scoping."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")

    captured = {}

    class FakeSts:
        def get_federation_token(self, **kwargs):
            captured.update(kwargs)
            return {
                "Credentials": {
                    "AccessKeyId": "ASIA-FAKE",
                    "SecretAccessKey": "secret-fake",
                    "SessionToken": "session-fake",
                    "Expiration": None,
                }
            }

    class FakeSession:
        def __init__(self, **kw):
            pass
        def client(self, name):
            assert name == "sts"
            return FakeSts()
        region_name = "us-west-2"

    monkeypatch.setattr("sandbox.aws.boto3.Session", lambda **kw: FakeSession(**kw))

    creds = mint_scoped_s3_token(profile=None, buckets=["my-bucket"])
    assert "Policy" in captured
    policy = json.loads(captured["Policy"])
    object_arns = {r for stmt in policy["Statement"] for r in stmt["Resource"]}
    assert "arn:aws:s3:::my-bucket" in object_arns
    assert "arn:aws:s3:::my-bucket/*" in object_arns
    assert creds["session_token"] == "session-fake"
    assert creds["region"] == "us-west-2"


def test_mint_scoped_s3_token_uses_named_profile(monkeypatch):
    used_profile = {}

    class FakeSts:
        def get_federation_token(self, **kwargs):
            return {
                "Credentials": {
                    "AccessKeyId": "x", "SecretAccessKey": "y",
                    "SessionToken": "z", "Expiration": None,
                }
            }

    class FakeSession:
        def __init__(self, **kw):
            used_profile["profile_name"] = kw.get("profile_name")
        def client(self, name):
            return FakeSts()
        region_name = "us-east-1"

    monkeypatch.setattr("sandbox.aws.boto3.Session", lambda **kw: FakeSession(**kw))
    mint_scoped_s3_token(profile="my-readonly-profile", buckets=["b"])
    assert used_profile["profile_name"] == "my-readonly-profile"


def test_mint_scoped_s3_token_raises_StsUnavailable_when_no_credentials(monkeypatch):
    """When no creds are configured, boto raises NoCredentialsError; we wrap it."""

    class FakeSts:
        def get_federation_token(self, **kwargs):
            raise botocore.exceptions.NoCredentialsError()

    class FakeSession:
        def __init__(self, **kw): pass
        def client(self, name):
            return FakeSts()
        region_name = None

    monkeypatch.setattr("sandbox.aws.boto3.Session", lambda **kw: FakeSession(**kw))

    with pytest.raises(StsUnavailable, match="sts:GetFederationToken"):
        mint_scoped_s3_token(profile=None, buckets=["b"])


def test_mint_scoped_s3_token_raises_StsUnavailable_when_access_denied(monkeypatch):
    class FakeSts:
        def get_federation_token(self, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetFederationToken",
            )

    class FakeSession:
        def __init__(self, **kw): pass
        def client(self, name): return FakeSts()
        region_name = "us-east-1"

    monkeypatch.setattr("sandbox.aws.boto3.Session", lambda **kw: FakeSession(**kw))

    with pytest.raises(StsUnavailable, match="--aws-unsafe-passthrough"):
        mint_scoped_s3_token(profile=None, buckets=["b"])


def test_mint_scoped_s3_token_raises_on_empty_buckets():
    with pytest.raises(ValueError, match="buckets cannot be empty"):
        mint_scoped_s3_token(profile=None, buckets=[])
