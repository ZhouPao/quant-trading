"""M10-2 凭证状态：缺凭证 / 探测成功 / 探测失败 / 缓存 / SMTP。"""
from __future__ import annotations

import pytest

from quant.dashboard import credentials
from quant.foundation.config import OKXSettings, Settings, SmtpSettings
from quant.foundation.errors import QuantError
from quant.okx.live_check import LiveCheckItem


@pytest.fixture(autouse=True)
def _clear_cache():
    credentials.clear_cache()
    yield
    credentials.clear_cache()


def _settings(**extra) -> Settings:
    return Settings(
        okx=OKXSettings(
            api_key="k", secret="s", passphrase="p", demo=True
        ),
        **extra,
    )


class _Client:
    def close(self) -> None:
        pass


class TestOkx:
    def test_missing_credentials(self) -> None:
        settings = Settings(
            okx=OKXSettings(api_key="", secret="", passphrase="")
        )

        status = credentials.okx_status(settings)

        assert status["configured"] is False
        assert "api_key" in str(status["error"])
        assert status["items"] == []

    def test_probe_success(self, monkeypatch) -> None:
        monkeypatch.setattr(credentials, "OKXClient", lambda _s: _Client())
        monkeypatch.setattr(
            credentials,
            "run_checks",
            lambda _c: [LiveCheckItem("交易权限", "pass", "perm=trade")],
        )

        status = credentials.okx_status(_settings())

        assert status["configured"] is True
        assert status["error"] is None
        assert status["items"] == [
            {"name": "交易权限", "status": "pass", "detail": "perm=trade"}
        ]

    def test_probe_failure_degrades(self, monkeypatch) -> None:
        monkeypatch.setattr(credentials, "OKXClient", lambda _s: _Client())

        def boom(_client):
            raise QuantError("50101 key mismatch")

        monkeypatch.setattr(credentials, "run_checks", boom)

        status = credentials.okx_status(_settings())

        # 配置是有的，只是探测失败：不能报成「未配置」
        assert status["configured"] is True
        assert "50101" in str(status["error"])
        assert status["items"] == []

    def test_cached_until_force(self, monkeypatch) -> None:
        calls = {"n": 0}

        def run(_client):
            calls["n"] += 1
            return []

        monkeypatch.setattr(credentials, "OKXClient", lambda _s: _Client())
        monkeypatch.setattr(credentials, "run_checks", run)
        settings = _settings()

        credentials.okx_status(settings)
        credentials.okx_status(settings)
        assert calls["n"] == 1

        credentials.okx_status(settings, force=True)
        assert calls["n"] == 2


class TestSmtp:
    def test_unconfigured(self) -> None:
        status = credentials.smtp_status(_settings())

        assert status["configured"] is False
        assert "host" in status["missing"]

    def test_configured(self) -> None:
        settings = _settings(
            smtp=SmtpSettings(
                host="smtp.example.com",
                username="u",
                to_addrs=["a@example.com", "b@example.com"],
            )
        )

        status = credentials.smtp_status(settings)

        assert status["configured"] is True
        assert status["recipients"] == 2
        assert status["missing"] == []


class TestCredentialStatus:
    def test_bundles_okx_and_smtp_without_secrets(self, monkeypatch) -> None:
        monkeypatch.setattr(credentials, "OKXClient", lambda _s: _Client())
        monkeypatch.setattr(credentials, "run_checks", lambda _c: [])

        payload = credentials.credential_status(_settings())

        assert set(payload) == {"okx", "smtp"}
        # 结构上就不存在密钥字段，杜绝「顺手加个 key 回显」
        assert set(payload["okx"]) == {
            "configured",
            "demo",
            "error",
            "items",
            "checked_at",
        }
        assert set(payload["smtp"]) == {
            "configured",
            "host",
            "port",
            "recipients",
            "missing",
        }
