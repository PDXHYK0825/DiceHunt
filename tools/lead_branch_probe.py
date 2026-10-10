"""分解真实搜索的节点来源：MAX / MIN / CHANCE 各展开多少、每个节点几个子节点。

插桩 `_Search._expand`（数值展开）与 `_Search.search_chance`（骰子六面），
统计每个深度的展开规模，定位组合爆炸到底来自哪一层。
"""
from __future__ import annotations

import collections
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))

from treasure_game.ai_module import AiModule  # noqa: E402
from treasure_game import opt_ai  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, Seat  # noqa: E402


def build(h, w, seed, treasures=None, traps=None) -> GameState:
    treasures = treasures if treasures is not None else max(3, h * w // 18)
    traps = traps if traps is not None else max(2, h * w // 24)
    tm, pa, pb = MapModule.generate(h, w, int(h * w * 0.12), treasures, traps,
                                    random.Random(seed))
    return GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                     turn=Seat.B, mode=GameMode.NORMAL, turn_index=0)


expand_log: list[tuple[bool, int, int]] = []      # (actor_is_opt, dice, 去重后子节点数)
chance_log: list[int] = []                        # 每个 chance 节点的面数

_orig_expand = opt_ai._Search._expand
_orig_chance = opt_ai._Search.search_chance


def expand_probe(self, node, candidates, actor_is_opt, dice):
    out = _orig_expand(self, node, candidates, actor_is_opt, dice)
    expand_log.append((actor_is_opt, dice, len(out)))
    return out


def chance_probe(self, node, depth, alpha, beta):
    chance_log.append(6)
    return _orig_chance(self, node, depth, alpha, beta)


opt_ai._Search._expand = expand_probe
opt_ai._Search.search_chance = chance_probe

st = build(12, 12, 7)

print("=" * 94)
print("节点来源分解（12x12, depth=3, K=24）")
print("=" * 94)
print(f"{'N':>3}{'总节点':>8}{'MAX展开':>9}{'MIN展开':>9}{'chance数':>10}"
      f"{'MAX去重后均值':>14}{'MIN去重后均值':>14}")
for n in (1, 2, 3, 4, 5, 6):
    expand_log.clear()
    chance_log.clear()
    ai = AiModule(depth=3, candidate_limit=24, partition_threshold=10 ** 9)
    ai.get_opt_move(st, n, Seat.B)
    mx = [e[2] for e in expand_log if e[0]]
    mn = [e[2] for e in expand_log if not e[0]]
    print(f"{n:>3}{ai.get_diagnostics().nodes:>8}{len(mx):>9}{len(mn):>9}{len(chance_log):>10}"
          f"{(sum(mx) / max(1, len(mx))):>14.2f}{(sum(mn) / max(1, len(mn))):>14.2f}")

print()
print("=" * 94)
print("深度 d 的影响：每层展开规模（N=3, K=24）")
print("=" * 94)
print(f"{'depth':>6}{'总节点':>9}{'MAX展开':>9}{'MIN展开':>9}{'chance数':>10}{'节点/展开':>11}")
for dep in (1, 2, 3, 4):
    expand_log.clear()
    chance_log.clear()
    ai = AiModule(depth=dep, candidate_limit=24, partition_threshold=10 ** 9)
    ai.get_opt_move(st, 3, Seat.B)
    total = ai.get_diagnostics().nodes
    n_exp = len(expand_log)
    print(f"{dep:>6}{total:>9}{len([e for e in expand_log if e[0]]):>9}"
          f"{len([e for e in expand_log if not e[0]]):>9}{len(chance_log):>10}"
          f"{(total / max(1, n_exp)):>11.2f}")

print()
print("=" * 94)
print("去重强度：候选数 → 去重后子节点数（N=6, depth=3）")
print("=" * 94)
expand_log.clear()
chance_log.clear()
ai = AiModule(depth=3, candidate_limit=24, partition_threshold=10 ** 9)
ai.get_opt_move(st, 6, Seat.B)
mx = [e[2] for e in expand_log if e[0]]
mn = [e[2] for e in expand_log if not e[0]]
print(f"  MAX 展开次数={len(mx)}  去重后子节点 均值={sum(mx)/max(1,len(mx)):.2f} "
      f"最大={max(mx, default=0)} 合计={sum(mx)}")
print(f"  MIN 展开次数={len(mn)}  去重后子节点 均值={sum(mn)/max(1,len(mn)):.2f} "
      f"最大={max(mn, default=0)} 合计={sum(mn)}")
print(f"  chance 节点数={len(chance_log)}（每个固定 6 面）→ 骰子分支合计={len(chance_log)*6}")
