# qq2feishu

把 **macOS 上 QQ 收到的消息**转发到 **飞书群机器人**。纯标准库，无第三方依赖，常驻后台运行。

## 原理

不去破解 QQ 的本地数据库（NTQQ 的 `nt_msg.db` 是全库加密的），而是读 **macOS 通知中心**：

```
QQ 收到消息 → 系统弹通知 → usernoted 写入 SQLite（record.data 是 binary plist）
                                      ↓
        qq2feishu 每 3 秒轮询 → 解 plist 取 titl/subt/body → 去重 → 过滤
                                      ↓
                            飞书自定义机器人 webhook
```

好处是跟 QQ 版本解耦、对所有 IM 通用；代价是**只能拿到通知横幅上的文字**（见文末「局限」）。

## 快速开始

```bash
git clone <your-repo> qq2feishu && cd qq2feishu

# 0. 生成配置
cp config.example.json config.json

# 1. 填飞书 webhook
vi config.json          # feishu.webhook 必填

# 2. 授权完全磁盘访问权限（只需一次）
#    系统设置 → 隐私与安全性 → 完全磁盘访问权限 → +  加入你的终端 App，然后退出重开终端

# 3. 自检
python3 -m qq2feishu doctor

# 4. 发一条测试消息到飞书
python3 -m qq2feishu test

# 5. 前台试跑（能看到实时日志），确认有消息进来
python3 -m qq2feishu run

# 6. 装成后台常驻
python3 -m qq2feishu install
```

## 飞书侧准备

1. 飞书群 → 右上角 `···` → **设置** → **群机器人** → **添加机器人** → **自定义机器人**
2. 起个名字，复制 **Webhook 地址**（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxx`）
3. 如果启用了「签名校验」，把密钥填到 `feishu.secret`
4. 安全设置里建议加「自定义关键词」，比如填 `QQ`（本工具的消息以 `【QQ】` 开头）

## 命令

| 命令 | 作用 |
| --- | --- |
| `python3 -m qq2feishu doctor` | 自检：权限、通知库路径、QQ 通知是否可读、飞书连通性 |
| `python3 -m qq2feishu dump -n 20` | 直接打印通知库里的消息（不推送、不改状态），排查用 |
| `python3 -m qq2feishu dump --all-apps --json` | 看所有 App 的通知，用来确认 bundle id |
| `python3 -m qq2feishu run` | 前台常驻转发，Ctrl-C 退出 |
| `python3 -m qq2feishu run --once` | 只跑一轮，适合 cron / 调试 |
| `python3 -m qq2feishu test [--dry-run]` | 发测试消息到飞书 |
| `python3 -m qq2feishu status` | 游标、计数、进程、最近日志 |
| `python3 -m qq2feishu install` / `uninstall` | 安装 / 卸载 launchd 常驻服务 |

## 配置（config.json）

```jsonc
{
  "feishu": {
    "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
    "secret": "",              // 机器人开了签名校验才需要
    "msg_type": "text",        // text | card（card 更好看）
    "at_all": false            // true = 每条都 @所有人，慎用
  },
  "monitor": {
    "bundle_ids": ["com.tencent.qq"],  // QQ 的 identifier，doctor 会帮你确认
    "app_label": "QQ",
    "db_path": "",             // 留空自动探测
    "poll_interval": 3,        // 轮询秒数
    "backfill_minutes": 0      // 首次启动回溯多少分钟，0 = 不补发历史
  },
  "filter": {
    "include_titles": [],      // 只转发标题匹配正则的会话，如 ["^项目", "运维"]
    "exclude_titles": [],      // 排除某些群
    "exclude_keywords": [],    // 正文含这些词就丢弃
    "skip_empty_body": true,   // 丢弃"你收到一条新消息"这类无正文通知
    "skip_presented": false    // true = 只转发系统真正弹出过的
  },
  "runtime": {
    "state_path": "~/.qq2feishu/state.db",
    "log_path": "~/.qq2feishu/qq2feishu.log",
    "log_level": "INFO",
    "min_send_interval": 0.3,  // 两条消息最小间隔，防限流
    "max_send_attempts": 3,
    "page_size": 200
  }
}
```

也支持环境变量覆盖：`FEISHU_WEBHOOK`、`FEISHU_SECRET`、`QQ_NOTIFICATION_DB`。

只转发指定的几个群：

```json
"include_titles": ["产品群", "运维告警"]
```

## 常驻与授权

launchd 服务用 `install` 自动生成：`~/Library/LaunchAgents/com.qq2feishu.watcher.plist`，
日志在 `~/.qq2feishu/launchd.out.log`。

**重点**：TCC 权限是认「谁在跑」的，不是认脚本：

- 在终端里手跑 → 给 **终端 / iTerm** 开完全磁盘访问
- 后台 launchd 跑 → 给 `install` 打印出来的那个 python 解释器开权限
  （`which python3` 的结果，例如 `/opt/homebrew/bin/python3` 或 `/usr/bin/python3`）

授权后**不需要重启进程**，下一轮轮询就会成功（日志里会有 `60 秒后重试` 的提示）。

## 常见问题

**doctor 说「被 TCC 拦截」**
没给运行它的 App 完全磁盘访问权限。终端里跑就给终端授权，授权后要完全退出终端再打开。

**通知库路径变了（macOS 大版本升级）**
本项目同时探测两处：
- macOS 26+：`~/Library/Group Containers/group.com.apple.usernoted/db2/db`
- 更早版本：`$(getconf DARWIN_USER_DIR)com.apple.notificationcenter/db2/db`

都不对时用 `monitor.db_path` 手动指定，或改 `notification_db.py` 里的 `candidate_db_paths()`。

**一条都收不到**
1. QQ 必须是登录状态，且 QQ 的「系统通知」是开启的（QQ 设置 → 通知）
2. 群/好友不能设成「消息免打扰」，否则系统不弹通知、库里就没记录
3. 系统开了「专注模式」也会挡掉
4. 用 `python3 -m qq2feishu dump -n 5` 看看库里到底有没有

**重复推送**
状态库 `~/.qq2feishu/state.db` 里按内容 md5 去重，删掉它相当于允许补发历史（配合 `backfill_minutes=0` 仍只发新消息）。

**飞书限流**
自定义机器人约 100 条/分钟。调大 `min_send_interval`，或用 `include_titles` 收窄范围。

## 局限（重要）

1. 只拿得到**通知文字**：长消息会被系统截断，图片/文件/语音只剩 `[图片]` 之类的占位；
2. 只有「真的弹了通知」的消息才有：免打扰群、被折叠的通知、你手动清掉的通知都不会出现；
3. 需要 QQ 客户端在这台 Mac 上保持登录并运行；
4. 依赖 macOS 通知库结构，大版本升级可能需要小改（`doctor` 会立刻告诉你）。

需要「完整聊天记录」（含图片文件、历史回溯）只能走协议层或读 NTQQ 本地库，那是另一套完全不同的工程量。

## 测试

```bash
python3 tests/smoke.py
```

用合成通知库 + 本地 HTTP 桩跑完整链路（解析、过滤、去重、签名、卡片），不需要权限也不发真消息。
