"""Fake Instagram client/hosting for publisher tests (no network, non-dry semantics)."""
from instagram.errors import IGError


class FakeIG:
    def __init__(self, publish_errors=(), recent=None, status_seq=None, quota=0, copyright_match=False,
                 container_error=None):
        self.dry_run = False
        self.calls = []
        self.publish_errors = list(publish_errors)
        self.recent = recent or []
        self.status_seq = list(status_seq or ["IN_PROGRESS", "FINISHED"])
        self.quota = quota
        self.copyright = copyright_match
        self.container_error = container_error

    def publishing_limit(self):
        self.calls.append("limit")
        return {"quota_usage": self.quota, "config": {"quota_total": 50}}

    def create_reel_container(self, caption, video_url=None, resumable=False, thumb_offset_ms=None, **kw):
        self.calls.append(("container", video_url, thumb_offset_ms))
        if self.container_error:
            raise self.container_error
        return {"id": "C1", "uri": "https://rupload/C1"}

    def upload_resumable(self, uri, path):
        self.calls.append("rupload")
        return {"success": True}

    def container_status(self, cid):
        self.calls.append("status")
        code = self.status_seq.pop(0) if len(self.status_seq) > 1 else self.status_seq[0]
        return {"status_code": code, "copyright_check_status": {"status": "completed", "matches_found": self.copyright}}

    def publish(self, cid):
        self.calls.append("publish")
        if self.publish_errors:
            raise self.publish_errors.pop(0)
        return {"id": "M1"}

    def recent_media(self, limit=10):
        self.calls.append("recent")
        return self.recent

    def media(self, mid, fields=None):
        return {"id": mid, "permalink": f"https://instagram.com/reel/{mid}"}


class FakeHost:
    def __init__(self):
        self.uploaded, self.deleted = [], []

    def upload_temp(self, path, dry_run=None):
        self.uploaded.append(path)
        return {"key": "k1", "url": "https://pub.example/k1.mp4"}

    def delete_temp(self, key, dry_run=None):
        self.deleted.append(key)


def ig_error(kind, msg="x"):
    return IGError(kind, msg)
