"""CLI 回测参数合并口径（M3-10，2026-11-04 拍板决策①）。

CLI 与看板/实盘共用 resolve_strategy 这个「函数与校验」，但默认不共用
「数据源」：离线研究必须可复现，不能被看板在线调参静默改变。
"""
from __future__ import annotations

from argparse import Namespace

import pytest

from quant.cli._params import resolve_cli_params
from quant.foundation.config import OKXSettings, Settings
from quant.state.sqlite_store import SQLiteStore
from quant.strategies.params import ParamError

STRATEGY_TOML = (
    "[strategy]\n"
    'strategy_id = "sma_cross"\n'
    'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
    "[params]\nfast_len = 5\nslow_len = 20\ncommission_rate = 0.001\n"
)
NOW = 1_700_000_000_000


def _settings(tmp_path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "quant.db"},
        engine={"config_dir": tmp_path / "strategies"},
    )


@pytest.fixture
def config_dir(tmp_path):
    directory = tmp_path / "strategies"
    directory.mkdir()
    (directory / "sma_cross.toml").write_text(STRATEGY_TOML)
    return directory


def _args(config_dir, params=None, use_db_params=False) -> Namespace:
    return Namespace(
        strategy="sma_cross",
        config_dir=str(config_dir),
        params=params,
        use_db_params=use_db_params,
    )


class TestCliParamBaseline:
    def test_default_ignores_db_override(self, tmp_path, config_dir) -> None:
        """默认纯 toml 基线：看板调过参也不改变历史回测结果（可复现）。"""
        settings = _settings(tmp_path)
        store = SQLiteStore(settings.state.db_path)
        store.set_strategy_param("sma_cross", "fast_len", "8", NOW)
        store.close()

        _cls, params, sources = resolve_cli_params(
            settings, _args(config_dir)
        )

        assert params["fast_len"] == 5
        assert sources["fast_len"] == "toml"

    def test_explicit_switch_reads_db_override(self, tmp_path, config_dir) -> None:
        settings = _settings(tmp_path)
        store = SQLiteStore(settings.state.db_path)
        store.set_strategy_param("sma_cross", "fast_len", "8", NOW)
        store.close()

        _cls, params, sources = resolve_cli_params(
            settings, _args(config_dir, use_db_params=True)
        )

        assert params["fast_len"] == 8
        assert sources["fast_len"] == "db"

    def test_global_rollback_switch_wins(self, tmp_path, config_dir) -> None:
        settings = _settings(tmp_path)
        settings.params.resolve_from_db = False
        store = SQLiteStore(settings.state.db_path)
        store.set_strategy_param("sma_cross", "fast_len", "8", NOW)
        store.close()

        _cls, params, _sources = resolve_cli_params(
            settings, _args(config_dir, use_db_params=True)
        )

        assert params["fast_len"] == 5

    def test_explicit_params_win_and_are_coerced(
        self, tmp_path, config_dir
    ) -> None:
        _cls, params, sources = resolve_cli_params(
            _settings(tmp_path), _args(config_dir, params="fast_len=12")
        )

        assert params["fast_len"] == 12
        assert sources["fast_len"] == "request"

    def test_fee_sensitivity_still_allowed(self, tmp_path, config_dir) -> None:
        """未声明但 toml 里存在的参数仍可显式覆盖做敏感性研究。"""
        _cls, params, sources = resolve_cli_params(
            _settings(tmp_path), _args(config_dir, params="commission_rate=0")
        )

        assert params["commission_rate"] == 0.0
        assert sources["commission_rate"] == "request"


class TestCliParamValidation:
    @pytest.mark.parametrize(
        "text",
        [
            "fast_len=1",  # 越界（min=2）
            "fast_len=nan",  # 非数字
            "injected_key=1",  # 键不在 toml / PARAM_SPEC
        ],
    )
    def test_invalid_params_are_rejected(
        self, tmp_path, config_dir, text
    ) -> None:
        with pytest.raises(ParamError):
            resolve_cli_params(
                _settings(tmp_path), _args(config_dir, params=text)
            )
