"""Synthetic fixtures only: no account, network, credentials or personal FIT data."""

from datetime import date, datetime, timezone
from io import BytesIO
import hashlib
import base64
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
from zoneinfo import ZoneInfo

from fitdecode.utils import compute_crc

from garmin2coros.domain import (
    ActivityTypes, CyclingExcluded, Session, SyncError, original_payload, same_sessions,
)
from garmin2coros.clients import CorosTarget, GarminSource, request
from garmin2coros.sync import Ledger, Runner, account_scope, run_lock
from garmin2coros.cli import main


START = int(datetime(2026, 9, 14, 22, 30, tzinfo=timezone.utc).timestamp())
DAY = date(2026, 9, 15)
TZ = ZoneInfo("Asia/Shanghai")
TYPES = [
    {"typeId": 1, "typeKey": "running"},
    {"typeId": 2, "typeKey": "cycling"},
    {"typeId": 3, "typeKey": "future_ride", "parentTypeId": 2},
    {"typeId": 4, "typeKey": "walking"},
    {"typeId": 5, "typeKey": "swimming"},
    {"typeId": 6, "typeKey": "strength_training"},
    {"typeId": 7, "typeKey": "hiking"},
    {"typeId": 8, "typeKey": "other"},
]


def fit(sport=1, start=START, duration=1000, distance=3000):
    # FIT session global message 18: start_time, sport, total_timer_time, total_distance.
    definition = bytes([0x40, 0, 0]) + struct.pack("<H", 18) + bytes([
        4, 2, 4, 0x86, 5, 1, 0, 8, 4, 0x86, 9, 4, 0x86,
    ])
    data = bytes([0]) + struct.pack("<IBII", start - 631065600, sport, duration * 1000, distance * 100)
    body = definition + data
    header = struct.pack("<BBHI4s", 14, 0x20, 2100, len(body), b".FIT")
    header += struct.pack("<H", compute_crc(header))
    output = header + body
    return output + struct.pack("<H", compute_crc(output))


def zipped(raw, name="activity.fit"):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, raw)
    return buffer.getvalue()


def row(activity_id="101", type_id=1, start=START):
    return {
        "activityId": activity_id,
        "activityType": {"typeId": type_id},
        "startTimeGMT": datetime.fromtimestamp(start, timezone.utc).isoformat(),
    }


class Source:
    def __init__(self, rows=None, raw=None):
        self.rows = rows if rows is not None else [row()]
        self.raw = raw if raw is not None else fit()
        self.downloads = []

    def activities(self, *args):
        return self.rows, ActivityTypes(TYPES)

    def download(self, activity_id):
        self.downloads.append(activity_id)
        if isinstance(self.raw, dict):
            return self.raw[activity_id]
        return self.raw


class Target:
    def __init__(self, raw=None, fail=False, appear=True):
        self.raw = raw
        self.fail = fail
        self.appear = appear
        self.uploads = 0

    def activities(self, *args):
        return [{"labelId": "target-id", "startTime": START, "sportType": 100}] if self.raw else []

    def download(self, row):
        return self.raw

    def upload(self, payload, before_submit, tz):
        before_submit()
        self.uploads += 1
        if self.fail:
            raise SyncError("模拟提交超时")
        if self.appear:
            self.raw = payload.content
        return "import-id"


class DomainTests(unittest.TestCase):
    def test_parent_cycling_is_excluded(self):
        self.assertTrue(ActivityTypes(TYPES).is_cycling(row(type_id=3)))

    def test_all_named_cycling_subtypes_excluded(self):
        types = ActivityTypes(TYPES)
        for name in ["road_biking", "indoor_cycling", "mountain_biking", "e_bike", "e_bike_mountain", "gravel_cycling", "virtual_ride_cycling", "cyclocross", "bmx"]:
            with self.subTest(name=name):
                self.assertTrue(types.is_cycling({"activityType": {"typeKey": name}}))

    def test_non_cycling_types_allowed(self):
        types = ActivityTypes(TYPES)
        for type_id in [1, 4, 5, 6, 7, 8]:
            self.assertFalse(types.is_cycling(row(type_id=type_id)))

    def test_unknown_parent_is_not_silently_allowed(self):
        with self.assertRaises(SyncError):
            ActivityTypes(TYPES).is_cycling({"activityType": {"typeId": 900, "parentTypeId": 999}})

    def test_cycle_in_type_graph_fails(self):
        with self.assertRaises(SyncError):
            ActivityTypes([{"typeId": 9, "parentTypeId": 9}]).is_cycling(row(type_id=9))

    def test_original_fit_preserved(self):
        raw = fit()
        payload = original_payload(zipped(raw))
        with zipfile.ZipFile(BytesIO(payload.content)) as archive:
            self.assertEqual(archive.read("activity.fit"), raw)
        self.assertEqual(payload.sessions, (Session(START, "running", 1000.0, 3000.0),))
        self.assertEqual(payload.content, original_payload(raw).content)

    def test_fit_cycling_overrides_garmin_label(self):
        with self.assertRaises(CyclingExcluded):
            original_payload(fit(sport=2))

    def test_mixed_sport_file_with_cycling_excluded(self):
        with self.assertRaises(CyclingExcluded):
            original_payload(fit() + fit(sport=2, start=START + 1000))

    def test_bad_fit_crc_rejected(self):
        bad = bytearray(fit())
        bad[-1] ^= 0xFF
        with self.assertRaises(SyncError):
            original_payload(bytes(bad))

    def test_zip_path_traversal_rejected(self):
        with self.assertRaises(SyncError):
            original_payload(zipped(fit(), "../activity.fit"))

    def test_multiple_files_not_partially_imported(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("one.fit", fit())
            archive.writestr("two.fit", fit(sport=2))
        with self.assertRaises(SyncError):
            original_payload(buffer.getvalue())

    def test_zip_size_bounded(self):
        with patch("garmin2coros.domain.MAX_BYTES", 20):
            with self.assertRaises(SyncError):
                original_payload(zipped(fit()))

    def test_tcx_original_and_biking(self):
        tcx = b'<TrainingCenterDatabase><Activities><Activity Sport="Running"><Id>2026-09-14T22:30:00Z</Id><Lap><TotalTimeSeconds>1000</TotalTimeSeconds><DistanceMeters>3000</DistanceMeters></Lap></Activity></Activities></TrainingCenterDatabase>'
        self.assertEqual(original_payload(tcx).sessions[0].sport, "running")
        with self.assertRaises(CyclingExcluded):
            original_payload(tcx.replace(b"Running", b"Biking"))
        with self.assertRaises(SyncError):
            original_payload(b'<!DOCTYPE x [<!ENTITY test "bad">]>' + tcx)

    def test_duplicate_requires_duration_and_sport(self):
        source = (Session(START, "running", 1000, 3000),)
        self.assertTrue(same_sessions(source, (Session(START + 1, "running", 1001, 3005),)))
        self.assertFalse(same_sessions(source, (Session(START, "cycling", 1000, 3000),)))
        self.assertFalse(same_sessions(source, (Session(START, "running", None, 3000),)))
        self.assertFalse(same_sessions(source, (Session(START, "running", 1000, 4000),)))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.json"
        self.ledger = Ledger(self.path, "test-scope")

    def run_sync(self, source=None, target=None, **kwargs):
        return Runner(source or Source(), target or Target(), self.ledger, DAY, DAY, TZ,
                      emit=lambda _: None, poll_seconds=0, **kwargs).run()

    def test_preview_never_uploads_or_writes_ledger(self):
        target = Target()
        result = self.run_sync(target=target)
        self.assertEqual(result["planned"], 1)
        self.assertEqual(target.uploads, 0)
        self.assertFalse(self.path.exists())

    def test_sync_confirm_and_second_run_no_upload(self):
        target = Target()
        first = self.run_sync(target=target, apply=True)
        self.assertEqual(first["confirmed"], 1)
        self.assertEqual(self.ledger.get("101")["status"], "confirmed")
        second = self.run_sync(target=target, apply=True)
        self.assertEqual(second["existing"], 1)
        self.assertEqual(target.uploads, 1)

    def test_import_submission_alone_is_not_success(self):
        target = Target(appear=False)
        result = self.run_sync(target=target, apply=True)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["confirmed"], 0)
        self.assertEqual(self.ledger.get("101")["status"], "pending")

    def test_timed_out_submit_not_retried_next_run(self):
        target = Target(fail=True)
        result = self.run_sync(target=target, apply=True)
        self.assertEqual(result["failed"], 1)
        self.ledger = Ledger(self.path, "test-scope")
        result = self.run_sync(target=target, apply=True)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(target.uploads, 1)

    def test_pending_file_change_does_not_auto_retry(self):
        target = Target(appear=False)
        self.run_sync(target=target, apply=True)
        result = self.run_sync(source=Source(raw=fit(duration=1200)), target=target, apply=True, retry_pending=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(target.uploads, 1)

    def test_late_import_recovers_pending(self):
        target = Target(appear=False)
        self.run_sync(target=target, apply=True)
        target.raw = fit()
        result = self.run_sync(target=target, apply=True)
        self.assertEqual(result["existing"], 1)
        self.assertEqual(self.ledger.get("101")["status"], "confirmed")

    def test_manual_retry_of_pending(self):
        target = Target(appear=False)
        self.run_sync(target=target, apply=True)
        target.appear = True
        result = self.run_sync(target=target, apply=True, retry_pending=True)
        self.assertEqual(result["confirmed"], 1)
        self.assertEqual(target.uploads, 2)

    def test_previously_confirmed_missing_not_recreated(self):
        target = Target()
        self.run_sync(target=target, apply=True)
        target.raw = None
        result = self.run_sync(target=target, apply=True)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(target.uploads, 1)

    def test_same_time_conflict_not_uploaded(self):
        target = Target(raw=fit(duration=1200))
        result = self.run_sync(target=target, apply=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(target.uploads, 0)

    def test_failed_ledger_write_prevents_submission(self):
        target = Target()
        with patch.object(self.ledger, "set", side_effect=OSError("full")):
            result = self.run_sync(target=target, apply=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(target.uploads, 0)

    def test_bad_original_does_not_prevent_next_activity(self):
        source = Source([row("101"), row("102")], {"101": b"bad", "102": fit()})
        result = self.run_sync(source=source, apply=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["confirmed"], 1)

    def test_filter_ride_without_downloading(self):
        source = Source([row(type_id=3)])
        result = self.run_sync(source=source, apply=True)
        self.assertEqual(result["cycling"], 1)
        self.assertEqual(source.downloads, [])

    def test_riding_fit_with_non_riding_metadata_excluded(self):
        result = self.run_sync(source=Source(raw=fit(sport=2)), apply=True)
        self.assertEqual(result["cycling"], 1)

    def test_swim_walk_hike_strength_are_processed(self):
        for sport, type_id in [(5, 5), (11, 4), (17, 7), (4, 6)]:
            with self.subTest(sport=sport):
                result = self.run_sync(source=Source([row(type_id=type_id)], fit(sport=sport)))
                self.assertEqual(result["planned"], 1)

    def test_limit(self):
        source = Source([row("101"), row("102")])
        result = self.run_sync(source=source, limit=1)
        self.assertEqual(result["planned"], 1)

    def test_ledger_scopes_accounts_and_hides_ids(self):
        self.ledger.set("private-activity-id", "digest", "pending")
        self.assertNotIn("private-activity-id", self.path.read_text())
        self.assertIsNone(Ledger(self.path, "other").get("private-activity-id"))
        self.assertNotEqual(account_scope("a", "b"), account_scope("b", "a"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_bad_ledger_not_silently_reset(self):
        self.path.write_text("bad")
        with self.assertRaises(SyncError):
            Ledger(self.path, "test-scope")

    def test_local_concurrent_run_blocked(self):
        with run_lock(self.temp.name):
            with self.assertRaises(SyncError):
                with run_lock(self.temp.name):
                    pass


class ClientTests(unittest.TestCase):
    def test_coros_login_uses_bcrypt_and_resolves_account_region(self):
        import bcrypt
        with patch.dict("os.environ", {"COROS_USERNAME": "example", "COROS_PASSWORD": "synthetic-password"}, clear=True):
            target = CorosTarget()
            response = Mock(status_code=200)
            response.json.return_value = {"result": "0000", "data": {"accessToken": "synthetic-token", "userId": "1234", "regionId": 4}}
            with patch("garmin2coros.clients.request", return_value=response) as req:
                target.login()
            payload = req.call_args.kwargs["json"]
            self.assertTrue(bcrypt.checkpw(hashlib.md5(b"synthetic-password").hexdigest().encode(), payload["p1"].encode()))
            self.assertNotIn("synthetic-password", json.dumps(payload))
            self.assertEqual(target.region, "sg")
            self.assertEqual(target.base_url, "https://teamsgapi.coros.com")

    def upload_fixture(self, region="cn"):
        from garmin2coros.clients import REGIONS
        with patch.dict("os.environ", {"COROS_REGION": region}, clear=True):
            target = CorosTarget()
        target.user_id = "1234"
        target.api = Mock(return_value={"id": "import-test"})
        credentials = {
            "Region": "oss-cn-beijing" if region == "cn" else "us-west-2",
            "Bucket": REGIONS[region][2], "AccessKeyId": "fake-id",
            "AccessKeySecret": "fake-secret", "SecurityToken": "fake-session",
            "SecretAccessKey": "fake-secret", "SessionToken": "fake-session",
        }
        response = Mock(status_code=200)
        response.json.return_value = {"code": 200, "data": {
            "credentials": "9y78gpoERW4lBNYL" + base64.b64encode(json.dumps(credentials).encode()).decode(),
        }}
        return target, response

    def test_aliyun_upload_protocol_and_intent_before_import(self):
        target, response = self.upload_fixture()
        events = []
        target.api.side_effect = lambda *a, **k: events.append("import") or {"id": "test"}
        with patch("garmin2coros.clients.request", return_value=response), patch("oss2.Bucket") as bucket:
            bucket.return_value.put_object.side_effect = lambda *a: events.append("put") or Mock(status=200)
            payload = original_payload(fit())
            target.upload(payload, lambda: events.append("intent"), TZ)
        self.assertEqual(events, ["put", "intent", "import"])
        args, kwargs = target.api.call_args
        self.assertEqual(args, ("POST", "/activity/fit/import"))
        metadata = json.loads(kwargs["files"]["jsonParameter"][1])
        self.assertEqual(metadata["timezone"], 32)
        self.assertEqual(metadata["md5"], hashlib.md5(payload.content).hexdigest())
        self.assertEqual(metadata["size"], len(payload.content))
        self.assertEqual(metadata["bucket"], "coros-oss")

    def test_aws_upload_uses_sts_and_correct_bucket(self):
        target, response = self.upload_fixture("us")
        with patch("garmin2coros.clients.request", return_value=response), patch("boto3.client") as storage:
            target.upload(original_payload(fit()), lambda: None, TZ)
        self.assertEqual(storage.call_args.kwargs["aws_session_token"], "fake-session")
        self.assertEqual(storage.return_value.put_object.call_args.kwargs["Bucket"], "coros-s3")

    def test_object_storage_failure_never_submits_import(self):
        target, response = self.upload_fixture()
        intent = Mock()
        with patch("garmin2coros.clients.request", return_value=response), patch("oss2.Bucket") as bucket:
            bucket.return_value.put_object.side_effect = RuntimeError("synthetic-secret")
            with self.assertRaises(SyncError) as error:
                target.upload(original_payload(fit()), intent, TZ)
        self.assertNotIn("synthetic-secret", str(error.exception))
        intent.assert_not_called()
        target.api.assert_not_called()

    def test_millisecond_target_timestamp_fails_instead_of_upload(self):
        with patch.dict("os.environ", {}, clear=True):
            target = CorosTarget()
        target.api = Mock(return_value={"totalPage": 1, "dataList": [{"labelId": "a", "sportType": 100, "startTime": START * 1000}]})
        with self.assertRaises(SyncError):
            target.activities(DAY, DAY)

    def test_read_retries_but_import_does_not(self):
        session = Mock()
        session.request.return_value = Mock(status_code=503)
        with patch("garmin2coros.clients.time.sleep"):
            request(session, "GET", "https://example.invalid", "test")
        self.assertEqual(session.request.call_count, 3)
        session.reset_mock()
        request(session, "POST", "https://example.invalid", "test")
        self.assertEqual(session.request.call_count, 1)

    def test_coros_pagination_and_missing_fields(self):
        with patch.dict("os.environ", {}, clear=True):
            target = CorosTarget()
        target.api = Mock(side_effect=[
            {"totalPage": 2, "dataList": [{"labelId": "a", "sportType": 100, "startTime": START}]},
            {"totalPage": 2, "dataList": [{"labelId": "b", "sportType": 200, "startTime": START + 500}]},
        ])
        self.assertEqual(len(target.activities(DAY, DAY)), 2)
        target.api = Mock(return_value={"totalPage": 1, "dataList": [{"labelId": "a", "sportType": 100}]})
        with self.assertRaises(SyncError):
            target.activities(DAY, DAY)

    def test_coros_repeated_page_fails(self):
        with patch.dict("os.environ", {}, clear=True):
            target = CorosTarget()
        target.api = Mock(return_value={"totalPage": 2, "dataList": [{"labelId": "a", "sportType": 100, "startTime": START}]})
        with self.assertRaises(SyncError):
            target.activities(DAY, DAY)

    def test_coros_untrusted_download_host_rejected(self):
        with patch.dict("os.environ", {}, clear=True):
            target = CorosTarget()
        target.api = Mock(return_value={"fileUrl": "https://attacker.invalid/a.fit"})
        with self.assertRaises(SyncError):
            target.download({"labelId": "a", "sportType": 100})

    def test_garmin_international_date_filter_and_unique_ids(self):
        with patch("garmin2coros.clients.Garmin") as library:
            source = GarminSource("unused")
            self.assertFalse(library.call_args.kwargs["is_cn"])
            source.api.get_activity_types.return_value = TYPES
            source.api.get_activities_by_date.return_value = [row(), row(), row("102", start=START - 86400)]
            rows, _ = source.activities(DAY, DAY, TZ)
            self.assertEqual([r["activityId"] for r in rows], ["101"])

    def test_invalid_cli_dates_and_retry(self):
        for args in [["--days", "0"], ["--start", "2026-09-15", "--end", "2026-09-01"], ["--retry-pending"]]:
            with self.subTest(args=args):
                from io import StringIO
                with patch("sys.stderr", new=StringIO()), self.assertRaises(SystemExit) as exc:
                    main(args)
                self.assertEqual(exc.exception.code, 2)

    def test_cli_returns_failure_for_pending(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as directory, patch("garmin2coros.cli.GarminSource") as source, patch("garmin2coros.cli.CorosTarget") as target, patch("garmin2coros.cli.Runner") as runner, patch("sys.stdout", new=StringIO()):
            source.return_value.identity = "synthetic-source"
            target.return_value.identity = "synthetic-target"
            runner.return_value.run.return_value = {"failed": 0, "pending": 1}
            self.assertEqual(main(["--state-dir", directory]), 2)


if __name__ == "__main__":
    unittest.main()
