"""对战地图寻宝策略分析平台 —— 包边界。

本模块**不导入任何子模块**（尤其不导入 ``gui``），因此 ``import treasure_game`` 零副作用、
零第三方依赖。调用方按需显式导入：

    from treasure_game.models import Direction, Position, TrueMap
    from treasure_game.path_module import PathModule

各模块的公开对象与冻结签名见 ``docs/06-interface-freeze.md``。
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
