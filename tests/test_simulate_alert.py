"""simulate-alert CLI 测试（M3）。

用 respx 拦截 HTTP，覆盖：
- 默认配置拼 URL / token
- --url / --token 覆盖
- 限价单 payload
- dry-run 不发请求
- 非 200 返回 1
- 缺少 --price 的限价单直接失败
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx

from quant.cli import simulate_alert
from quant.foundation.config import OKXSettings, Settings, WebhookSettings

URL = "http://127.0.0.1:8000/webhook/tv"


def _settings(token: str = "secret-token") -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        webhook=WebhookSettings(
            host="127.0.0.1", port=8000, path="/webhook/tv", auth_token=token
        ),
    )


def _args(**overrides) -> SimpleNamespace:
    args = {
        "config": "config/config.toml",
        "strategy": "sma_cross",
        "side": "buy",
        "order_type": "market",
        "inst": "BTC-USDT",
        "qty": "0.001",
        "price": None,
        "url": None,
        "token": None,
        "dry_run": False,
    }
    args.update(overrides)
    return SimpleNamespace(**args)


@pytest.fixture
def patched_settings(monkeypatch):
    monkeypatch.setattr(simulate_alert.Settings, "load", classmethod(lambda cls, p: _settings()))


@respx.mock
def test_market_alert_posts_payload(patched_settings):
    route = respx.post(URL).respond(200, json={"status": "accepted"})

    code = simulate_alert.main(_args())

    assert code == 0
    assert route.called
    req = route.calls[0].request
    assert req.headers["X-Auth-Token"] == "secret-token"
    body = req.read().decode()
    assert '"side":"buy"' in body or '"side": "buy"' in body
    assert "0.001" in body
    assert "price" not in body


@respx.mock
def test_limit_alert_includes_price(patched_settings):
    route = respx.post(URL).respond(200, json={})

    code = simulate_alert.main(
        _args(order_type="limit", price="64000")
    )

    assert code == 0
    body = route.calls[0].request.read().decode()
    assert "64000" in body


@respx.mock
def test_url_and_token_override(patched_settings):
    url = "http://127.0.0.1:9999/custom"
    route = respx.post(url).respond(200, json={})

    code = simulate_alert.main(_args(url=url, token="other"))

    assert code == 0
    assert route.calls[0].request.headers["X-Auth-Token"] == "other"


@respx.mock
def test_dry_run_sends_nothing(patched_settings):
    route = respx.post(URL).respond(200, json={})

    code = simulate_alert.main(_args(dry_run=True))

    assert code == 0
    assert not route.called


@respx.mock
def test_non_200_returns_failure(patched_settings):
    respx.post(URL).respond(400, json={"error": "invalid"})

    assert simulate_alert.main(_args()) == 1


def test_limit_without_price_returns_failure(patched_settings):
    assert simulate_alert.main(_args(order_type="limit")) == 1


@respx.mock
def test_network_error_returns_failure(patched_settings):
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))

    assert simulate_alert.main(_args()) == 1
