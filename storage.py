"""
S3-compatible object storage helper for the ID Vault (Backblaze B2).

Uploads go through a presigned PUT URL sent with a plain `requests.put()`
rather than boto3's own put_object/upload_fileobj. Multiple newer-botocore
request-framing behaviors (default checksum trailers, chunked/unsigned
streaming payloads) trigger "IncompleteBody" errors against Backblaze B2's
S3-compatible endpoint no matter how those options are configured. A
presigned URL still uses boto3/botocore (just to compute the signature),
but the actual byte transfer happens over a completely plain HTTP PUT with
no special encoding, which sidesteps the problem entirely.
"""

import boto3
import requests
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
    data = file_obj.read()
    put_url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": config.R2_BUCKET_NAME, "Key": key},
        ExpiresIn=300,
    )
    headers = {"Content-Type": content_type} if content_type else {}
    resp = requests.put(put_url, data=data, headers=headers, timeout=60)
    resp.raise_for_status()


def presigned_url(key, download_name=None, expires_in=300):
    """Return a temporary signed URL the browser can hit directly to view/download."""
    params = {"Bucket": config.R2_BUCKET_NAME, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)


def delete_file(key):
    _client().delete_object(Bucket=config.R2_BUCKET_NAME, Key=key)
