"""命令行入口：doctor / dump / run / test / status / install / uninstall。"""

import argparse
import json
import os
import plistlib
import subprocess
import sys

from . import __version__
from .config import ConfigError, load_config
from .feishu import FeishuBot, FeishuError
from .notification_db import (
    NotificationDBError,
    NotificationStore,
    PermissionDenied,
    candidate_db_paths,
    resolve_db_path,
)
from .runner import Forwarder, format_card, format_text, setup_logging
from .state import StateStore

LABEL = "com.qq2feishu.watcher"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")


# ---------------------------------------------------------------- 工具

def _load(args):
    cfg = load_config(args.config)
    setup_logging(cfg.log_path, cfg.runtime.get("log_level", "INFO"))
    return cfg


def _mask(url):
    if not url:
        return "(未配置)"
    if len(url) <= 24:
        return url[:8] + "…"
    return url[:38] + "…"


def _width(text):
    from unicodedata import east_asian_width

    return sum(2 if east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _print_kv(key, value):
    pad = max(1, 30 - _width(key))
    print(f"  {key}{' ' * pad}{value}")


# ---------------------------------------------------------------- doctor

def cmd_doctor(args):
    ok = True
    print(f"qq2feishu {__version__}   Python {sys.version.split()[0]}   {sys.platform}")

    print("\n[配置]")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"  ✗ {exc}")
        return 1
    print(f"  配置文件                 {os.path.abspath(args.config or 'config.json')}"
          f"{'' if os.path.exists(args.config or 'config.json') else '  (不存在，使用默认值)'}")
    _print_kv("飞书 webhook", _mask(cfg.webhook))
    _print_kv("消息类型", cfg.msg_type)
    _print_kv("监控 bundle_ids", cfg.monitor["bundle_ids"])
    _print_kv("轮询间隔", f"{cfg.monitor['poll_interval']}s")

    print("\n[通知库路径]")
    for path, label in candidate_db_paths():
        state = "存在" if os.path.exists(path) else "不存在"
        readable = "可读"
        try:
            os.listdir(os.path.dirname(path))
        except PermissionError:
            readable = "被 TCC 拦截"
        except OSError as exc:
            readable = f"不可访问({exc.strerror})"
        print(f"  - {label}")
        print(f"      {path}")
        print(f"      {state} / {readable}")

    print("\n[数据库读取]")
    db_ok = True
    try:
        db_path, label = resolve_db_path(cfg.db_path)
        store = NotificationStore(db_path)
        print(f"  ✓ 已定位 {db_path}  ({label})")
    except PermissionDenied as exc:
        print("  ✗ 权限不足")
        print("    " + str(exc).replace("\n", "\n    "))
        db_ok = ok = False
    except NotificationDBError as exc:
        print(f"  ✗ {exc}")
        db_ok = ok = False

    if db_ok:
        apps = store.app_map()
        print(f"  应用总数                 {len(apps)}")
        wanted = [b.lower() for b in cfg.monitor["bundle_ids"]]
        hits = [(aid, ident) for aid, ident in apps.items() if ident.lower() in wanted]
        tencent = [(aid, i) for aid, i in apps.items() if "tencent" in i.lower() or "qq" in i.lower()]
        if hits:
            for aid, ident in hits:
                print(f"  ✓ 命中 {ident} (app_id={aid})")
        else:
            ok = False
            print(f"  ✗ 通知库里没有 {cfg.monitor['bundle_ids']} 的记录")
            print("    → 确认 QQ 已登录、通知权限已开启，并先让别人给你发一条消息")
            if tencent:
                print("    库里与腾讯/QQ 相关的 identifier：")
                for aid, ident in tencent:
                    print(f"      app_id={aid}  {ident}")
                print("    如果是新 identifier，请把它填进 config.json 的 monitor.bundle_ids")

        if hits:
            placeholders = ",".join("?" for _ in hits)
            count = store.conn.execute(
                f"SELECT COUNT(*) AS n FROM record WHERE app_id IN ({placeholders})",
                [aid for aid, _ in hits],
            ).fetchone()["n"]
        else:
            count = 0
        print(f"  QQ 通知条数(全库)         {count}")
        try:
            recent, _ = store.fetch(
                bundle_ids=cfg.monitor["bundle_ids"], after_rec_id=0, limit=3
            )
            if recent:
                print("  最近 3 条：")
                for note in reversed(recent[-3:]):
                    print(
                        f"      {note.delivered_at:%m-%d %H:%M:%S}  "
                        f"[{note.title}] {(note.body or '')[:40]}"
                    )
        except NotificationDBError as exc:
            print(f"  ! 读取记录失败：{exc}")

    print("\n[本地状态]")
    try:
        st = StateStore(cfg.state_path)
        print(f"  ✓ {cfg.state_path}")
        _print_kv("游标 cursor", st.get_cursor())
        _print_kv("累计计数", st.stats() or "{}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  ✗ {cfg.state_path}: {exc}")

    print("\n[飞书连通性]")
    if not cfg.webhook:
        print("  ✗ 未配置 webhook，无法推送")
        ok = False
    else:
        try:
            FeishuBot(cfg.webhook, cfg.secret, max_attempts=1).send_text(
                f"qq2feishu doctor 自检消息（{__version__}）"
            )
            print("  ✓ 已发送一条自检消息到群里，请查看")
        except FeishuError as exc:
            ok = False
            print(f"  ✗ {exc}")

    print("\n" + ("✓ 全部检查通过" if ok else "✗ 存在问题，见上方提示"))
    return 0 if ok else 1


# ---------------------------------------------------------------- dump

def cmd_dump(args):
    cfg = _load(args)
    db_path, label = resolve_db_path(cfg.db_path)
    store = NotificationStore(db_path)
    bundle_ids = [] if args.all_apps else cfg.monitor["bundle_ids"]
    items, _ = store.fetch(bundle_ids=bundle_ids, after_rec_id=0, limit=args.limit)
    items = list(reversed(items))[: args.limit]
    print(f"# {db_path}  ({label})  共 {len(items)} 条", file=sys.stderr)
    for note in items:
        if args.json:
            print(json.dumps(note.as_dict(), ensure_ascii=False))
        else:
            print(
                f"{note.delivered_at:%Y-%m-%d %H:%M:%S}  "
                f"[{note.title}] {note.subtitle} :: {(note.body or '')[:80]}"
            )
    return 0


# ---------------------------------------------------------------- run

def cmd_run(args):
    cfg = _load(args)
    forwarder = Forwarder(cfg)
    try:
        forwarder.run(once=args.once)
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


# ---------------------------------------------------------------- test

def cmd_test(args):
    cfg = _load(args)
    label = cfg.monitor.get("app_label", "QQ")
    note = _fake_note()
    if cfg.msg_type == "card":
        title, md = format_card(note, label)
        payload_desc, payload = "card", {"title": title, "markdown": md}
    else:
        text = format_text(note, label)
        payload_desc, payload = "text", {"text": text}
    print(f"[{payload_desc}] 将要发送的内容：\n{payload.get('text') or payload}\n")

    if args.dry_run:
        print("(dry-run，未真正发送)")
        return 0
    cfg.validate(need_webhook=True)
    bot = FeishuBot(cfg.webhook, cfg.secret)
    if cfg.msg_type == "card":
        bot.send_card(*format_card(note, label))
    else:
        bot.send_text(format_text(note, label))
    print("✓ 已发送，请到飞书群里确认")
    return 0


def _fake_note():
    from datetime import datetime

    from .notification_db import Notification

    return Notification(
        rec_id=0,
        app_id=0,
        app_identifier="com.tencent.qq",
        title="qq2feishu 测试群",
        subtitle="张三",
        body="这是一条测试消息，看到它说明转发链路已经打通。",
        delivered_at=datetime.now(),
        presented=True,
        digest="test",
    )


# ---------------------------------------------------------------- status

def cmd_status(args):
    cfg = load_config(args.config)
    st = StateStore(cfg.state_path)
    print(f"配置文件   {os.path.abspath(args.config or 'config.json')}")
    print(f"状态库     {cfg.state_path}")
    print(f"游标       {st.get_cursor()}")
    print(f"计数       {st.stats() or '{}'}")
    pids = subprocess.run(
        ["pgrep", "-f", "qq2feishu run"], capture_output=True, text=True
    ).stdout.split()
    print(f"进程       {', '.join(pids) if pids else '未在运行'}")
    print(f"launchd    {'已加载' if os.path.exists(PLIST_PATH) else '未安装'}")
    if os.path.exists(cfg.log_path):
        print(f"\n最近日志 {cfg.log_path}:")
        with open(cfg.log_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp.readlines()[-15:]:
                print("  " + line.rstrip())
    return 0


# ---------------------------------------------------------------- launchd

def _build_plist(cfg, config_path):
    log_dir = os.path.dirname(cfg.log_path) or os.path.join(HOME, ".qq2feishu")
    os.makedirs(log_dir, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "qq2feishu",
            "run",
            "--config",
            os.path.abspath(config_path),
        ],
        "WorkingDirectory": PROJECT_ROOT,
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": os.path.join(log_dir, "launchd.out.log"),
        "StandardErrorPath": os.path.join(log_dir, "launchd.err.log"),
    }


def cmd_install(args):
    cfg = load_config(args.config)
    cfg.validate(need_webhook=True)
    config_path = os.path.abspath(args.config or "config.json")
    plist = _build_plist(cfg, config_path)
    if args.dry_run:
        print(plistlib.dumps(plist).decode("utf-8"))
        return 0
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "wb") as fp:
        plistlib.dump(plist, fp)
    _launchctl("bootout", target=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST_PATH],
                   capture_output=True, text=True)
    print(f"✓ 已安装并启动：{PLIST_PATH}")
    print(f"  解释器：{sys.executable}")
    print("\n如果状态里一直没有消息，请把上面这个解释器加进")
    print("「系统设置 → 隐私与安全性 → 完全磁盘访问权限」。")
    print("查看状态：python3 -m qq2feishu status")
    return 0


def cmd_uninstall(args):
    _launchctl("bootout", target=True)
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
    print(f"✓ 已卸载 {PLIST_PATH}")
    return 0


def _launchctl(action, target=False):
    cmd = ["launchctl", action, f"gui/{os.getuid()}/{LABEL}"] if target else \
          ["launchctl", action, PLIST_PATH]
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------- 入口

def build_parser():
    p = argparse.ArgumentParser(
        prog="qq2feishu",
        description="把 macOS 通知中心里的 QQ 消息转发到飞书群机器人",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "-c", "--config",
        default=os.path.join(PROJECT_ROOT, "config.json"),
        help="配置文件路径（默认项目根目录 config.json）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # 让 --config 既能放在子命令前，也能放在子命令后
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", dest="config_sub", default=None,
                        help=argparse.SUPPRESS)

    sub.add_parser("doctor", parents=[common],
                   help="环境自检：权限、QQ 通知、飞书连通性").set_defaults(func=cmd_doctor)

    d = sub.add_parser("dump", parents=[common], help="直接打印通知库里的消息（不推送）")
    d.add_argument("-n", "--limit", type=int, default=20)
    d.add_argument("--all-apps", action="store_true", help="不过滤应用")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_dump)

    r = sub.add_parser("run", parents=[common], help="开始转发（前台常驻）")
    r.add_argument("--once", action="store_true", help="只轮询一轮就退出")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("test", parents=[common], help="发一条测试消息到飞书")
    t.add_argument("--dry-run", action="store_true", help="只打印消息内容，不发送")
    t.set_defaults(func=cmd_test)

    sub.add_parser("status", parents=[common],
                   help="查看运行状态与最近日志").set_defaults(func=cmd_status)
    ins = sub.add_parser("install", parents=[common],
                         help="安装为 launchd 常驻服务")
    ins.add_argument("--dry-run", action="store_true", help="只打印 plist，不安装")
    ins.set_defaults(func=cmd_install)
    sub.add_parser("uninstall", parents=[common],
                   help="卸载 launchd 服务").set_defaults(func=cmd_uninstall)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.config = args.config_sub or args.config
    try:
        return args.func(args)
    except (ConfigError, NotificationDBError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
