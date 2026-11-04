"""配置只读视图（M10-1 + M10-9）。

`GET /api/config` 返回**脱敏后**的生效配置，并逐项标注来源：

- `toml`    — 来自 `config.toml`
- `env`     — 来自 `${VAR}` / `${VAR:-default}` 环境变量引用
- `default` — 未在 toml 声明，由 Pydantic 默认值填充

密钥类字段（OKX 凭证、SMTP 密码、dashboard token）只显示「已配置/未配置」，
永不回显原值；Redis URL 里的密码同样脱敏。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from quant.foundation.config import Settings

REDACTED = "***"

#: 永不回显的配置键（展平后的点路径）
SECRET_KEYS = frozenset(
    {
        "okx.api_key",
        "okx.secret",
        "okx.passphrase",
        "smtp.password",
        "dashboard.token",
    }
)

_PASSWORD_IN_URL = re.compile(r"://[^@/]*@")

#: 生效方式：hot=在线改立即生效；restart=改 toml 后重启；env_only=仅环境变量
RELOAD_MODES: dict[str, str] = {
    "hot": "在线修改，立即生效",
    "restart": "改 config.toml 后重启对应进程",
    "env_only": "只能通过环境变量提供，改完需重启",
}

#: 热改白名单（M10-10）：模式 + 可操作入口。未命中的一律 restart。
_HOT_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "okx.",
        "env_only",
        "OKX 凭证只能通过环境变量提供，改完需重启 worker；看板永不回显原值",
    ),
    (
        "smtp.password",
        "env_only",
        "SMTP 密码只能通过环境变量提供，改完需重启 monitor",
    ),
    (
        "dashboard.",
        "restart",
        "看板地址/端口/静态目录需改 config.toml 后重启 dashboard",
    ),
    (
        "redis.",
        "restart",
        "Redis 地址与键名需改 config.toml 后重启所有进程",
    ),
    (
        "state.",
        "restart",
        "状态库路径需改 config.toml 后重启所有进程（并先停进程）",
    ),
    (
        "risk.",
        "hot",
        "「风险控制」页在线调整（POST /api/risk/overrides），worker 下一轮心跳生效",
    ),
)


def reloadability(key: str) -> tuple[str, str]:
    """配置键 -> (生效方式, 操作提示)。"""
    for prefix, mode, hint in _HOT_RULES:
        if key == prefix.rstrip(".") or key.startswith(prefix):
            return mode, hint
    return "restart", "改 config.toml 后重启对应进程"


def hot_reload_rules() -> list[dict[str, str]]:
    """热改白名单（供设置页图例/脚本查询）。"""
    return [
        {"pattern": prefix, "mode": mode, "hint": hint}
        for prefix, mode, hint in _HOT_RULES
    ]


def _flatten(value: object, prefix: str = "") -> dict[str, object]:
    """把嵌套配置展平成点路径；列表作为一个整体值展示。"""
    out: dict[str, object] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(item, path))
        return out
    out[prefix] = value
    return out


def _redact(key: str, value: object) -> object:
    """密钥只报「是否配置」，URL 里的密码替换为 ***。"""
    if key in SECRET_KEYS:
        return REDACTED if value not in (None, "", [], {}) else ""
    if key == "redis.url" and isinstance(value, str):
        return _PASSWORD_IN_URL.sub("://***@", value)
    return value


def config_snapshot(
    settings: Settings, config_path: Path | str | None = None
) -> dict[str, object]:
    """返回 `{config_path, config_exists, items[]}`，每项含 key/value/source。"""
    path = Path(config_path or "config/config.toml")
    declared: dict[str, object] = {}
    exists = path.is_file()
    if exists:
        try:
            declared = _flatten(
                tomllib.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            # 配置坏了不该让整页 500：值仍来自已加载的 settings，
            # 只是来源全部退化为 default
            declared = {}

    values = _flatten(settings.model_dump(mode="json"))
    items: list[dict[str, object]] = []
    for key in sorted(values):
        if key in declared:
            # toml 里写的是 ${VAR} 还是字面量：决定「环境变量」还是「配置文件」
            source = "env" if "${" in str(declared[key]) else "toml"
        else:
            source = "default"
        mode, hint = reloadability(key)
        items.append(
            {
                "key": key,
                "value": _redact(key, values[key]),
                "source": source,
                # M10-10：让用户知道「改这个该去哪」，而不是默认 toml 会热生效
                "reload": mode,
                "reload_hint": hint,
            }
        )
    return {
        "config_path": str(path),
        "config_exists": exists,
        "items": items,
    }


__all__ = [
    "REDACTED",
    "RELOAD_MODES",
    "SECRET_KEYS",
    "config_snapshot",
    "hot_reload_rules",
    "reloadability",
]
