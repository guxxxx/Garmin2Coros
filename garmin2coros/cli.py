"""Command line interface; only --apply submits records to COROS."""

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clients import CorosTarget, GarminSource
from .domain import SyncError
from .sync import Ledger, Runner, account_scope, private_dir, run_lock


def positive(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if result <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="佳明国际版 → 高驰：仅排除骑行，默认预览")
    parser.add_argument("command", nargs="?", choices=("sync", "login"), default="sync")
    parser.add_argument("--days", type=positive, default=14, help="最近 N 天（含今天），默认 14")
    parser.add_argument("--start", type=date.fromisoformat, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, help="结束日期 YYYY-MM-DD（含当天）")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="日期边界时区，默认 Asia/Shanghai")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="实际上传到高驰")
    mode.add_argument("--dry-run", action="store_true", help="预览、读取和核对，不上传（默认）")
    parser.add_argument("--limit", type=positive, help="最多检查 N 条非骑行候选，按时间从早到晚")
    parser.add_argument("--retry-pending", action="store_true", help="人工确认上次未导入后，允许重试待核实记录")
    parser.add_argument("--poll-seconds", type=positive, default=60, help="每次提交后最长等待确认秒数，默认 60")
    parser.add_argument("--state-dir", type=Path, default=Path(".state"))
    parser.add_argument("--auth-dir", type=Path, default=Path(".auth"))
    args = parser.parse_args(argv)
    if args.retry_pending and not args.apply:
        parser.error("--retry-pending 必须与 --apply 一起使用")
    if args.command == "login" and (args.apply or args.retry_pending):
        parser.error("login 不接受上传参数")
    try:
        tz = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        parser.error("未知时区")
    end_day = args.end or datetime.now(tz).date()
    try:
        start_day = args.start or end_day - timedelta(days=args.days - 1)
    except OverflowError:
        parser.error("日期范围过大")
    if start_day > end_day:
        parser.error("开始日期不能晚于结束日期")
    try:
        with run_lock(args.state_dir):
            if args.command == "login":
                if not sys.stdin.isatty():
                    raise SyncError("login 需要交互终端，以便输入验证码")
                private_dir(args.auth_dir)
                source = GarminSource(args.auth_dir, interactive=True)
                source.login()
                print(f"佳明会话已保存到 {args.auth_dir / 'garmin_tokens.json'}（敏感文件，勿提交）")
                return 0
            source = GarminSource(args.auth_dir)
            source.login()
            target = CorosTarget()
            target.login()
            scope = account_scope(source.identity, target.identity)
            ledger = Ledger(args.state_dir / "ledger.json", scope)
            if args.apply and not ledger.path.exists():
                ledger.save()
            print(f"{'执行同步' if args.apply else '预览'}：{start_day} 至 {end_day}，{args.timezone}，排除所有骑行")
            counts = Runner(source, target, ledger, start_day, end_day, tz, apply=args.apply,
                            limit=args.limit, retry_pending=args.retry_pending,
                            poll_seconds=args.poll_seconds).run()
            return 2 if counts["failed"] or counts["pending"] else 0
    except (SyncError, OSError) as error:
        message = str(error) if isinstance(error, SyncError) else f"本地文件操作失败（{type(error).__name__}）"
        print(f"停止：{message}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"停止：{type(error).__name__}；未输出可能含凭据的异常正文", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
