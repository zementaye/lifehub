"""
S3-compatible object storage helper for the ID Vault (Backblaze B2).

Uploads go through a presigned PUT URL sent with a plain `requests.put()`
rather than boto3's own put_object/upload_fileobj, since botocore's request
framing (chunked/unsigned streaming payloads, checksum trailers) triggers
"IncompleteBody" errors against Backblaze B2's S3-compatible endpoint no
matter how those options are configured. boto3/botocore is only used here
to compute the presigned URL's signature — the actual byte transfer is a
plain HTTP PUT with no special encoding.
"""

import re

import boto3
import requests
from botocore.config import Config

import config


def _region_from_endpoint(endpoint_url):
    """B2 endpoints look like https://s3.<region>.backblazeb2.com — SigV4
    presigning needs the matching region or B2 rejects the signature."""
    if not endpoint_url:
        return "us-east-1"
    match = re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", endpoint_url)
    return match.group(1) if match else "us-east-1"


def _client():
    return boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT_URL,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name=_region_from_endpoint(config.R2_ENDPOINT_URL),
        # B2 requires SigV4 — boto3 sometimes falls back to the older SigV2
        # query-auth scheme for presigned URLs, which B2 rejects with a
        # plain 403. Force SigV4 explicitly.
        config=Config(signature_version="s3v4"),
    )


def upload_fileobj(file_obj, key, content_type=None):
    """Upload a file-like object (e.g. Flask's request.files['file']) under `key`."""
    import logging
    logger = logging.getLogger(__name__)
    logger.info(
        "B2 debug: key_id=%r (len %d) secret_len=%d bucket=%r endpoint=%r",
        config.R2_ACCESS_KEY_ID, len(config.R2_ACCESS_KEY_ID or ""),
        len(config.R2_SECRET_ACCESS_KEY or ""),
        config.R2_BUCKET_NAME, config.R2_ENDPOINT_URL,
    )

    data = file_obj.read()
    put_url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": config.R2_BUCKET_NAME, "Key": key},
        ExpiresIn=300,
    )
    headers = {"Content-Type": content_type} if content_type else {}
    resp = requests.put(put_url, data=data, headers=headers, timeout=60)
    if not resp.ok:
        # B2's error XML body has the real reason (bad key, wrong bucket,
        # expired credentials, etc.) — raise_for_status() alone hides it.
        raise RuntimeError(f"B2 upload failed ({resp.status_code}): {resp.text}")


def presigned_url(key, download_name=None, expires_in=300):
    """Return a temporary signed URL the browser can hit directly to view/download."""
    params = {"Bucket": config.R2_BUCKET_NAME, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)


def delete_file(key):
    _client().delete_object(Bucket=config.R2_BUCKET_NAME, Key=key)
