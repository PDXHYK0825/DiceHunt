"""Lead 探查：状态爆炸的驱动因素分解（实测，不是复述设计文档推导）。

分别单独变化：
  A. 骰子值 N（决定候选序列数：sum_{k=0..N} 4^k）
  B. 搜索深度 d
  C. 每节点候选上限 K
  D. 地图规模 / 资源数（宝藏 T、陷阱 X）
记录节点数、候选数、缓存命中率与耗时，用于定位主要的组合爆炸来源。
"""
from __future__ import annotations

import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))

from treasure_game.ai_module import AiModule  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, Seat  # noqa: E402


def build(h, w, seed, treasures=None, traps=None) -> GameState:
    treasures = treasures if treasures is not None else max(3, h * w // 18)
    traps = traps if traps is not None else max(2, h * w // 24)
    tm, pa, pb = MapModule.generate(h, w, int(h * w * 0.12), treasures, traps,
                                    random.Random(seed))
    return GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                     turn=Seat.B, mode=GameMode.NORMAL, turn_index=0)


def probe(state: GameState, dice: int, depth: int, k: int, *, exhaustive: bool = False):
    ai = AiModule(depth=depth, candidate_limit=k, partition_threshold=10 ** 9)
    if exhaustive:
        ai = AiModule(depth=depth, candidate_limit=k, partition_threshold=10 ** 9,
                      exhaustive_candidates=True)
    t0 = time.perf_counter()
    mv = ai.get_opt_move(state, dice, Seat.B)
    ms = (time.perf_counter() - t0) * 1000.0
    d = ai.get_diagnostics()
    return {"nodes": d.nodes, "hits": d.cache_hits, "misses": d.cache_misses,
            "ms": ms, "len": len(mv)}


print("=" * 92)
print("A) 骰子值 N 的影响（depth=3, K=24）：候选序列数理论上限 sum(4^k) = 1,5,21,85,341,1365,5461")
print("=" * 92)
st = build(12, 12, 7)
print(f"{'N':>3}{'节点':>8}{'候选枚举上限':>14}{'耗时ms':>10}{'命中':>8}{'未命中':>9}")
for n in (1, 2, 3, 4, 5, 6):
    r = probe(st, n, 3, 24)
    theory = sum(4 ** k for k in range(0, n + 1))
    print(f"{n:>3}{r['nodes']:>8}{theory:>14}{r['ms']:>10.1f}{r['hits']:>8}{r['misses']:>9}")

print()
print("=" * 92)
print("B) 搜索深度 d 的影响（N=3, K=24）")
print("=" * 92)
print(f"{'depth':>6}{'节点':>9}{'耗时ms':>10}{'相对上1层':>12}")
prev = None
for dep in (1, 2, 3, 4):
    r = probe(st, 3, dep, 24)
    ratio = "-" if prev in (None, 0) else f"{r['nodes'] / prev:.2f}x"
    print(f"{dep:>6}{r['nodes']:>9}{r['ms']:>10.1f}{ratio:>12}")
    prev = r["nodes"]

print()
print("=" * 92)
print("C) 候选上限 K 的影响（N=3, depth=3）—— K=0 表示不截断")
print("=" * 92)
print(f"{'K':>5}{'节点':>9}{'耗时ms':>10}{'命中':>8}")
for k in (0, 4, 8, 16, 24, 48):
    r = probe(st, 3, 3, k)
    print(f"{k:>5}{r['nodes']:>9}{r['ms']:>10.1f}{r['hits']:>8}")

print()
print("=" * 92)
print("D) 规模与资源数的影响（N=3, depth=3, K=24）")
print("=" * 92)
print(f"{'场景':>26}{'面积':>7}{'宝藏':>6}{'陷阱':>6}{'节点':>9}{'耗时ms':>10}")
cases = [
    ("8x8  默认", 8, 8, None, None),
    ("10x10 默认", 10, 10, None, None),
    ("12x12 默认", 12, 12, None, None),
    ("14x14 默认", 14, 14, None, None),
    ("12x12 宝藏=4", 12, 12, 4, 6),
    ("12x12 宝藏=8", 12, 12, 8, 6),
    ("12x12 宝藏=16", 12, 12, 16, 6),
    ("12x12 宝藏=30", 12, 12, 30, 6),
    ("12x12 陷阱=2", 12, 12, 8, 2),
    ("12x12 陷阱=12", 12, 12, 8, 12),
]
for label, h, w, t, x in cases:
    s = build(h, w, 7, t, x)
    r = probe(s, 3, 3, 24)
    area = h * w
    print(f"{label:>26}{area:>7}{len(s.true_map.remaining_treasures):>6}"
          f"{len(s.true_map.active_traps):>6}{r['nodes']:>9}{r['ms']:>10.1f}")

print()
print("=" * 92)
print("E) 穷举候选（exhaustive）对比：K 截断到底省了多少（N=3, depth=2）")
print("=" * 92)
for k in (0, 24):
    r = probe(st, 3, 2, k, exhaustive=True)
    print(f"  exhaustive K={k:<3} 节点={r['nodes']:>7} 耗时={r['ms']:>8.1f} ms")
