"""飞书群自定义机器人（webhook）推送。

- 支持签名校验（签名密钥）
- 支持 text / interactive card 两种消息
- 网络错误与 5xx 自动重试，429 按 Retry-After 退避
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request


class FeishuError(RuntimeError):
    pass


class FeishuPermanentError(FeishuError):
    """重试无意义的错误：内容不合法、机器人被移出群、token 失效等。"""


class FeishuBot:
    def __init__(self, webhook, secret="", timeout=10, max_attempts=3):
        self.webhook = webhook
        self.secret = secret or ""
        self.timeout = timeout
        self.max_attempts = max_attempts

    # ---------------- 对外接口

    def send_text(self, text, at_all=False):
        content = {"text": text}
        if at_all:
            content["text"] = text + "\n<at user_id=\"all\">所有人</at>"
        return self._post({"msg_type": "text", "content": content})

    def send_card(self, title, markdown, template="blue"):
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": template,
                    "title": {"tag": "plain_text", "content": title},
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": markdown}}
                ],
            },
        }
        return self._post(payload)

    # ---------------- 内部实现

    def build_payload(self, payload):
        """补上时间戳与签名（飞书自定义机器人签名校验）。"""
        body = dict(payload)
        if self.secret:
            ts = str(int(time.time()))
            string_to_sign = f"{ts}\n{self.secret}"
            digest = hmac.new(
                string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
            ).digest()
            body["timestamp"] = ts
            body["sign"] = base64.b64encode(digest).decode("utf-8")
        return body

    def _post(self, payload):
        body = json.dumps(self.build_payload(payload), ensure_ascii=False).encode("utf-8")
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(
                self.webhook,
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                return self._check(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code == 429:
                    wait = float(exc.headers.get("Retry-After") or 3)
                    time.sleep(min(wait, 10))
                    last_error = FeishuError(f"HTTP 429 限流：{detail}")
                    continue
                if 500 <= exc.code < 600:
                    last_error = FeishuError(f"HTTP {exc.code}：{detail}")
                    time.sleep(2 ** attempt)
                    continue
                raise FeishuPermanentError(f"HTTP {exc.code}：{detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = FeishuError(f"网络错误：{exc}")
                time.sleep(2 ** attempt)
                continue
        raise last_error or FeishuError("推送失败（未知原因）")

    @staticmethod
    def _check(raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise FeishuError(f"飞书返回非 JSON：{raw[:200]}")
        # v2 机器人返回 code，老版返回 StatusCode
        code = data.get("code", data.get("StatusCode", 0))
        if code not in (0, None):
            raise FeishuError(
                f"飞书返回错误 code={code} msg={data.get('msg') or data.get('StatusMessage')}"
            )
        return data
