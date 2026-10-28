"""邮件告警器测试（F5.5）。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from quant.foundation.config import SmtpSettings
from quant.monitor.alerter import EmailAlerter


def _smtp(**overrides) -> SmtpSettings:
    defaults = {
        "host": "smtp.example.com",
        "port": 465,
        "username": "user@example.com",
        "password": "secret",
        "from_addr": "user@example.com",
        "to_addrs": ["owner@example.com"],
        "use_ssl": True,
    }
    defaults.update(overrides)
    return SmtpSettings(**defaults)


class TestEmailAlerterEnabled:
    def test_enabled_when_configured(self):
        assert EmailAlerter(_smtp()).enabled() is True

    def test_disabled_when_missing_host(self):
        assert EmailAlerter(_smtp(host="")).enabled() is False

    def test_disabled_when_missing_username(self):
        assert EmailAlerter(_smtp(username="")).enabled() is False

    def test_disabled_when_missing_recipients(self):
        assert EmailAlerter(_smtp(to_addrs=[])).enabled() is False


class TestEmailAlerterSend:
    def test_send_skips_when_disabled(self):
        alerter = EmailAlerter(_smtp(host=""))
        assert alerter.send("subject", "body") is False

    @patch("quant.monitor.alerter.smtplib.SMTP_SSL")
    def test_send_uses_ssl(self, mock_smtp_cls):
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        alerter = EmailAlerter(_smtp())
        assert alerter.send("subject", "body") is True

        mock_smtp.login.assert_called_once()
        mock_smtp.send_message.assert_called_once()

    @patch("quant.monitor.alerter.smtplib.SMTP_SSL")
    def test_send_returns_false_on_error(self, mock_smtp_cls):
        mock_smtp_cls.side_effect = OSError("boom")
        alerter = EmailAlerter(_smtp())
        assert alerter.send("subject", "body") is False


class TestCliTestEmail:
    """`quant test-email`：SMTP 未配置时退出码 1，配置且发送成功为 0。"""

    @staticmethod
    def _args(to=None):
        from types import SimpleNamespace

        return SimpleNamespace(config="config/config.toml", to=to)

    @staticmethod
    def _settings(smtp: SmtpSettings):
        from quant.foundation.config import OKXSettings, Settings

        return Settings(
            okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
            smtp=smtp,
        )

    def test_returns_one_when_smtp_missing(self, monkeypatch):
        from quant.cli import test_email

        settings = self._settings(SmtpSettings())
        monkeypatch.setattr(
            test_email.Settings, "load", classmethod(lambda cls, _p: settings)
        )
        assert test_email.main(self._args()) == 1

    def test_returns_zero_when_sent(self, monkeypatch):
        from quant.cli import test_email

        settings = self._settings(_smtp())
        monkeypatch.setattr(
            test_email.Settings, "load", classmethod(lambda cls, _p: settings)
        )
        monkeypatch.setattr(
            test_email.EmailAlerter, "send", lambda self, s, b: True
        )
        assert test_email.main(self._args()) == 0
