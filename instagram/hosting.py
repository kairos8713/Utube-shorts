"""
instagram/hosting.py — temporary hosting of the reel for `video_url`
(IG_UPLOAD_METHOD=r2_url). Cloudflare R2 / any S3-compatible bucket via boto3.

R2_URL_MODE:
  presigned (default) — the bucket stays private; Instagram gets a signed GET URL
                        valid R2_PRESIGN_EXPIRES_S (1 h). R2_PUBLIC_BASE_URL not needed.
                        NOT yet verified that Instagram's fetcher accepts it → check on
                        the first real post (fallback: R2_URL_MODE=public).
  public              — R2_PUBLIC_BASE_URL/<random key> (bucket needs public access).

The object gets a random, unguessable key, is used once by Instagram's
fetcher, and is deleted right after the container reaches a final state.
Credentials: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
"""
import os
import secrets

from config import settings
from log_setup import get_logger

log = get_logger("ig_hosting")


class HostingNotConfigured(RuntimeError):
    pass


def url_mode() -> str:
    mode = (settings.R2_URL_MODE or "presigned").strip().lower()
    if mode not in ("presigned", "public"):
        raise HostingNotConfigured(f"R2_URL_MODE must be 'presigned' or 'public', got {mode!r}")
    return mode


def configured() -> bool:
    base = all([settings.R2_ENDPOINT_URL, settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY,
                settings.R2_BUCKET])
    if url_mode() == "public":
        return base and bool(settings.R2_PUBLIC_BASE_URL)
    return base


def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=settings.R2_ENDPOINT_URL,
                        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY, region_name="auto",
                        config=Config(signature_version="s3v4"))


def upload_temp(path: str, dry_run: bool = None, client=None) -> dict:
    dry = settings.DRY_RUN if dry_run is None else dry_run
    key = f"tmp/{secrets.token_urlsafe(18)}{os.path.splitext(path)[1] or '.mp4'}"
    mode = url_mode()
    if dry:
        url = f"https://dry-run.invalid/{key}" + ("?X-Amz-Signature=dry" if mode == "presigned" else "")
        log.info("[DRY_RUN] would upload %s → %s (%s)", os.path.basename(path), url, mode)
        return {"key": key, "url": url}
    if not configured():
        need = "R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET" + \
               (", R2_PUBLIC_BASE_URL" if mode == "public" else "")
        raise HostingNotConfigured(f"{need} missing in .env (needed for IG_UPLOAD_METHOD=r2_url)")
    s3 = client or _s3()
    s3.upload_file(path, settings.R2_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    if mode == "presigned":
        url = s3.generate_presigned_url("get_object", Params={"Bucket": settings.R2_BUCKET, "Key": key},
                                        ExpiresIn=settings.R2_PRESIGN_EXPIRES_S)
    else:
        url = f"{settings.R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    log.info("uploaded %s → %s (%s URL)", os.path.basename(path), key, mode)
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
