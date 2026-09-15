"""Activity selection and lossless, bounded original-file validation."""

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import math
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET
import zipfile

import fitdecode


MAX_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 100


class SyncError(Exception):
    """An actionable error whose message contains no credentials or raw responses."""


class CyclingExcluded(SyncError):
    pass


def number(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def utc_seconds(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise SyncError("活动开始时间格式无效") from None
    else:
        raise SyncError("活动缺少开始时间")
    if dt.tzinfo is None:
        # Garmin startTimeGMT and FIT timestamps are UTC even when offset is absent.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def cycling_key(value):
    key = str(value or "").lower().replace("-", "_")
    return bool(re.search(r"(^|_)(cycling|biking|bike|bicycle|ebike|bmx|cyclocross|velomobile)(_|$)", key))


class ActivityTypes:
    """Use Garmin's parent graph, including future descendants of cycling."""

    def __init__(self, rows):
        if not isinstance(rows, list) or not rows:
            raise SyncError("佳明活动类型表无效，无法可靠排除骑行")
        self.by_id = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("typeId") is None:
                raise SyncError("佳明活动类型表字段变化")
            self.by_id[str(row["typeId"])] = row

    def is_cycling(self, row):
        current = row.get("activityType") or {}
        if not isinstance(current, dict):
            raise SyncError("活动类型字段无效")
        visited = set()
        while current:
            if cycling_key(current.get("typeKey")):
                return True
            type_id = str(current.get("typeId", ""))
            if type_id in visited:
                raise SyncError("佳明活动类型继承关系出现循环")
            visited.add(type_id)
            known = self.by_id.get(type_id, {})
            if cycling_key(known.get("typeKey")):
                return True
            parent = current.get("parentTypeId", known.get("parentTypeId"))
            if parent is None or str(parent) in ("0", "-1"):
                break
            current = self.by_id.get(str(parent))
            if current is None:
                raise SyncError("佳明活动类型父类缺失，需核实后同步")
        return False


@dataclass(frozen=True)
class Session:
    start: float
    sport: str
    duration: float | None
    distance: float | None


@dataclass(frozen=True)
class Payload:
    content: bytes
    filename: str
    sessions: tuple[Session, ...]


def normalize_sport(value):
    sport = str(value or "").lower()
    return {"biking": "cycling", "other": "generic"}.get(sport, sport)


def fit_sessions(raw):
    sessions = []
    try:
        with fitdecode.FitReader(BytesIO(raw), check_crc=fitdecode.CrcCheck.RAISE) as reader:
            for frame in reader:
                if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "session":
                    sport = normalize_sport(frame.get_value("sport", fallback=None))
                    if not sport:
                        raise SyncError("FIT 会话缺少运动类型")
                    sessions.append(Session(
                        utc_seconds(frame.get_value("start_time", fallback=None)),
                        sport,
                        number(frame.get_value("total_timer_time", fallback=None)),
                        number(frame.get_value("total_distance", fallback=None)),
                    ))
    except SyncError:
        raise
    except Exception:
        raise SyncError("FIT 校验或解析失败，未上传") from None
    if not sessions:
        raise SyncError("FIT 不含活动会话（可能是训练计划或健康数据）")
    return tuple(sessions)


def tcx_sessions(raw):
    # Restrict TCX to UTF-8 XML without DTD/entity declarations.
    try:
        text = raw.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise SyncError("TCX 含不支持的 XML 声明")
        root = ET.fromstring(text)
        sessions = []
        for activity in root.findall(".//{*}Activities/{*}Activity"):
            laps = activity.findall("{*}Lap")
            start = activity.findtext("{*}Id")
            durations = [number(lap.findtext("{*}TotalTimeSeconds")) for lap in laps]
            distances = [number(lap.findtext("{*}DistanceMeters")) for lap in laps]
            sport = normalize_sport(activity.get("Sport"))
            if not sport:
                raise SyncError("TCX 缺少运动类型")
            sessions.append(Session(
                utc_seconds(start), sport,
                sum(durations) if durations and None not in durations else None,
                sum(distances) if distances and None not in distances else None,
            ))
    except SyncError:
        raise
    except (UnicodeError, ET.ParseError, ValueError):
        raise SyncError("TCX 校验或解析失败") from None
    if not sessions:
        raise SyncError("TCX 不含已完成的活动")
    return tuple(sessions)


def original_payload(data, *, exclude_cycling=True):
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise SyncError("原始文件为空或超过 200 MiB")
    raw = data
    extension = None
    if zipfile.is_zipfile(BytesIO(data)):
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > MAX_MEMBERS or sum(x.file_size for x in members) > MAX_BYTES:
                    raise SyncError("压缩包展开后过大或文件过多")
                files = []
                for member in members:
                    path = PurePosixPath(member.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                        raise SyncError("压缩包含不安全路径")
                    if member.is_dir() or any(p.startswith((".", "__MACOSX")) for p in path.parts):
                        continue
                    if path.suffix.lower() in (".fit", ".tcx"):
                        files.append(member)
                if len(files) != 1:
                    raise SyncError("原始压缩包须恰好包含一个 FIT/TCX；未做部分导入")
                member = files[0]
                extension = PurePosixPath(member.filename).suffix.lower()
                raw = archive.read(member)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise SyncError("原始压缩包损坏或格式不支持") from None
    if extension is None:
        extension = ".fit" if raw[8:12] == b".FIT" else ".tcx"
    sessions = fit_sessions(raw) if extension == ".fit" else tcx_sessions(raw)
    if exclude_cycling and any(cycling_key(s.sport) for s in sessions):
        raise CyclingExcluded("原始文件含骑行会话（混合运动整条跳过，保留原文件）")
    buffer = BytesIO()
    # A fixed ZIP timestamp makes repeated uploads of identical originals identical.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo("activity" + extension, (1980, 1, 1, 0, 0, 0))
        archive.writestr(info, raw)
    content = buffer.getvalue()
    if len(content) > MAX_BYTES:
        raise SyncError("待上传压缩包超过 200 MiB")
    return Payload(content, "activity.zip", sessions)


def same_sessions(source, target):
    """Conservative matching for existing/imported records; never match by day alone."""
    if len(source) != len(target):
        return False
    for left, right in zip(sorted(source, key=lambda s: s.start), sorted(target, key=lambda s: s.start)):
        if abs(left.start - right.start) > 2 or left.sport != right.sport:
            return False
        # A missing duration cannot establish a duplicate.
        if left.duration is None or right.duration is None or abs(left.duration - right.duration) > 3:
            return False
        if left.distance is not None and right.distance is not None:
            if abs(left.distance - right.distance) > max(10, left.distance * 0.01):
                return False
    return True
