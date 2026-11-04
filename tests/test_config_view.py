"""M10-1 / M10-9 配置视图：脱敏、来源标注、缺失文件降级。"""
from __future__ import annotations

from pathlib import Path

from quant.dashboard.config_view import (
    REDACTED,
    config_snapshot,
    hot_reload_rules,
)
from quant.foundation.config import OKXSettings, Settings


def _settings() -> Settings:
    return Settings(
        okx=OKXSettings(
            api_key="key-123", secret="sec-456", passphrase="pass", demo=True
        ),
        dashboard={"token": "tk-789"},
    )


def _items(data: dict) -> dict[str, dict]:
    return {item["key"]: item for item in data["items"]}


class TestConfigSnapshot:
    def test_secrets_are_redacted(self) -> None:
        data = config_snapshot(_settings(), Path("does-not-exist.toml"))
        items = _items(data)

        assert items["okx.api_key"]["value"] == REDACTED
        assert items["okx.secret"]["value"] == REDACTED
        assert items["okx.passphrase"]["value"] == REDACTED
        assert items["dashboard.token"]["value"] == REDACTED

    def test_redis_url_password_redacted(self) -> None:
        settings = Settings(
            okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
            redis={"url": "redis://:secret@localhost:6379/0"},
        )

        data = config_snapshot(settings, Path("does-not-exist.toml"))

        value = str(_items(data)["redis.url"]["value"])
        assert "secret" not in value
        assert "***" in value

    def test_sources_distinguish_toml_env_and_default(self, tmp_path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            '[engine]\n'
            'bar = "4H"\n'
            'status_key = "${QUANT_STATUS_KEY:-quant:engine:status}"\n',
            encoding="utf-8",
        )

        data = config_snapshot(_settings(), config)
        items = _items(data)

        assert items["engine.bar"]["source"] == "toml"
        assert items["engine.status_key"]["source"] == "env"
        # 未在 toml 声明的项由 Pydantic 默认值填充
        assert items["okx.api_key"]["source"] == "default"

    def test_missing_config_marks_all_default(self, tmp_path) -> None:
        data = config_snapshot(_settings(), tmp_path / "nope.toml")

        assert data["config_exists"] is False
        assert all(item["source"] == "default" for item in data["items"])

    def test_values_reflect_effective_settings(self, tmp_path) -> None:
        data = config_snapshot(_settings(), tmp_path / "nope.toml")
        items = _items(data)

        assert items["okx.demo"]["value"] is True
        assert items["engine.bar"]["value"] == "1H"

    def test_reload_modes_mark_hot_restart_and_env_only(self) -> None:
        """M10-10：别让用户以为改 toml 会热生效。"""
        data = config_snapshot(_settings(), Path("nope.toml"))
        items = _items(data)

        assert items["risk.daily_loss_limit"]["reload"] == "hot"
        # 热改提示必须指向真实入口（POST /api/risk/overrides）
        assert "risk/overrides" in items["risk.daily_loss_limit"]["reload_hint"]
        assert items["okx.secret"]["reload"] == "env_only"
        assert items["engine.bar"]["reload"] == "restart"
        assert items["engine.bar"]["reload_hint"]

    def test_hot_reload_rules_cover_risk_and_env(self) -> None:
        rules = {item["pattern"]: item for item in hot_reload_rules()}

        assert rules["risk."]["mode"] == "hot"
        assert rules["okx."]["mode"] == "env_only"
        assert rules["redis."]["mode"] == "restart"

    def test_broken_toml_degrades_to_default(self, tmp_path) -> None:
        """配置语法错误不该让整页 500：值仍来自已加载的 settings。"""
        config = tmp_path / "config.toml"
        config.write_text("not = valid = toml", encoding="utf-8")

        data = config_snapshot(_settings(), config)

        assert data["config_exists"] is True
        assert all(item["source"] == "default" for item in data["items"])
