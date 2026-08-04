"""
S3-compatible object storage helper for the ID Vault (Backblaze B2).
"""

import boto3
from botocore.config import Config
import config


def _client():
    return boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT_URL,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        # Newer boto3/botocore versions send data-integrity checksum headers
        # by default that Backblaze B2 doesn't support, which causes the
        # connection to be dropped mid-request. Dial checksums back to
        # "only when the API requires them" so uploads/downloads work.
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # Without this, botocore streams PutObject as an unsigned
            # "chunked" payload by default, which Backblaze B2 doesn't
            # reconstruct correctly (raises IncompleteBody). Forcing signed,
            # non-chunked payloads fixes it.
            s3={"payload_signing_enabled": True},
        ),
    )


def upload_fileobj(file_obj, key, content_type=None):
    """Upload a file-like object (e.g. Flask's request.files['file']) under `key`.

    Uses a plain put_object with the bytes read into memory rather than
    boto3's managed upload_fileobj/TransferManager — that path uses a
    chunked-encoding upload style that Backblaze B2 handles unreliably for
    small files (raises IncompleteBody). Vault files are small (photos/PDFs),
    so reading fully into memory first is simple and safe.
    """
    data = file_obj.read()
    extra = {"ContentType": content_type} if content_type else {}
    _client().put_object(Bucket=config.R2_BUCKET_NAME, Key=key, Body=data, **extra)


def presigned_url(key, download_name=None, expires_in=300):
    """Return a temporary signed URL the browser can hit directly to view/download."""
    params = {"Bucket": config.R2_BUCKET_NAME, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)


def delete_file(key):
    _client().delete_object(Bucket=config.R2_BUCKET_NAME, Key=key)
