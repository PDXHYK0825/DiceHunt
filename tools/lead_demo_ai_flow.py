"""演示：一轮里"人类先走 → AI 看到走完后的局面 → AI 决策"的确切时序。

用一个手写小图 + 固定骰序，逐步打印双方动作、棋盘变化，以及 AI 决策时的可见信息。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))

from treasure_game.ai_module import AiModule  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import (  # noqa: E402
    Direction, GameMode, GameState, Position, Seat, TrueMap,
)
from treasure_game.simulator import SeatPolicy, Simulator  # noqa: E402


def board(m: TrueMap, pos_a: Position, pos_b: Position, mode: GameMode, reveal=None) -> str:
    view = MapModule(m).get_view_map(mode)
    lines = []
    for r in range(m.height):
        row = []
        for c in range(m.width):
            if Position(r, c) == pos_a and Position(r, c) == pos_b:
                row.append("AB")
            elif Position(r, c) == pos_a:
                row.append(" A")
            elif Position(r, c) == pos_b:
                row.append(" B")
            else:
                v = view.cell_value(r, c)
                row.append(" #" if v == 1 else (" ." if v == 0 else f"{v:>2}"))
        lines.append("  " + " ".join(row))
    return "\n".join(lines)


# 8x8：A 初始 (3,0)、B 初始 (3,4)；宝藏 (3,2)=+6 与 (7,7)=+2；陷阱 (5,5)=-2
TERRAIN = [[0] * 8 for _ in range(8)]
TM = TrueMap.create(height=8, width=8, terrain=TERRAIN,
                    treasures={Position(3, 2): 6, Position(7, 7): 2},
                    traps={Position(5, 5): -2})
POS_A, POS_B = Position(3, 0), Position(3, 4)
MODE = GameMode.NORMAL


class TraceIO:
    """只负责按预置脚本走棋的人类适配器，并把每次调用打印出来。"""

    def __init__(self, script):
        self.script = list(script)
        self.n = 0

    def render(self, view, state_view, events):  # noqa: ANN001
        return None

    def next_action(self, seat, dice_N, view, state_view):  # noqa: ANN001
        seq = self.script[self.n] if self.n < len(self.script) else ()
        self.n += 1
        print(f"      [人类输入] 骰子 N={dice_N}  输入序列={[str(d) for d in seq] or '结束回合'}")
        return tuple(seq)

    def log(self, message):  # noqa: ANN001
        return None


print("=" * 84)
print("初始局面（A=人类 在 (3,0)，B=OPT AI 在 (3,4)）")
print("=" * 84)
print(board(TM, POS_A, POS_B, MODE))
print("  图例：# 墙 | 正数 宝藏 | 负数 陷阱 | . 空地 | A 人类 | B 机器")

# 固定骰序：让人类先手有足够步数走到宝藏 (3,2)
DICE = [3, 3, 3, 3, 3, 3, 3, 3]
io = TraceIO([(Direction.RIGHT, Direction.RIGHT),
              (Direction.RIGHT,), (Direction.RIGHT,)])
state = GameState(true_map=TM, pos_a=POS_A, pos_b=POS_B, score_a=0, score_b=0,
                  turn=Seat.A, mode=MODE, turn_index=0)
ai = AiModule(depth=2, candidate_limit=24, partition_threshold=10 ** 9)

print()
print("=" * 84)
print("同一轮内的时序（骰序固定为 3，人类脚本已预置）")
print("=" * 84)

# ---- 第一轮：A 先动 ----
print()
print("【第 1 轮 · 座位 A（人类）】")
print(f"  决策前棋盘：")
print(board(state.true_map, state.pos_a, state.pos_b, MODE))
print(f"    A 位置={state.pos_a}  B 位置={state.pos_b}  比分 A={state.score_a} B={state.score_b}")

# 手工模拟 A 的移动（用 PathModule 保证与真实语义一致）
from treasure_game.path_module import PathModule  # noqa: E402

a_move = PathModule.sim_player_multi_step(state.true_map, state.pos_a,
                                          (Direction.RIGHT, Direction.RIGHT), 3, actor=Seat.A)
print(f"  → A 走了 右,右：终点 {a_move.final_pos}，得分增量 {a_move.score_delta:+d}，"
      f"有效步 {a_move.valid_steps}")
state_a = GameState(true_map=a_move.true_map, pos_a=a_move.final_pos, pos_b=state.pos_b,
                    score_a=state.score_a + a_move.score_delta, score_b=state.score_b,
                    turn=Seat.B, mode=MODE, turn_index=0)
print(f"  A 走完后的棋盘（这正是 AI 将要看到的局面）：")
print(board(state_a.true_map, state_a.pos_a, state_a.pos_b, MODE))
print(f"    A 位置={state_a.pos_a}  B 位置={state_a.pos_b}  "
      f"比分 A={state_a.score_a} B={state_a.score_b}  "
      f"剩余宝藏={len(state_a.true_map.remaining_treasures)}")

# ---- 第二轮：B（AI）决策 ----
print()
print("【第 1 轮 · 座位 B（OPT AI）】")
print("  AI 此刻的输入 = 上面那个 state_a（真值图）+ 本回合骰子 N=3")
print("  AI 接下来内部做的事：")
print("    1) 生成自己的候选走法（空动作 / 走向宝藏 / 靠近对手 / 远离对手）")
print("    2) 对每个候选，用 PathModule 在真值图上推演本回合结果")
print("    3) 展开 Expecti-Max 搜索树（我的骰子已知，之后是人类骰子 6 面 × 人类候选）")
print("    4) 取 U 最大的候选作为本回合动作")

b_seq = ai.get_opt_move(state_a, 3, Seat.B)
d = ai.get_diagnostics()
print()
print(f"  → AI 决定走出：{[str(x) for x in b_seq]}")
print(f"     搜索节点={d.nodes}  缓存命中={d.cache_hits}  耗时={d.elapsed_ns/1e6:.1f} ms")
print(f"     预测终点={d.root_final_pos}  预测得分增量={d.root_score_delta:+d}")

b_move = PathModule.sim_player_multi_step(state_a.true_map, state_a.pos_b, b_seq, 3, actor=Seat.B)
from treasure_game.battle_module import BattleModule  # noqa: E402

battle = BattleModule.trigger_battle(b_move.true_map, state_a.pos_a, b_move.final_pos,
                                     state_a.score_a, state_a.score_b + b_move.score_delta)
print(f"     实际执行终点={b_move.final_pos}（与预测一致："
      f"{b_move.final_pos == d.root_final_pos}）")
print(f"     本回合末是否重合触发对战：{battle.triggered}")
print()
print("  第 1 轮结束后的棋盘：")
print(board(b_move.true_map, battle.pos_a, battle.pos_b, MODE))
print(f"    比分 A={battle.score_a} B={battle.score_b}  剩余宝藏="
      f"{len(b_move.true_map.remaining_treasures)}")
