"""邮件告警（F5.5）。

- `Alert`：告警条目（规则名/级别/消息/时间戳）
- `EmailAlerter`：SMTP 发送；未配置（缺 host/username/to_addrs）时降级为 no-op，
  告警仍由上层记日志，避免因缺邮箱配置导致监控进程崩溃。
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from quant.foundation.config import SmtpSettings
from quant.foundation.logger import get_logger

log = get_logger("monitor.alerter")


@dataclass(frozen=True, slots=True)
class Alert:
    """告警条目。

    `inst_id` 必须追加在末尾且带默认值：现有代码与单测大量使用位置参数
    构造，插到中间不会报错，只会让 `ts` 与 `inst_id` 悄悄错位。
    """

    rule: str
    level: str  # WARNING / ERROR
    message: str
    ts: int
    inst_id: str | None = None


class EmailAlerter:
    """SMTP 邮件告警。"""

    def __init__(self, settings: SmtpSettings) -> None:
        self._s = settings

    def enabled(self) -> bool:
        """是否已配置可用的 SMTP 与收件人。"""
        return bool(self._s.host and self._s.username and self._s.to_addrs)

    def send(self, subject: str, body: str) -> bool:
        """发送邮件；未配置或发送失败返回 False（不抛错）。"""
        if not self.enabled():
            return False

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._s.from_addr or self._s.username
        msg["To"] = ", ".join(self._s.to_addrs)
        msg.set_content(body)

        try:
            if self._s.use_ssl:
                with smtplib.SMTP_SSL(self._s.host, self._s.port) as smtp:
                    smtp.login(self._s.username, self._s.password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self._s.host, self._s.port) as smtp:
                    smtp.login(self._s.username, self._s.password)
                    smtp.send_message(msg)
            return True
        except Exception as e:  # noqa: BLE001
            log.error(f"email send failed: {e}")
            return False


__all__ = ["Alert", "EmailAlerter"]
