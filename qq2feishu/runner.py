"""主循环：轮询通知库 → 过滤 → 推送飞书。"""

import logging
import os
import re
import sys
import time

from .notification_db import (
    NotificationDBError,
    NotificationStore,
    PermissionDenied,
    resolve_db_path,
)
from .feishu import FeishuBot, FeishuError, FeishuPermanentError
from .state import StateStore

LOG = logging.getLogger("qq2feishu")


class SendFailed(RuntimeError):
    """临时性推送失败：本轮中止、游标不前进，稍后重试。"""


def setup_logging(path, level="INFO"):
    handlers = [logging.StreamHandler(sys.stdout)]
    if path:
        os.makedirs(os.path.dirname(os.path.expanduser(path)), exist_ok=True)
        handlers.append(logging.FileHandler(os.path.expanduser(path), encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------- 过滤

class Filter:
    def __init__(self, cfg):
        self.include = [re.compile(p) for p in cfg.filter.get("include_titles") or []]
        self.exclude = [re.compile(p) for p in cfg.filter.get("exclude_titles") or []]
        self.keywords = [k for k in (cfg.filter.get("exclude_keywords") or []) if k]
        self.skip_empty = bool(cfg.filter.get("skip_empty_body", True))
        self.skip_presented = bool(cfg.filter.get("skip_presented", False))

    def ok(self, note):
        title = note.title or ""
        if self.skip_presented and not note.presented:
            return False, "未弹出（presented=0）"
        if self.skip_empty and not (note.body or "").strip():
            return False, "空正文"
        if self.include and not any(p.search(title) for p in self.include):
            return False, "标题不在 include_titles 内"
        if any(p.search(title) for p in self.exclude):
            return False, "标题命中 exclude_titles"
        for kw in self.keywords:
            if kw in (note.body or ""):
                return False, f"命中排除关键字：{kw}"
        return True, ""


# ---------------------------------------------------------------- 文案

def format_text(note, label):
    lines = [f"【{label}】{note.title}" if note.title else f"【{label}】"]
    body = (note.body or "").strip()
    if note.subtitle:
        lines.append(f"{note.subtitle}：{body}" if body else note.subtitle)
    elif body:
        lines.append(body)
    lines.append(note.delivered_at.strftime("%m-%d %H:%M:%S"))
    return "\n".join(lines)


def format_card(note, label):
    title = f"{label} · {note.title}" if note.title else f"{label} 消息"
    parts = []
    if note.subtitle:
        parts.append(f"**{note.subtitle}**")
    if note.body:
        parts.append(note.body.strip())
    parts.append(
        f"<font color='grey'>{note.delivered_at.strftime('%Y-%m-%d %H:%M:%S')}</font>"
    )
    return title, "\n".join(parts)


# ---------------------------------------------------------------- 主循环

class Forwarder:
    def __init__(self, cfg):
        cfg.validate(need_webhook=True)
        self.cfg = cfg
        self.label = cfg.monitor.get("app_label", "QQ")
        self.filter = Filter(cfg)
        self.state = StateStore(cfg.state_path)
        self.bot = FeishuBot(
            cfg.webhook,
            cfg.secret,
            max_attempts=int(cfg.runtime.get("max_send_attempts", 3)),
        )
        self.db_path, self.db_label = resolve_db_path(cfg.db_path)
        self.store = NotificationStore(self.db_path)
        LOG.info("通知库: %s (%s)", self.db_path, self.db_label)
        self._running = True

    def stop(self, *_):
        self._running = False

    # ---------------- 生命周期

    def run(self, once=False):
        self._init_cursor()
        interval = float(self.cfg.monitor["poll_interval"])
        LOG.info(
            "开始监控 bundle_ids=%s，轮询间隔 %ss", self.cfg.monitor["bundle_ids"], interval
        )
        while self._running:
            try:
                self._poll()
            except SendFailed as exc:
                LOG.error("%s", exc)
                LOG.error("30 秒后重试（游标已停在失败消息之前，不会丢消息）")
                time.sleep(30)
                continue
            except PermissionDenied as exc:
                LOG.error("%s", exc)
                LOG.error("60 秒后重试（授权后无需重启本进程）")
                time.sleep(60)
                continue
            except NotificationDBError as exc:
                LOG.error("读取通知库失败：%s", exc)
                time.sleep(max(interval, 10))
                continue
            except Exception:  # noqa: BLE001 - 常驻进程不能因为意外异常退出
                LOG.exception("轮询异常")
                time.sleep(max(interval, 10))
                continue

            if once:
                return
            for _ in range(int(interval * 10)):
                if not self._running:
                    break
                time.sleep(0.1)

    def _init_cursor(self):
        if self.state.get_cursor() is not None:
            return
        backfill = int(self.cfg.monitor.get("backfill_minutes") or 0)
        if backfill > 0:
            self.state.set_cursor(0)
            self.state.set_meta("since_unix", str(int(time.time() - backfill * 60)))
            LOG.info("首次运行，回溯最近 %s 分钟", backfill)
        else:
            cursor = self.store.max_rec_id()
            self.state.set_cursor(cursor)
            LOG.info("首次运行，从当前位置开始（不补发历史），cursor=%s", cursor)

    # ---------------- 单轮

    def _poll(self):
        cursor = self.state.get_cursor() or 0
        page = int(self.cfg.runtime.get("page_size", 200))
        since_unix = self._since_unix()
        items, new_cursor = self.store.fetch(
            bundle_ids=self.cfg.monitor["bundle_ids"],
            after_rec_id=cursor,
            limit=page,
            since=since_unix,
        )
        if not items:
            self.state.set_cursor(new_cursor)
            return

        sent = skipped = 0
        for note in items:
            if self.state.is_final(note.digest):
                continue
            ok, reason = self.filter.ok(note)
            if not ok:
                self.state.mark(note.digest, note.rec_id, "skipped")
                skipped += 1
                LOG.debug("跳过 rec=%s %r：%s", note.rec_id, note.title, reason)
                continue

            if self._deliver(note) == "retry":
                raise SendFailed(
                    f"推送失败，暂停本轮：rec={note.rec_id} {note.title!r} "
                    f"{note.delivered_at:%H:%M:%S}"
                )
            sent += 1
            time.sleep(float(self.cfg.runtime.get("min_send_interval", 0.3)))

        self.state.set_cursor(new_cursor)
        if sent or skipped:
            LOG.info("本轮 sent=%s skipped=%s cursor=%s", sent, skipped, new_cursor)

    def _since_unix(self):
        raw = self.state.get_meta("since_unix")
        if not raw:
            return None
        return float(raw) - 978307200  # Unix 秒 → Cocoa 秒

    def _deliver(self, note):
        try:
            if self.cfg.msg_type == "card":
                title, markdown = format_card(note, self.label)
                self.bot.send_card(title, markdown)
            else:
                self.bot.send_text(
                    format_text(note, self.label),
                    at_all=bool(self.cfg.feishu.get("at_all")),
                )
        except FeishuPermanentError as exc:
            # 内容/权限类错误，重试无意义，标记失败后继续
            self.state.mark(note.digest, note.rec_id, "failed")
            LOG.error("推送被飞书拒绝（不会重试）：%s | %r", exc, note.title)
            return "failed"
        except FeishuError as exc:
            self.state.mark(note.digest, note.rec_id, "pending")
            LOG.warning("推送失败，稍后重试：%s | %r", exc, note.title)
            return "retry"

        self.state.mark(note.digest, note.rec_id, "sent")
        LOG.info(
            "已推送 %s | %s | %s",
            note.delivered_at.strftime("%m-%d %H:%M:%S"),
            note.title or "-",
            (note.body or "")[:60].replace("\n", " "),
        )
        return "sent"
