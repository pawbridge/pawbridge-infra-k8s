"""외부 접속 없이 업로드·손상·보관 정책을 검증하는 S3 대역."""
import io
import datetime as dt
import email.utils
from pathlib import Path


class MemoryS3:
    def __init__(self, now=None):
        self.now = now
        self.objects = {}
        self.deleted = []
        self.corrupt_upload = False

    def upload_file(self, path, bucket, key, **kwargs):
        if key in self.objects:
            raise AssertionError("immutable key overwrite")
        data = Path(path).read_bytes()
        self.objects[key] = data + b"corrupted" if self.corrupt_upload else data

    def put_object(self, Bucket, Key, Body, **kwargs):
        if Key in self.objects:
            raise AssertionError("immutable marker overwrite")
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            from backup import BackupError
            raise BackupError("current completion marker missing")
        date = self.now or dt.datetime.now(dt.timezone.utc)
        return {"ResponseMetadata":{"HTTPHeaders":{"date":email.utils.format_datetime(date, usegmt=True)}}}

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        return {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)], "IsTruncated": False}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)
