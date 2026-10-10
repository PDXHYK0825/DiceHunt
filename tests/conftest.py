"""pytest 公共夹具：把 ``Project/src`` 注入 ``sys.path``，让 ``import treasure_game`` 可用。

``run_tests.cmd`` 已经通过 ``PYTHONPATH`` 注入了同样的路径；这里再兜一层，
保证直接执行 ``python -m pytest Project\\tests``（或 IDE 内运行）也能导入包。
本文件不得引入任何第三方依赖。
"""

from __future__ import annotations

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

if _SRC.is_dir():
    _src_text = str(_SRC)
    if _src_text not in sys.path:
        sys.path.insert(0, _src_text)
