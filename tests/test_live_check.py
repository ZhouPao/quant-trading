"""M8-7 实盘接入自检：按 /account/config 判定 Key 权限与账户配置。"""

from __future__ import annotations

from quant.okx.live_check import (
    FAIL,
    PASS,
    WARN,
    evaluate_account_config,
    has_failures,
    run_checks,
)

_HEALTHY = {
    "uid": "1002",
    "mainUid": "1001",
    "perm": "read_only,trade",
    "ip": "203.0.113.7",
    "acctLv": "2",
}


def _status(items, name: str) -> str:
    return next(item for item in items if item.name == name).status


class TestEvaluateAccountConfig:
    def test_healthy_sub_account_passes(self) -> None:
        items = evaluate_account_config(_HEALTHY)

        assert not has_failures(items)
        assert _status(items, "交易权限") == PASS
        assert _status(items, "提币权限") == PASS
        assert _status(items, "子账户") == PASS
        assert _status(items, "IP 白名单") == PASS
        assert _status(items, "账户模式") == PASS

    def test_withdraw_permission_is_fatal(self) -> None:
        items = evaluate_account_config({**_HEALTHY, "perm": "read_only,trade,withdraw"})

        assert _status(items, "提币权限") == FAIL
        assert has_failures(items)

    def test_read_only_key_cannot_trade(self) -> None:
        items = evaluate_account_config({**_HEALTHY, "perm": "read_only"})

        assert _status(items, "交易权限") == FAIL
        assert has_failures(items)

    def test_main_account_only_warns(self) -> None:
        items = evaluate_account_config({**_HEALTHY, "uid": "1001", "mainUid": "1001"})

        assert _status(items, "子账户") == WARN
        # 主账户不是阻断项：PRD 建议走子账户，但不该让人无法启动
        assert not has_failures(items)

    def test_unbound_ip_warns(self) -> None:
        items = evaluate_account_config({**_HEALTHY, "ip": ""})

        assert _status(items, "IP 白名单") == WARN
        assert not has_failures(items)

    def test_missing_fields_are_not_treated_as_pass(self) -> None:
        items = evaluate_account_config({})

        assert _status(items, "交易权限") == FAIL
        assert _status(items, "子账户") == WARN
        assert _status(items, "IP 白名单") == WARN
        assert _status(items, "账户模式") == PASS  # 仅展示，未知也放行


class _FakeClient:
    def __init__(self, config: dict) -> None:
        self._config = config
        self.calls = 0

    def get_account_config(self) -> dict:
        self.calls += 1
        return self._config


def test_run_checks_reads_account_config_once() -> None:
    client = _FakeClient(_HEALTHY)

    items = run_checks(client)  # type: ignore[arg-type]

    assert client.calls == 1
    assert not has_failures(items)


class TestCliCheckLive:
    """`quant check-live`：没有 fail 项返回 0，有 fail 或接口失败返回 1。"""

    @staticmethod
    def _args():
        from types import SimpleNamespace

        return SimpleNamespace(config="config/config.toml")

    @staticmethod
    def _settings():
        from quant.foundation.config import OKXSettings, Settings

        return Settings(
            okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=False)
        )

    def _run(self, monkeypatch, config: dict, *, error: Exception | None = None):
        from quant.cli import check_live

        settings = self._settings()
        monkeypatch.setattr(
            check_live.Settings, "load", classmethod(lambda cls, _p: settings)
        )

        class Client:
            def __init__(self, _settings) -> None:
                pass

            def get_account_config(self) -> dict:
                if error is not None:
                    raise error
                return config

            def close(self) -> None:
                pass

        monkeypatch.setattr(check_live, "OKXClient", Client)
        return check_live.main(self._args())

    def test_returns_zero_when_checks_pass(self, monkeypatch) -> None:
        assert self._run(monkeypatch, _HEALTHY) == 0

    def test_returns_one_when_withdraw_enabled(self, monkeypatch) -> None:
        config = {**_HEALTHY, "perm": "read_only,trade,withdraw"}
        assert self._run(monkeypatch, config) == 1

    def test_returns_one_when_api_fails(self, monkeypatch) -> None:
        from quant.foundation.errors import AuthError

        assert self._run(monkeypatch, {}, error=AuthError("50101", "key mismatch")) == 1

