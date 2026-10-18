"""
instagram/hosting.py — temporary public hosting of the reel for `video_url`
(IG_UPLOAD_METHOD=r2_url). Cloudflare R2 / any S3-compatible bucket via boto3.

The object gets a random, unguessable key, is used once by Instagram's
fetcher, and is deleted right after the container reaches a final state.
Credentials: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET, R2_PUBLIC_BASE_URL (the bucket's public r2.dev / custom domain).
"""
import os
import secrets

from config import settings
from log_setup import get_logger

log = get_logger("ig_hosting")


class HostingNotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return all([settings.R2_ENDPOINT_URL, settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY,
                settings.R2_BUCKET, settings.R2_PUBLIC_BASE_URL])


def _s3():
    import boto3
    return boto3.client("s3", endpoint_url=settings.R2_ENDPOINT_URL,
                        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY, region_name="auto")


def upload_temp(path: str, dry_run: bool = None, client=None) -> dict:
    dry = settings.DRY_RUN if dry_run is None else dry_run
    key = f"tmp/{secrets.token_urlsafe(18)}{os.path.splitext(path)[1] or '.mp4'}"
    url = f"{(settings.R2_PUBLIC_BASE_URL or 'https://dry-run.invalid').rstrip('/')}/{key}"
    if dry:
        log.info("[DRY_RUN] would upload %s → %s", os.path.basename(path), url)
        return {"key": key, "url": url}
    if not configured():
        raise HostingNotConfigured("R2_* settings missing in .env (needed for IG_UPLOAD_METHOD=r2_url)")
    (client or _s3()).upload_file(path, settings.R2_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    log.info("uploaded %s → %s", os.path.basename(path), key)
    return {"key": key, "url": url}


def delete_temp(key: str, dry_run: bool = None, client=None):
    dry = settings.DRY_RUN if dry_run is None else dry_run
    if dry or not key:
        log.info("[DRY_RUN] would delete %s", key)
        return
    try:
        (client or _s3()).delete_object(Bucket=settings.R2_BUCKET, Key=key)
    except Exception as e:
        log.warning("could not delete temp object %s: %s", key, e)
