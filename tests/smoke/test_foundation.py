"""Foundation Step 1 烟测脚本。

验证：
1. Settings.load 能从 TOML + .env 加载，并展开 ${ENV}
2. 日志能写控制台和文件
3. 日志脱敏生效（api_key/secret/passphrase 替换为 ***REDACTED***）
4. 异常体系可用

运行：
    python3 tests/smoke/test_foundation.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 让 src 可被找到（在未 pip install -e 时也能跑）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# 加载 .env（M1 不依赖 python-dotenv，手工 load）
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip("'").strip('"')
        os.environ.setdefault(k, v)


def main() -> int:
    from quant.foundation import Settings, get_logger, setup_logger
    from quant.foundation.errors import AuthError, QuantError, RateLimitTimeoutError

    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "config" / "config.toml.example"

    # 用 .env 中的真值覆盖占位符（.example 里用 ${ENV} 引用语法）
    # 这里直接用 example 模板，因为它内部用 ${OKX_API_KEY} 等引用语法
    print("== 1. 加载配置 ==")
    settings = Settings.load(config_path)
    print(f"  OKX demo = {settings.okx.demo}")
    print(f"  storage.data_dir = {settings.storage.data_dir}")
    print(f"  rate_limit.timeout_seconds = {settings.rate_limit.timeout_seconds}")
    print(f"  logging.level = {settings.logging.level}")
    print(f"  redis.queue_key = {settings.redis.queue_key}")
    print(f"  webhook.host:port = {settings.webhook.host}:{settings.webhook.port}")
    print(f"  OKX_API_KEY 长度 = {len(settings.okx.api_key)} chars")
    print(f"  OKX_SECRET 长度 = {len(settings.okx.secret)} chars")

    # 关键：打印只显示前4位 + ****，验证真值能被读到
    k = settings.okx.api_key
    s = settings.okx.secret
    print(f"  OKX_API_KEY 预览 = {k[:4]}****（共 {len(k)} chars）")
    print(f"  OKX_SECRET 预览 = {s[:4]}****（共 {len(s)} chars）")

    print("\n== 2. 初始化日志 ==")
    log_path = project_root / "logs" / "smoke-test.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    setup_logger(
        level="DEBUG",
        file=str(log_path),
        rotation="1 MB",
        retention="1 days",
    )
    log = get_logger("smoke.foundation")

    print("\n== 3. 写日志（含脱敏验证）==")
    # 故意把敏感字段打到日志里，验证会被脱敏
    log.info(f"loaded settings, api_key={settings.okx.api_key}, secret={settings.okx.secret}")
    log.info(
        'json form: {"api_key": "' + settings.okx.api_key
        + '", "passphrase": "' + settings.okx.passphrase + '"}'
    )
    log.warning("acquire group=public timeout after 10.0s")
    log.debug("debug level also works")

    print("\n== 4. 异常体系 ==")
    try:
        raise AuthError(code="50101", msg="invalid api key")
    except QuantError as e:
        print(f"  caught QuantError: {e}")

    try:
        raise RateLimitTimeoutError(group="public", timeout=10.0)
    except RateLimitTimeoutError as e:
        print(f"  caught RateLimitTimeoutError: {e}, group={e.group}, timeout={e.timeout}")

    print("\n== 5. 检查日志文件 ==")
    if log_path.exists():
        content = log_path.read_text(encoding="utf-8")
        # 验证敏感值未出现在日志文件中
        if settings.okx.api_key in content or settings.okx.secret in content:
            print("  FAIL: 敏感值出现在日志文件中！")
            return 1
        if "***REDACTED***" in content:
            print("  PASS: 日志文件已脱敏")
        else:
            print("  WARN: 日志文件没找到 ***REDACTED***，可能脱敏未触发")
        print(f"  日志文件路径: {log_path}")
        print(f"  日志文件大小: {log_path.stat().st_size} bytes")
    else:
        print("  FAIL: 日志文件未生成")
        return 1

    print("\n== 全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
