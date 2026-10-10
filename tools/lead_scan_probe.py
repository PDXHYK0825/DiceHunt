"""在**真实搜索过程中**统计每个节点的实际候选数（通过包装 `_Search._opt_candidates`）。

不修改源码，只在运行时插桩，测的是实际用到的候选规模。
"""
from __future__ import annotations

import collections
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))

from treasure_game.ai_module import AiModule  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, Seat  # noqa: E402
from treasure_game.path_module import PathModule  # noqa: E402
from treasure_game import opt_ai  # noqa: E402


def build(h, w, seed, treasures=None, traps=None) -> GameState:
    treasures = treasures if treasures is not None else max(3, h * w // 18)
    traps = traps if traps is not None else max(2, h * w // 24)
    tm, pa, pb = MapModule.generate(h, w, int(h * w * 0.12), treasures, traps,
                                    random.Random(seed))
    return GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                     turn=Seat.B, mode=GameMode.NORMAL, turn_index=0)


stats: dict[str, object] = {"sizes": [], "by_dice": collections.defaultdict(list)}
_orig = opt_ai._Search._opt_candidates


def instrumented(self, m, pos_opt, pos_human, dice):
    out = _orig(self, m, pos_opt, pos_human, dice)
    stats["sizes"].append(len(out))            # type: ignore[union-attr]
    stats["by_dice"][dice].append(len(out))    # type: ignore[union-attr]
    return out


opt_ai._Search._opt_candidates = instrumented

st = build(12, 12, 7)
print("=" * 88)
print("真实搜索中：MAX 节点的候选数（depth=3, K=24, 12x12 默认）")
print("=" * 88)
print(f"{'N':>3}{'MAX节点数':>11}{'候选均值':>10}{'候选最大':>10}{'候选总数(展开)':>16}")
for n in (1, 2, 3, 4, 5, 6):
    stats["sizes"] = []
    stats["by_dice"] = collections.defaultdict(list)
    ai = AiModule(depth=3, candidate_limit=24, partition_threshold=10 ** 9)
    ai.get_opt_move(st, n, Seat.B)
    sizes = stats["sizes"]                       # type: ignore[assignment]
    nodes = ai.get_diagnostics().nodes
    theory = sum(4 ** k for k in range(0, n + 1))
    print(f"{n:>3}{nodes:>11}{sum(sizes)/max(1,len(sizes)):>10.1f}{max(sizes, default=0):>10}"
          f"{sum(sizes):>16}   (穷举上限/节点={theory})")

print()
print("=" * 88)
print("MIN（人类）节点的候选数：由 HumanPolicy 的视图策略集决定")
print("=" * 88)
from treasure_game.human_policy import HumanPolicy  # noqa: E402
from treasure_game.models import PublicStateView  # noqa: E402

view = MapModule(st.true_map).get_view_map(GameMode.NORMAL)
poly = HumanPolicy("GATHER", random.Random(1))
for n in (1, 2, 3, 4, 6):
    pub = PublicStateView(mode=GameMode.NORMAL, pos_a=st.pos_a, pos_b=st.pos_b,
                          score_a=0, score_b=0, turn=Seat.A, turn_index=0,
                          dice_N=n, remaining_treasure_count=len(st.true_map.remaining_treasures))
    acts = poly.candidate_actions(view, Seat.A, n, pub)
    print(f"  N={n}: 人类候选={len(acts):>3}  (穷举上限 {sum(4**k for k in range(0, n+1)):>5})")
