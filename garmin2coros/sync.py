"""Idempotent orchestration with explicit unresolved submissions."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .domain import SyncError, original_payload, same_sessions


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise SyncError("状态目录不能是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextmanager
def run_lock(directory):
    directory = private_dir(directory)
    fd = os.open(directory / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("已有同步进程在运行，请等待其结束") from None
        yield
    finally:
        os.close(fd)


class Ledger:
    def __init__(self, path, scope):
        self.path = Path(path)
        self.scope = scope
        if self.path.is_symlink():
            raise SyncError("状态文件不能是符号链接")
        self.data = {"version": 1, "accounts": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                if self.data["version"] != 1 or not isinstance(self.data["accounts"], dict):
                    raise ValueError
                for records in self.data["accounts"].values():
                    if not isinstance(records, dict):
                        raise ValueError
                    for record in records.values():
                        if not isinstance(record, dict) or record.get("status") not in ("pending", "confirmed"):
                            raise ValueError
                        if not isinstance(record.get("sha256"), str):
                            raise ValueError
            except (ValueError, TypeError, KeyError):
                raise SyncError("同步状态文件损坏，停止以防重复上传；请保留文件核查") from None

    def get(self, activity_id):
        return self.data["accounts"].get(self.scope, {}).get(self.key(activity_id))

    def key(self, activity_id):
        return hashlib.sha256(f"{self.scope}:{activity_id}".encode()).hexdigest()

    def set(self, activity_id, digest, status):
        self.data["accounts"].setdefault(self.scope, {})[self.key(activity_id)] = {
            "sha256": digest, "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()

    def save(self):
        private_dir(self.path.parent)
        # Preserve pending intent across interruption and power loss before POST.
        fd, name = tempfile.mkstemp(prefix="ledger-", suffix=".tmp", dir=self.path.parent)
        with os.fdopen(fd, "w") as output:
            json.dump(self.data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def account_scope(source_id, target_id):
    return hashlib.sha256(json.dumps([source_id, target_id], separators=(",", ":")).encode()).hexdigest()


class Runner:
    def __init__(self, source, target, ledger, start_day, end_day, tz, *, apply=False,
                 limit=None, retry_pending=False, poll_seconds=60, sleep=time.sleep,
                 clock=time.monotonic, emit=print):
        self.source, self.target, self.ledger = source, target, ledger
        self.start_day, self.end_day, self.tz = start_day, end_day, tz
        self.apply, self.limit, self.retry_pending = apply, limit, retry_pending
        self.poll_seconds, self.sleep, self.clock, self.emit = poll_seconds, sleep, clock, emit
        self.remote_files = {}

    def find_existing(self, payload, remote):
        starts = [session.start for session in payload.sessions]
        candidates = [row for row in remote if any(abs(float(row["startTime"]) - start) <= 2 for start in starts)]
        for row in candidates:
            label = str(row["labelId"])
            if label not in self.remote_files:
                self.remote_files[label] = original_payload(self.target.download(row))
            if same_sessions(payload.sessions, self.remote_files[label].sessions):
                return True
        if candidates:
            raise SyncError("高驰存在同开始时间但内容不一致的记录，请人工核对，未重复上传")
        return False

    def confirm(self, payload):
        deadline = self.clock() + self.poll_seconds
        while True:
            remote = self.target.activities(self.start_day, self.end_day)
            if self.find_existing(payload, remote):
                return True, remote
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False, remote
            self.sleep(min(5, remaining))

    def run(self):
        counts = {"existing": 0, "planned": 0, "confirmed": 0, "pending": 0, "failed": 0}
        activities = self.source.activities(self.start_day, self.end_day, self.tz)
        remote = self.target.activities(self.start_day, self.end_day)
        checked = 0
        for activity in activities:
            activity_id = str(activity["activityId"])
            try:
                if self.limit is not None and checked >= self.limit:
                    break
                checked += 1
                payload = original_payload(self.source.download(activity_id))
                digest = hashlib.sha256(payload.content).hexdigest()
                record = self.ledger.get(activity_id)
                if self.find_existing(payload, remote):
                    counts["existing"] += 1
                    if self.apply:
                        self.ledger.set(activity_id, digest, "confirmed")
                    self.emit(f"{activity_id}：高驰已有匹配记录")
                    continue
                if record:
                    if record["sha256"] != digest:
                        raise SyncError("同一佳明 ID 的原始文件已变化，需人工核对历史同步状态")
                    if record["status"] == "pending" and not self.retry_pending:
                        counts["pending"] += 1
                        self.emit(f"{activity_id}：上次提交结果待核实，未再次上传；核实后可用 --retry-pending")
                        continue
                if not self.apply:
                    counts["planned"] += 1
                    sports = ",".join(sorted({s.sport for s in payload.sessions}))
                    self.emit(f"{activity_id}：计划同步（{sports}），未上传")
                    continue
                self.target.upload(payload, lambda: self.ledger.set(activity_id, digest, "pending"), self.tz)
                confirmed, remote = self.confirm(payload)
                if confirmed:
                    self.ledger.set(activity_id, digest, "confirmed")
                    counts["confirmed"] += 1
                    self.emit(f"{activity_id}：导入后已核对高驰记录")
                else:
                    counts["pending"] += 1
                    self.emit(f"{activity_id}：已提交，高驰尚无匹配记录；可能仍在处理或不支持该活动，请核实")
            except SyncError as error:
                counts["failed"] += 1
                self.emit(f"{activity_id}：未完成：{error}")
            except Exception as error:
                counts["failed"] += 1
                self.emit(f"{activity_id}：未完成（{type(error).__name__}）；未输出可能含凭据的异常正文")
        self.emit("汇总：" + "，".join(f"{label} {counts[key]}" for key, label in [
            ("existing", "高驰已有"), ("planned", "计划同步"),
            ("confirmed", "新增并确认"), ("pending", "待核实"), ("failed", "失败"),
        ]))
        return counts
