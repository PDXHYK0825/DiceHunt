"""同一局面、不同搜索深度下，AI 候选的根效用差异。

用途：看清"AI 权衡了什么"以及"深度如何改变它的判断"。
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))

from treasure_game.models import GameMode, GameState, Position, Seat, TrueMap  # noqa: E402
from treasure_game.opt_ai import OptConfig, _Node, _Search  # noqa: E402
from treasure_game.path_module import PathModule  # noqa: E402

TM = TrueMap.create(height=8, width=8, terrain=[[0] * 8 for _ in range(8)],
                    treasures={Position(3, 2): 6, Position(7, 7): 2},
                    traps={Position(5, 5): -2})
TM_MID = dataclasses.replace(TM, remaining_treasures=frozenset({Position(7, 7)}))
STATE = GameState(true_map=TM_MID, pos_a=Position(3, 2), pos_b=Position(3, 4),
                  score_a=6, score_b=0, turn=Seat.B, mode=GameMode.NORMAL, turn_index=0)

print("局面：A(人类)=(3,2) 已得 6 分；B(AI)=(3,4) 得 0 分；剩余宝藏 (7,7)=+2；骰子 N=3")
print("=" * 86)

for depth in (1, 2, 3):
    cfg = OptConfig(depth=depth, candidate_limit=24)
    search = _Search(cfg, STATE, Seat.B)
    node = _Node(TM_MID, STATE.pos_b, STATE.pos_a, 0, 6)
    cands = search._opt_candidates(TM_MID, STATE.pos_b, STATE.pos_a, 3)  # noqa: SLF001
    rows = []
    for seq in cands:
        val = search.search_max(node, 3, cfg.depth, -1e18, 1e18)
        res = PathModule.sim_player_multi_step(TM_MID, STATE.pos_b, tuple(seq), 3, actor=Seat.B)
        label = result = None
        label = ",".join(str(d) for d in seq) or "(空)"
        rows.append((label, val.lo, res.final_pos, res.score_delta))
    rows.sort(key=lambda r: -r[1])
    spread = rows[0][1] - rows[-1][1]
    print(f"\ndepth={depth}   候选数={len(rows)}   "
          f"根 U 区间=[{rows[-1][1]:.2f}, {rows[0][1]:.2f}]   极差={spread:.2f}")
    for label, u, pos, sd in rows:
        print(f"    {label:<18} U={u:>9.2f}   终点={pos} 得分增量={sd:+d}")
    if spread == 0:
        print("    ⚠ 所有候选同值 → AI 的选择由并列打破规则决定，不是'偏好'")
