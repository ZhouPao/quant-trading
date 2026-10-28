"""test-email 命令：验证 SMTP 配置能否真正发出邮件。

用法：
  quant test-email
  quant test-email --to someone@example.com

用途：告警与日报都依赖 SMTP，配置完先用这条命令确认能到达邮箱，
避免出事时才发现告警发不出去。
"""
from __future__ import annotations

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.monitor.alerter import EmailAlerter


def main(args) -> int:
    """发送一封测试邮件。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.test_email")

    smtp = settings.smtp
    if args.to:
        smtp = smtp.model_copy(update={"to_addrs": args.to})

    alerter = EmailAlerter(smtp)
    if not alerter.enabled():
        log.error(
            "SMTP 未配置：需要在 config.toml 的 [smtp] 填 host/username，"
            "并至少一个收件人（password 建议用环境变量）"
        )
        return 1

    log.info(
        f"sending test email via {smtp.host}:{smtp.port} "
        f"ssl={smtp.use_ssl} to={', '.join(smtp.to_addrs)}"
    )
    ok = alerter.send(
        "quant-trading SMTP 测试",
        "收到这封邮件说明告警链路已通。",
    )
    if not ok:
        log.error("send failed, check host/port/ssl/credentials in logs above")
        return 1
    log.info("test email sent")
    return 0
