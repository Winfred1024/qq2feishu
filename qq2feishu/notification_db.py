"""读取 macOS 通知中心（usernoted）数据库，解析出 QQ 通知。

设计要点：
- 不做任何 Hook / 注入，只读系统通知库，所以对所有 IM 通用，QQ 版本升级也不受影响；
- 数据库位置随 macOS 版本变化，这里同时探测新旧两个位置；
- 通知正文是 binary plist，用标准库 plistlib 解析（不需要 biplist）；
- record 表结构各版本略有差异，按实际列名动态拼 SQL。
"""

import hashlib
import os
import plistlib
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime

# 2001-01-01 与 1970-01-01 的秒差（Cocoa/Core Data 纪元）
COCOA_EPOCH_OFFSET = 978307200

# 大于该值就当成 Unix 时间戳，否则当成 Cocoa 时间戳
UNIX_THRESHOLD = 1_200_000_000

_TCC_HINT = """无权读取 macOS 通知中心数据库：
  {path}

这是 TCC（隐私）保护导致的，不是路径写错。解决方式：
  1. 打开「系统设置 → 隐私与安全性 → 完全磁盘访问权限」
  2. 点 + ，把你用来运行本工具的 App 加进去并打开开关
     · 在「终端 / iTerm」里手跑 → 授权 终端 / iTerm
     · 由 launchd 后台跑      → 授权 /usr/bin/python3 或你的 python 解释器
  3. 完全退出该 App（终端要退出重开）后重试：python3 -m qq2feishu doctor"""


class NotificationDBError(RuntimeError):
    pass


class PermissionDenied(NotificationDBError):
    pass


@dataclass(frozen=True)
class Notification:
    rec_id: int
    app_id: int
    app_identifier: str
    title: str
    subtitle: str
    body: str
    delivered_at: datetime
    presented: bool
    digest: str

    def as_dict(self):
        return {
            "rec_id": self.rec_id,
            "app_identifier": self.app_identifier,
            "title": self.title,
            "subtitle": self.subtitle,
            "body": self.body,
            "delivered_at": self.delivered_at.strftime("%Y-%m-%d %H:%M:%S"),
            "presented": self.presented,
            "digest": self.digest,
        }


# ---------------------------------------------------------------- 路径探测

def candidate_db_paths():
    """返回 [(path, 说明), ...]，按新→旧排列。"""
    home = os.path.expanduser("~")
    out = [
        (
            os.path.join(home, "Library", "Group Containers",
                         "group.com.apple.usernoted", "db2", "db"),
            "macOS 26+ (Group Containers)",
        )
    ]
    darwin_user_dir = _getconf_user_dir()
    if darwin_user_dir:
        out.append(
            (
                os.path.join(darwin_user_dir, "com.apple.notificationcenter", "db2", "db"),
                "macOS 10.15~15 (DARWIN_USER_DIR)",
            )
        )
    return out


def _getconf_user_dir():
    try:
        res = subprocess.run(
            ["getconf", "DARWIN_USER_DIR"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip()


def resolve_db_path(explicit=""):
    """定位通知库，返回 (path, 说明)。会区分「不存在」和「没权限」。"""
    if explicit:
        path = os.path.expanduser(explicit)
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise NotificationDBError(f"配置里的 db_path 目录不存在：{parent}")
        _assert_readable(parent)
        if not os.path.exists(path):
            raise NotificationDBError(f"配置里的 db_path 文件不存在：{path}")
        return path, "config"

    errors = []
    for path, label in candidate_db_paths():
        parent = os.path.dirname(path)
        try:
            _assert_readable(parent)
        except PermissionDenied as exc:
            errors.append(str(exc))
            continue
        except NotificationDBError:
            continue
        if os.path.exists(path):
            return path, label
    if errors:
        # 目录在、但被 TCC 挡住，是最常见的情况，优先报这个
        raise PermissionDenied(errors[0])
    raise NotificationDBError(
        "没找到 macOS 通知中心数据库，可能该系统版本路径不同。\n"
        "候选路径：\n" + "\n".join(f"  - {p}   ({label})" for p, label in candidate_db_paths())
    )


def _assert_readable(directory):
    if not os.path.isdir(directory):
        raise NotificationDBError(f"目录不存在：{directory}")
    try:
        os.listdir(directory)
    except PermissionError:
        raise PermissionDenied(_TCC_HINT.format(path=os.path.join(directory, "db")))


# ---------------------------------------------------------------- 读取

class NotificationStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = self._connect(db_path)

    @staticmethod
    def _connect(path):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM record LIMIT 1")
            return conn
        except sqlite3.OperationalError as exc:
            msg = str(exc)
            if "authoriz" in msg or "unable to open" in msg:
                raise PermissionDenied(_TCC_HINT.format(path=path))
            if "no such table" in msg:
                raise NotificationDBError(
                    f"数据库能打开但没有 record 表，schema 可能已变：{path}（{exc}）"
                )
            raise NotificationDBError(f"打开通知库失败：{exc}")
        except sqlite3.DatabaseError as exc:
            raise NotificationDBError(f"通知库不是合法的 SQLite 文件：{exc}")

    # ---- schema 适配
    def _columns(self, table):
        try:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.DatabaseError:
            return set()
        return {r["name"] for r in rows}

    def app_map(self):
        """app_id -> bundle identifier"""
        cols = self._columns("app")
        if "identifier" not in cols:
            return {}
        try:
            rows = self.conn.execute("SELECT app_id, identifier FROM app").fetchall()
        except sqlite3.DatabaseError:
            return {}
        return {int(r["app_id"]): str(r["identifier"] or "") for r in rows}

    def list_identifiers(self):
        return sorted({v for v in self.app_map().values() if v})

    def max_rec_id(self):
        row = self.conn.execute("SELECT COALESCE(MAX(rec_id), 0) AS m FROM record").fetchone()
        return int(row["m"] or 0)

    def fetch(self, bundle_ids, after_rec_id=0, limit=200, since=None):
        """取 rec_id > after_rec_id 的通知，按时间正序返回 (list, max_rec_id)。

        bundle_ids 为空时不做应用过滤（dump 场景）。
        """
        cols = self._columns("record")
        if "rec_id" not in cols or "app_id" not in cols or "data" not in cols:
            raise NotificationDBError(f"record 表字段不符合预期：{sorted(cols)}")

        delivered = "delivered_date" if "delivered_date" in cols else "0"
        presented = "presented" if "presented" in cols else "0"

        apps = self.app_map()
        wanted = {b.lower() for b in bundle_ids if b}
        app_ids = [aid for aid, ident in apps.items() if ident.lower() in wanted] if wanted else []

        if wanted and not app_ids:
            return [], after_rec_id

        where = [f"rec_id > ?"]
        params = [after_rec_id]
        if app_ids:
            where.append("app_id IN (%s)" % ",".join("?" * len(app_ids)))
            params.extend(app_ids)
        if since:
            where.append(f"{delivered} >= ?")
            params.append(since)

        sql = (
            f"SELECT rec_id, app_id, data, {delivered} AS delivered_date, "
            f"{presented} AS presented FROM record WHERE "
            + " AND ".join(where)
            + " ORDER BY rec_id ASC LIMIT ?"
        )
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()

        items = []
        for row in rows:
            note = self._parse_row(row, apps)
            if note is not None:
                items.append(note)

        new_cursor = after_rec_id
        if rows:
            new_cursor = max(int(r["rec_id"]) for r in rows)
        return items, new_cursor

    # ---- 单行解析
    def _parse_row(self, row, apps):
        rec_id = int(row["rec_id"])
        app_id = int(row["app_id"])
        identifier = apps.get(app_id, "")
        title, subtitle, body = _parse_payload(row["data"])
        delivered_at = _to_datetime(row["delivered_date"])
        digest_src = f"{app_id}|{title}|{subtitle}|{body}|{row['delivered_date']}"
        digest = hashlib.md5(digest_src.encode("utf-8", "replace")).hexdigest()
        return Notification(
            rec_id=rec_id,
            app_id=app_id,
            app_identifier=identifier,
            title=title,
            subtitle=subtitle,
            body=body,
            delivered_at=delivered_at,
            presented=bool(row["presented"]),
            digest=digest,
        )


# ---------------------------------------------------------------- plist 解析

def _parse_payload(raw):
    """从 record.data（binary plist）里取出 titl / subt / body。"""
    if raw is None:
        return "", "", ""
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    try:
        pl = plistlib.loads(bytes(raw))
    except Exception:
        return "", "", ""

    req = {}
    if isinstance(pl, dict):
        req = pl.get("req") or pl.get("request") or {}
    if not isinstance(req, dict) or not {"titl", "body"} & set(req):
        # 兜底：在整棵 plist 里找同时含 titl / body 的字典
        req = _deep_find(pl, {"titl", "subt", "body"}) or {}

    return (
        _as_text(req.get("titl")),
        _as_text(req.get("subt")),
        _as_text(req.get("body")),
    )


def _deep_find(obj, keys, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if keys & set(obj):
            return obj
        for value in obj.values():
            found = _deep_find(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for value in obj[:30]:
            found = _deep_find(value, keys, depth + 1)
            if found:
                return found
    return None


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _to_datetime(value):
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return datetime.fromtimestamp(0)
    if v > 1e11:          # 毫秒
        v = v / 1000.0
    ts = v + COCOA_EPOCH_OFFSET if v < UNIX_THRESHOLD else v
    try:
        return datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return datetime.fromtimestamp(0)
