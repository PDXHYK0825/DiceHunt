"""Lead 调查 v2：定位 NORMAL+CAUTIOUS 下 map_seed=63 的往复循环。

用真实 Simulator + 记录型 GameIO 逐步打印人类与 AI 的动作、落点、比分与对战，
以区分「规则缺陷」与「启发式代理的病理行为」。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))
sys.path.insert(0, str(ROOT / "Project" / "tools"))

from lead_replay import RecordingAI, RecordingIO, build, make_dice_table  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, Seat  # noqa: E402
from treasure_game.path_module import PathModule  # noqa: E402
from treasure_game.simulator import SeatPolicy, Simulator  # noqa: E402


def trace(map_seed: int, dice_seed: int, mode: str, strategy: str, max_show: int = 24) -> None:
    tm, pa, pb = build(map_seed, 12, 12, 16, 8, 6)
    print(f"--- map_seed={map_seed} dice_seed={dice_seed} mode={mode} A策略={strategy} ---")
    def _key(p):
        return (p.row, p.col)

    print(f"    出生点 A={pa} B={pb}  陷阱={sorted(tm.trap_values, key=_key)}"
          f"  宝藏={sorted(tm.remaining_treasures, key=_key)}")
    state = GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                      turn=Seat.A, mode=GameMode(mode), turn_index=0)
    io = RecordingIO(strategy, dice_seed)
    ai = RecordingAI(depth=3, candidate_limit=24, partition_threshold=144)
    sim = Simulator(initial_state=state, io=io, rng=random.Random(dice_seed),
                    policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT,
                    max_turns=500, ai=ai, dice_seed_table=make_dice_table(dice_seed))
    result = sim.run_human_vs_ai(GameMode(mode))
    print(f"    终局：score_a={result.score_a} score_b={result.score_b} turns={result.turns} "
          f"draw_timeout={result.draw_timeout} battles={result.metrics.get('battles')}")

    moves = [e for e in result.events if e.kind == "MOVE"]
    battles = [e for e in result.events if e.kind == "BATTLE"]
    print(f"    MOVE 事件={len(moves)} BATTLE 事件={len(battles)}")
    print("    前若干步位置轨迹（座位, 落点）：")
    line = []
    for i, ev in enumerate(moves[:max_show]):
        who = ev.actor.value if ev.actor else "?"
        line.append(f"{who}{ev.position.row},{ev.position.col}")
    print("      " + " | ".join(line))
    # 统计位置重复，确认循环
    from collections import Counter
    pos_count = Counter((e.actor.value, e.position.row, e.position.col) for e in moves)
    top = pos_count.most_common(5)
    print(f"    最常出现的 (座位,位置) 次数：{top}")


if __name__ == "__main__":
    trace(63, 100045, "NORMAL", "CAUTIOUS")
    print()
    trace(63, 100045, "HARD", "CAUTIOUS")
    print()
    trace(63, 100045, "NORMAL", "GATHER")
