"""Read Garmin International originals and use COROS Training Hub's import protocol."""

import base64
from datetime import datetime, timedelta
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import bcrypt
import requests
from garminconnect import Garmin

from .domain import MAX_BYTES, SyncError, number, utc_seconds


REGIONS = {
    "cn": (2, "https://teamcnapi.coros.com", "coros-oss", "aliyun"),
    "us": (1, "https://teamapi.coros.com", "coros-s3", "aws"),
    "eu": (3, "https://teameuapi.coros.com", "eu-coros", "aws"),
    "sg": (4, "https://teamsgapi.coros.com", "coros-sg-prod", "aliyun"),
}


def response_json(response, action):
    if not 200 <= response.status_code < 300:
        raise SyncError(f"{action}：HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise SyncError(f"{action}：响应不是 JSON") from None
    if not isinstance(data, dict):
        raise SyncError(f"{action}：响应结构变化")
    return data


def request(session, method, url, action, **kwargs):
    """Retry only reads; a timed-out import POST may already have succeeded."""
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            response = session.request(method, url, timeout=(15, 60), allow_redirects=False, **kwargs)
            if response.status_code >= 500 and attempt + 1 < attempts:
                response.close()
                time.sleep(2 ** attempt)
                continue
            return response
        except requests.RequestException:
            if attempt + 1 == attempts:
                raise SyncError(f"{action}：网络失败；上传请求不会自动重发") from None
            time.sleep(2 ** attempt)
    raise SyncError(f"{action}失败")


class GarminSource:
    def __init__(self, auth_dir, *, interactive=False):
        # Third-party auth messages may include server content; expose our own errors.
        logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
        self.auth_dir = Path(auth_dir)
        self.interactive = interactive
        self.api = Garmin(
            email=os.getenv("GARMIN_USERNAME"), password=os.getenv("GARMIN_PASSWORD"),
            is_cn=False, prompt_mfa=self.mfa,
        )

    def mfa(self):
        if not self.interactive:
            raise SyncError("佳明需要验证码，请先运行 login，再更新 GARMIN_TOKENS")
        import getpass
        code = getpass.getpass("佳明验证码（输入不回显）：").strip()
        if not code:
            raise SyncError("未输入佳明验证码")
        return code

    def login(self):
        inline = os.getenv("GARMIN_TOKENS")
        if inline:
            try:
                parsed = json.loads(inline)
                if not isinstance(parsed, dict) or not parsed.get("di_refresh_token"):
                    raise ValueError
            except ValueError:
                raise SyncError("GARMIN_TOKENS 必须是当前版本生成的完整 JSON 会话") from None
        # Avoid the library's implicit GARMINTOKENS fallback (which can point outside this project).
        tokenstore = inline or str(self.auth_dir)
        try:
            self.api.login(tokenstore)
        except Exception:
            raise SyncError("佳明国际版登录失败：检查账号、会话有效期、验证码及网络限制；未输出原始响应") from None
        if not self.api.display_name:
            raise SyncError("佳明未返回账号标识")
        if self.interactive:
            self.api.client.dump(str(self.auth_dir))

    @property
    def identity(self):
        return str(self.api.display_name)

    def activities(self, start_day, end_day, tz):
        try:
            # Fetch a margin because Garmin filters by each activity's local date.
            rows = self.api.get_activities_by_date(
                (start_day - timedelta(days=2)).isoformat(),
                (end_day + timedelta(days=2)).isoformat(),
                sortorder="asc",
            )
        except SyncError:
            raise
        except Exception:
            raise SyncError("佳明活动列表读取失败") from None
        if not isinstance(rows, list):
            raise SyncError("佳明活动列表格式变化")
        unique = {}
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("activityId", "")).isdigit():
                raise SyncError("佳明活动缺少有效 ID")
            start = utc_seconds(row.get("startTimeGMT"))
            day = datetime.fromtimestamp(start, tz).date()
            if start_day <= day <= end_day:
                unique[str(row["activityId"])] = row
        return sorted(unique.values(), key=lambda r: utc_seconds(r["startTimeGMT"]))

    def download(self, activity_id):
        try:
            return self.api.download_activity(activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        except Exception:
            raise SyncError("佳明原始 FIT/TCX 下载失败；无原始文件的活动不能上传") from None


class CorosTarget:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0", "Origin": "https://t.coros.com",
            "Referer": "https://t.coros.com/",
        })
        self.region = os.getenv("COROS_REGION", "cn").strip().lower() or "cn"
        self.region = {"1": "us", "2": "cn", "3": "eu", "4": "sg"}.get(self.region, self.region)
        if self.region not in REGIONS:
            raise SyncError("COROS_REGION 必须为 cn/us/eu/sg")
        self.user_id = None
        self.base_url = REGIONS[self.region][1]

    def login(self):
        username, password = os.getenv("COROS_USERNAME"), os.getenv("COROS_PASSWORD")
        if not username or not password:
            raise SyncError("缺少 COROS_USERNAME 或 COROS_PASSWORD")
        account_type = os.getenv("COROS_ACCOUNT_TYPE", "2") or "2"
        if account_type not in ("1", "2"):
            raise SyncError("COROS_ACCOUNT_TYPE 必须为 1 或 2")
        signed = hashlib.md5(password.encode()).hexdigest().encode()
        salt = bcrypt.gensalt(rounds=10)
        result = response_json(request(self.session, "POST", self.base_url + "/account/login", "高驰登录", json={
            "account": username, "accountType": int(account_type),
            "p1": bcrypt.hashpw(signed, salt).decode(), "p2": salt.decode(),
        }), "高驰登录")
        data = result.get("data")
        if str(result.get("result")) != "0000" or not isinstance(data, dict) or not data.get("accessToken"):
            raise SyncError("高驰登录未成功：核对账号、地区和验证码要求")
        self.user_id = str(data.get("userId") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.user_id):
            raise SyncError("高驰未返回有效用户 ID")
        region_id = data.get("regionId", REGIONS[self.region][0])
        resolved = [key for key, value in REGIONS.items() if str(value[0]) == str(region_id)]
        if not resolved:
            raise SyncError("高驰返回未知区域")
        self.region = resolved[0]
        self.base_url = REGIONS[self.region][1]
        self.session.headers["accessToken"] = str(data["accessToken"])

    @property
    def identity(self):
        return f"{self.region}:{self.user_id}"

    def api(self, method, path, **kwargs):
        result = response_json(request(self.session, method, self.base_url + path, "高驰接口", **kwargs), "高驰接口")
        if str(result.get("result")) != "0000":
            code = str(result.get("result", "unknown"))
            code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code) else "unknown"
            raise SyncError(f"高驰拒绝请求（代码 {code}）；请在 Training Hub 核实导入结果")
        return result.get("data")

    def activities(self, start_day, end_day):
        result, seen = [], set()
        for page in range(1, 1001):
            data = self.api("GET", "/activity/query", params={
                "size": 100, "pageNumber": page, "modeList": "",
                "startDay": (start_day - timedelta(days=2)).strftime("%Y%m%d"),
                "endDay": (end_day + timedelta(days=2)).strftime("%Y%m%d"),
            })
            if not isinstance(data, dict) or not isinstance(data.get("dataList"), list):
                raise SyncError("高驰列表结构变化，停止以防重复导入")
            try:
                total = int(data["totalPage"])
            except (ValueError, TypeError, KeyError):
                raise SyncError("高驰分页信息缺失") from None
            rows = data["dataList"]
            if not rows and page < total:
                raise SyncError("高驰分页提前返回空列表")
            for row in rows:
                if not isinstance(row, dict):
                    raise SyncError("高驰活动结构变化")
                label = str(row.get("labelId") or "")
                start = number(row.get("startTime"))
                if not label or row.get("sportType") is None or start is None or not 0 < start < 100_000_000_000:
                    raise SyncError("高驰活动缺少 ID、类型或开始时间，无法安全去重")
                if label in seen:
                    raise SyncError("高驰分页重复，停止以防漏读")
                seen.add(label)
                result.append(row)
            if page >= total:
                return result
        raise SyncError("高驰活动分页超过上限")

    def download(self, row):
        data = self.api("POST", "/activity/detail/download", params={
            "labelId": row["labelId"], "sportType": row["sportType"], "fileType": 4,
        })
        url = data.get("fileUrl") if isinstance(data, dict) else None
        if not isinstance(url, str):
            raise SyncError("高驰已有记录无法下载核对")
        # Keep the account accessToken away from CDN and object storage downloads.
        for _ in range(4):
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if parsed.scheme != "https" or parsed.username or not any(
                host.endswith(suffix) for suffix in (".coros.com", ".aliyuncs.com", ".amazonaws.com", ".amazonaws.com.cn")
            ):
                raise SyncError("高驰下载地址不属于预期 HTTPS 文件服务")
            try:
                with requests.get(url, timeout=(15, 60), stream=True, allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = response.headers.get("Location", "")
                        continue
                    if response.status_code != 200:
                        raise SyncError(f"高驰文件下载失败：HTTP {response.status_code}")
                    output = BytesIO()
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
                        if output.tell() > MAX_BYTES:
                            raise SyncError("高驰文件超过 200 MiB")
                    return output.getvalue()
            except requests.RequestException:
                raise SyncError("高驰文件下载网络失败") from None
        raise SyncError("高驰下载重定向次数过多")

    def upload(self, payload, before_submit, tz):
        """Upload object, persist intent, then submit one import request exactly once."""
        region_id, _, bucket, service = REGIONS[self.region]
        token = self.session.headers.get("accessToken")
        if not token:
            raise SyncError("获取高驰临时上传凭据前需要登录高驰")
        # Training Hub's same-origin STS request authenticates with cookies,
        # unlike the team API's accessToken header. Keep them out of storage/CDN requests.
        with requests.Session() as upload_session:
            # Preserve server-issued login cookies (including HttpOnly cookies)
            # and login headers, as a browser does for the same-origin proxy.
            upload_session.headers.update(self.session.headers)
            upload_session.cookies.update(self.session.cookies)
            for name, value in [("CPL-coros-token", token), ("CPL-coros-region", str(region_id))]:
                upload_session.cookies.set(name, value, domain="t.coros.com", path="/api/proxy/oss", secure=True)
            result = response_json(request(upload_session, "GET", "https://t.coros.com/api/proxy/oss/sts", "获取高驰临时上传凭据", params={
                "bucket": bucket, "service": service, "v": 2,
            }), "获取高驰临时上传凭据")
        try:
            if result["code"] != 200:
                raise ValueError
            encoded = result["data"]["credentials"].replace("9y78gpoERW4lBNYL", "", 1)
            credentials = json.loads(base64.b64decode(encoded, validate=True))
            if credentials["Bucket"] != bucket:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise SyncError("高驰临时上传凭据无效或存储区域不匹配") from None
        digest = hashlib.md5(payload.content).hexdigest()
        key = f"fit_zip/{self.user_id}/{digest}.zip"
        try:
            if service == "aliyun":
                import oss2
                region = credentials["Region"]
                if not re.fullmatch(r"oss-[a-z0-9-]+", region):
                    raise ValueError
                auth = oss2.StsAuth(credentials["AccessKeyId"], credentials["AccessKeySecret"], credentials["SecurityToken"])
                storage = oss2.Bucket(auth, f"https://{region}.aliyuncs.com", bucket, connect_timeout=60)
                response = storage.put_object(key, payload.content)
                if response.status != 200:
                    raise ValueError
            else:
                import boto3
                from botocore.config import Config
                storage = boto3.client(
                    "s3", region_name=credentials["Region"],
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"],
                    config=Config(connect_timeout=15, read_timeout=60, retries={"max_attempts": 1}),
                )
                storage.put_object(Bucket=bucket, Key=key, Body=payload.content)
        except Exception:
            raise SyncError("高驰文件存储上传失败；尚未提交活动导入") from None
        offset = datetime.fromtimestamp(payload.sessions[0].start, tz).utcoffset()
        metadata = {
            "source": 1, "timezone": int(offset.total_seconds() / 900),
            "bucket": bucket, "md5": digest, "size": len(payload.content),
            "object": key, "serviceName": service, "oriFileName": payload.filename,
        }
        before_submit()
        data = self.api("POST", "/activity/fit/import", files={
            "jsonParameter": (None, json.dumps(metadata)),
        })
        if not isinstance(data, dict) or not data.get("id"):
            raise SyncError("高驰未返回导入任务 ID；请求可能已生效，保留待核实状态")
        return str(data["id"])
