from __future__ import annotations

import json

import boto3
import botocore.exceptions


class StsUnavailable(RuntimeError):
    """Raised when STS GetFederationToken cannot be called or returns an error."""


def _session_policy(buckets: list[str]) -> str:
    """Render an AWS session policy granting read-only S3 access to the given buckets."""
    object_arns = [f"arn:aws:s3:::{b}/*" for b in buckets]
    bucket_arns = [f"arn:aws:s3:::{b}" for b in buckets]
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": object_arns},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": bucket_arns},
        ],
    })


def mint_scoped_s3_token(
    *,
    profile: str | None,
    buckets: list[str],
    duration_seconds: int = 3600,
) -> dict[str, str | None]:
    """Mint federated STS credentials scoped read-only to the named buckets.

    Returns a dict suitable for ComposeConfig.aws_credentials:
        {
            "access_key_id": ...,
            "secret_access_key": ...,
            "session_token": ...,
            "region": ...,
        }

    Raises:
        ValueError: if `buckets` is empty.
        StsUnavailable: if the underlying STS call fails for any reason
            (no credentials, permission denied, etc.). The error message
            points the caller at `--aws-unsafe-passthrough` as an escape
            hatch and at the required IAM permission.
    """
    if not buckets:
        raise ValueError("buckets cannot be empty")
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        sts = session.client("sts")
        resp = sts.get_federation_token(
            Name="claude-code-sandbox",
            Policy=_session_policy(buckets),
            DurationSeconds=duration_seconds,
        )
    except (
        botocore.exceptions.ClientError,
        botocore.exceptions.NoCredentialsError,
        botocore.exceptions.ProfileNotFound,
        botocore.exceptions.PartialCredentialsError,
    ) as e:
        raise StsUnavailable(
            f"Could not mint scoped S3 token via sts:GetFederationToken: {e}. "
            "Required IAM permission: sts:GetFederationToken on yourself. "
            "Or pass --aws-unsafe-passthrough to use raw profile credentials (not recommended)."
        ) from e

    creds = resp["Credentials"]
    region = session.region_name or "us-east-1"
    return {
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretAccessKey"],
        "session_token": creds["SessionToken"],
        "region": region,
    }
