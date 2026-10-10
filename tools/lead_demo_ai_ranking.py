"""把 AI 同一局面下的候选按根效用排序打印出来，看它"在权衡什么"。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))

from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import (  # noqa: E402
    GameMode, GameState, Position, Seat, TrueMap,
)
from treasure_game import opt_ai  # noqa: E402

TM = TrueMap.create(height=8, width=8, terrain=[[0] * 8 for _ in range(8)],
                    treasures={Position(3, 2): 6, Position(7, 7): 2},
                    traps={Position(5, 5): -2})
# 人类已吃掉 (3,2) 的 +6，AI 面对剩余 1 枚宝藏（与演示脚本一致的中间局面）
import dataclasses  # noqa: E402

TM_MID = dataclasses.replace(TM, remaining_treasures=frozenset({Position(7, 7)}))
STATE = GameState(true_map=TM_MID, pos_a=Position(3, 2), pos_b=Position(3, 4),
                  score_a=6, score_b=0, turn=Seat.B, mode=GameMode.NORMAL, turn_index=0)

print("=" * 84)
print("局面：A(人类)=(3,2) 得 6 分；B(AI)=(3,4) 得 0 分；剩余宝藏 (7,7)=+2；骰子 N=3")
print("=" * 84)

# 直接构造 _Search 以便查看每个根候选的精确效用
from treasure_game.opt_ai import OptConfig, _Search, _Node  # noqa: E402

cfg = OptConfig(depth=3, candidate_limit=24)
search = _Search(cfg, STATE, Seat.B)
node = _Node(TM_MID, STATE.pos_b, STATE.pos_a, 0, 6)
cands = search._opt_candidates(TM_MID, STATE.pos_b, STATE.pos_a, 3)  # noqa: SLF001

print(f"{'候选动作':<26}{'搜索根 U':>12}{'排序':>6}")
rows = []
for seq in cands:
    val = search.search_max(node, 3, cfg.depth, -1e18, 1e18)
    rows.append((tuple(seq), val.lo))
rows.sort(key=lambda r: -r[1])
from treasure_game.path_module import PathModule  # noqa: E402

for rank, (seq, u) in enumerate(rows, 1):
    text = ",".join(str(d) for d in seq) or "(空 = 放弃剩余步数)"
    res = PathModule.sim_player_multi_step(TM_MID, STATE.pos_b, seq, 3, actor=Seat.B)
    print(f"{text:<26}{u:>12.2f}{rank:>6}   本回合终点={res.final_pos} 得分增量={res.score_delta:+d}")

print()
print("注：U = scoreOpt − scoreHuman（AI 视角，越大越好）。")
print("人类走法在搜索内部由 HumanPolicy 的策略集产生，MIN 节点选对 AI 最不利的那个。")
