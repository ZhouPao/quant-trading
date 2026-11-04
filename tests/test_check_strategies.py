"""M3-13：quant check-strategies 的 CLI 行为与严格模式。"""
from __future__ import annotations

from types import SimpleNamespace

from quant.cli.check_strategies import main as check_main


def _args(tmp_path=None, *, strict: bool) -> SimpleNamespace:
    return SimpleNamespace(
        strict=strict,
        pine_dir=str(tmp_path / "pine") if tmp_path else None,
        config_dir=str(tmp_path / "cfg") if tmp_path else None,
    )


class TestRepoCheck:
    def test_repo_has_no_unregistered_drift(self, capsys) -> None:
        code = check_main(_args(strict=True))

        assert code == 0
        assert "未登记 0 项" in capsys.readouterr().out


class TestCustomDirs:
    def _make(self, tmp_path, pine_extra: str = "") -> SimpleNamespace:
        pine = tmp_path / "pine"
        cfg = tmp_path / "cfg"
        pine.mkdir()
        cfg.mkdir()
        (pine / "demo.pine").write_text(
            "// 基线策略副本\n"
            "a = input(1)\n"
            f"{pine_extra}",
            encoding="utf-8",
        )
        (cfg / "demo.toml.example").write_text(
            "[params]\na = 1\n", encoding="utf-8"
        )
        return _args(tmp_path, strict=True)

    def test_unregistered_drift_fails_strict(self, tmp_path, capsys) -> None:
        # Pine 多出一个 b，toml 没有：未登记
        args = self._make(tmp_path, "b = input(2)\n")

        code = check_main(args)

        assert code == 1
        assert "未登记 1 项" in capsys.readouterr().out

    def test_non_strict_returns_zero(self, tmp_path) -> None:
        args = self._make(tmp_path, "b = input(2)\n")
        args.strict = False

        assert check_main(args) == 0

    def test_matching_params_pass(self, tmp_path, capsys) -> None:
        args = self._make(tmp_path)

        code = check_main(args)

        assert code == 0
        assert "未发现任何差异" in capsys.readouterr().out
