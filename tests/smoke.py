"""端到端冒烟测试：合成一个 macOS 通知库 + 本地飞书桩，验证整条链路。

不需要完全磁盘访问权限，也不会真的发消息到飞书：

    python3 tests/smoke.py
"""

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCOA = 978307200

received = []


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        received.append(json.loads(self.rfile.read(length)))
        body = b'{"code":0,"msg":"success"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def build_fake_db(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER);
        CREATE TABLE record (
            rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB,
            request_date REAL, request_last_date REAL, delivered_date REAL,
            presented Bool, style INTEGER, snooze_fire_date REAL
        );
        INSERT INTO app VALUES (35, 'com.tencent.xinwechat', 0);
        INSERT INTO app VALUES (78, 'com.tencent.qq', 2);
        """
    )
    now = datetime.now()
    for i, (rec_id, app_id, title, subtitle, body, minutes_ago) in enumerate(rows):
        payload = plistlib.dumps(
            {"req": {"titl": title, "subt": subtitle, "body": body}},
            fmt=plistlib.FMT_BINARY,
        )
        conn.execute(
            "INSERT INTO record VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                rec_id, app_id, b"uuid", payload, 0, 0,
                (now - timedelta(minutes=minutes_ago)).timestamp() - COCOA,
                1, 0, 0,
            ),
        )
    conn.commit()
    conn.close()


def write_config(tmp, extra_filter=None):
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    cfg["monitor"]["db_path"] = os.path.join(tmp, "usernoted", "db2", "db")
    cfg["runtime"]["state_path"] = os.path.join(tmp, "state.db")
    cfg["runtime"]["log_path"] = os.path.join(tmp, "run.log")
    cfg["runtime"]["min_send_interval"] = 0
    cfg["filter"].update(extra_filter or {})
    path = os.path.join(tmp, "config.json")
    json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return path


def run_once(cfg_path, webhook, secret=""):
    env = dict(os.environ, FEISHU_WEBHOOK=webhook, FEISHU_SECRET=secret)
    proc = subprocess.run(
        [sys.executable, "-m", "qq2feishu", "run", "--once", "--config", cfg_path],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"run 失败 rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def main():
    tmp = tempfile.mkdtemp(prefix="qq2feishu-smoke-")
    db = os.path.join(tmp, "usernoted", "db2", "db")
    build_fake_db(
        db,
        [
            (1, 78, "产品群", "张三", "今晚八点发版，注意回归", 30),
            (2, 35, "某个微信群", "李四", "这条不应该被转发", 25),
            (3, 78, "张三", "", "私聊消息", 20),
            (4, 78, "产品群", "王五", "收到", 15),
            (5, 78, "闲聊群", "赵六", "【广告】加微信领资料", 10),
        ],
    )

    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webhook = f"http://127.0.0.1:{server.server_port}/open-apis/bot/v2/hook/test"

    print("① dump 解析（不过滤应用）")
    cfg = write_config(tmp)
    out = subprocess.run(
        [sys.executable, "-m", "qq2feishu", "dump", "-n", "10", "--all-apps", "--config", cfg],
        cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, QQ_NOTIFICATION_DB=db),
    )
    assert out.returncode == 0, out.stderr
    lines = [l for l in out.stdout.splitlines() if l.strip()]
    assert len(lines) == 5, lines
    assert any("产品群" in l and "今晚八点发版" in l for l in lines), lines
    print("   ✓ 5 条通知解析成功（含微信群）")

    print("② 只转发 QQ + 排除广告，首次运行跳过历史")
    cfg = write_config(tmp, {"exclude_keywords": ["加微信领资料"]})
    run_once(cfg, webhook)
    assert len(received) == 0, f"首次运行不应补发历史，实际发了 {len(received)} 条"
    print("   ✓ 首次运行未补发历史")

    print("③ 新增消息 → 只推送 QQ 且过滤广告")
    build_fake_db(
        db,
        [
            (1, 78, "产品群", "张三", "今晚八点发版，注意回归", 30),
            (2, 35, "某个微信群", "李四", "这条不应该被转发", 25),
            (3, 78, "张三", "", "私聊消息", 20),
            (4, 78, "产品群", "王五", "收到", 15),
            (5, 78, "闲聊群", "赵六", "【广告】加微信领资料", 10),
            (6, 78, "产品群", "张三", "灰度已经开始了", 1),
            (7, 78, "产品群", "", "   ", 1),
        ],
    )
    run_once(cfg, webhook)
    texts = [m.get("content", {}).get("text", "") for m in received]
    assert len(received) == 1, texts
    assert "灰度已经开始了" in texts[0] and "产品群" in texts[0] and "张三" in texts[0], texts
    print(f"   ✓ 只推送 1 条：{texts[0]!r}")

    print("④ 再跑一次不重复推送")
    before = len(received)
    run_once(cfg, webhook)
    assert len(received) == before, "重复推送了"
    print("   ✓ 去重生效")

    print("⑤ 签名 + 卡片模式")
    cfg = write_config(tmp, {"exclude_keywords": []})
    with open(cfg, encoding="utf-8") as fp:
        c = json.load(fp)
    c["feishu"]["msg_type"] = "card"
    json.dump(c, open(cfg, "w", encoding="utf-8"), ensure_ascii=False)
    build_fake_db(db, [(1, 78, "产品群", "张三", "卡片消息", 30), (8, 78, "产品群", "李四", "第二条", 1)])
    run_once(cfg, webhook, secret="my-secret")
    assert received[-1]["msg_type"] == "interactive", received[-1]
    assert "sign" in received[-1] and "timestamp" in received[-1], received[-1]
    print("   ✓ 卡片消息带签名")

    server.shutdown()
    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
