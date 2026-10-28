"""Models: 数据模型（spec §5）。

- Bar: K线（OHLCV + confirm）
- Tick: 实时盘口
- Instrument: 产品配置
- FundingRate: 资金费率

全部 frozen dataclass + Decimal 精度，构造时校验。
"""
from quant.models.bar import Bar
from quant.models.funding_rate import FundingRate
from quant.models.instrument import Instrument
from quant.models.tick import Tick

__all__ = ["Bar", "Tick", "Instrument", "FundingRate"]
