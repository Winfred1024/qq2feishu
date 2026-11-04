"""配置加载：config.json + 环境变量覆盖。"""

import json
import os

DEFAULTS = {
    "feishu": {
        "webhook": "",
        "secret": "",
        "msg_type": "text",          # text | card
        "at_all": False,
    },
    "monitor": {
        # macOS 通知库里 QQ 的 bundle identifier。QQ NT 通常注册为 com.tencent.qq
        "bundle_ids": ["com.tencent.qq"],
        "app_label": "QQ",
        # 留空则自动探测（macOS 26+ 与旧版路径都支持）
        "db_path": "",
        "poll_interval": 5,          # 秒
        "backfill_minutes": 0,       # 首次启动回溯多少分钟，0 = 只转发启动之后的新消息
        "reply_only_new": True,
    },
    "filter": {
        "include_titles": [],        # 只转发标题匹配任一正则的会话，空 = 全部
        "exclude_titles": [],        # 标题命中任一正则则丢弃
        "exclude_keywords": [],      # 正文命中任一关键字则丢弃
        "skip_empty_body": True,     # 没有正文的通知（如"你收到一条新消息"）丢弃
        "skip_presented": False,     # True 时只转发系统实际弹出过的通知
    },
    "runtime": {
        "state_path": "~/.qq2feishu/state.db",
        "log_path": "~/.qq2feishu/qq2feishu.log",
        "log_level": "INFO",
        "min_send_interval": 0.3,    # 两条飞书消息之间的最小间隔（秒），防限流
        "max_send_attempts": 3,
        "page_size": 200,
    },
}

ENV_OVERRIDES = {
    ("feishu", "webhook"): "FEISHU_WEBHOOK",
    ("feishu", "secret"): "FEISHU_SECRET",
    ("monitor", "db_path"): "QQ_NOTIFICATION_DB",
}


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, data):
        self.data = data
        self.feishu = data["feishu"]
        self.monitor = data["monitor"]
        self.filter = data["filter"]
        self.runtime = data["runtime"]

    # ---- 便捷访问 ----
    @property
    def webhook(self):
        return self.feishu["webhook"]

    @property
    def secret(self):
        return self.feishu["secret"]

    @property
    def msg_type(self):
        return self.feishu["msg_type"]

    @property
    def state_path(self):
        return os.path.expanduser(self.runtime["state_path"])

    @property
    def log_path(self):
        return os.path.expanduser(self.runtime["log_path"])

    @property
    def db_path(self):
        p = self.monitor.get("db_path") or ""
        return os.path.expanduser(p) if p else ""

    def validate(self, need_webhook=True):
        if need_webhook:
            wh = self.webhook
            if not wh:
                raise ConfigError(
                    "未配置飞书 webhook。请在 config.json 的 feishu.webhook 填入群机器人地址，"
                    "或设置环境变量 FEISHU_WEBHOOK。"
                )
            if not (wh.startswith("https://") or wh.startswith("http://")):
                raise ConfigError(f"飞书 webhook 不是合法 URL：{wh}")
        if self.msg_type not in ("text", "card"):
            raise ConfigError(f"feishu.msg_type 只能是 text 或 card，当前：{self.msg_type}")
        try:
            float(self.monitor["poll_interval"])
        except (TypeError, ValueError):
            raise ConfigError("monitor.poll_interval 必须是数字")


def _merge(base, override):
    out = {}
    for k, v in base.items():
        if isinstance(v, dict):
            out[k] = _merge(v, override.get(k, {}) or {})
        else:
            out[k] = override.get(k, v)
    for k, v in override.items():
        if k not in out:
            out[k] = v
    return out


def load_config(path=None):
    """读取 config.json；不存在则用默认值。环境变量优先级最高。"""
    path = os.path.expanduser(path or os.path.join(os.getcwd(), "config.json"))
    user = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fp:
            try:
                user = json.load(fp)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"config.json 解析失败：{exc}")

    data = _merge(DEFAULTS, user)

    for (section, key), env in ENV_OVERRIDES.items():
        if os.environ.get(env):
            data[section][key] = os.environ[env]

    # 兼容字符串形式的数字
    data["monitor"]["poll_interval"] = float(data["monitor"]["poll_interval"])
    data["runtime"]["min_send_interval"] = float(data["runtime"]["min_send_interval"])

    return Config(data)
