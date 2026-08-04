"""
S3-compatible object storage helper for the ID Vault (Backblaze B2).
"""

import boto3
import config


def _client():
    return boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT_URL,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
    )


def upload_fileobj(file_obj, key, content_type=None):
    """Upload a file-like object (e.g. Flask's request.files['file']) under `key`."""
    extra = {"ContentType": content_type} if content_type else {}
    _client().upload_fileobj(file_obj, config.R2_BUCKET_NAME, key, ExtraArgs=extra)


def presigned_url(key, download_name=None, expires_in=300):
    """Return a temporary signed URL the browser can hit directly to view/download."""
    params = {"Bucket": config.R2_BUCKET_NAME, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)


def delete_file(key):
    _client().delete_object(Bucket=config.R2_BUCKET_NAME, Key=key)
