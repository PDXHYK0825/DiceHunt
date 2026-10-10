"""A/B：单一启发式人类模型 vs 最坏情况人类模型。

两件事：
  1. 同一局面同一骰子下，两种模型选的动作有多少不同（行为差异）
  2. 同一批地图同一骰序下，两种模型各自对局的表现（是否真的更强/更主动）
"""
from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))

from treasure_game.ai_module import AiModule  # noqa: E402
from treasure_game.human_policy import HumanPolicy  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, PublicStateView, Seat  # noqa: E402
from treasure_game.simulator import SeatPolicy, Simulator  # noqa: E402
from treasure_game.opt_ai import HUMAN_MODEL_HEURISTIC, HUMAN_MODEL_WORST_CASE  # noqa: E402

MODELS = {
    "heuristic(GATHER)": dict(human_model=HUMAN_MODEL_HEURISTIC),
    "worst_case": dict(human_model=HUMAN_MODEL_WORST_CASE),
}


def make_state(h, w, seed, mode=GameMode.NORMAL) -> GameState:
    tm, pa, pb = MapModule.generate(h, w, int(h * w * 0.12), max(3, h * w // 18),
                                    max(2, h * w // 24), random.Random(seed))
    return GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                     turn=Seat.B, mode=mode, turn_index=0)


print("=" * 94)
print("1) 同一局面同一骰子：两种人类模型的动作分歧率（12x12，depth=3）")
print("=" * 94)
total = same = 0
samples = []
for seed in (7, 11, 23):
    st = make_state(12, 12, seed)
    for n in (2, 3, 4, 6):
        acts = {}
        for label, kw in MODELS.items():
            ai = AiModule(depth=3, candidate_limit=24, partition_threshold=10 ** 9, **kw)
            acts[label] = tuple(str(d) for d in ai.get_opt_move(st, n, Seat.B))
        total += 1
        if acts["heuristic(GATHER)"] == acts["worst_case"]:
            same += 1
        else:
            samples.append((seed, n, acts["heuristic(GATHER)"], acts["worst_case"]))
print(f"  对比局面数={total}  动作相同={same}  不同={total - same}  "
      f"分歧率={1 - same / total:.1%}")
for seed, n, hh, ww in samples[:5]:
    print(f"    seed={seed} N={n}: heuristic={list(hh) or '空'}  worst_case={list(ww) or '空'}")

print()
print("=" * 94)
print("2) 对局表现：同一批地图 + 同一骰序，AI(B) 用不同人类模型 vs 固定人类代理(A)")
print("=" * 94)
print(f"{'模型':<22}{'局数':>5}{'AI胜率(严格)':>13}{'分差均值':>10}{'样本std':>9}"
      f"{'AI踩陷阱':>9}{'对战均值':>9}{'AI耗时中位ms':>13}")

MAPS, DICE_SEEDS = 12, 2
results: dict[str, list] = {}
for label, kw in MODELS.items():
    rows = []
    for i in range(MAPS):
        map_seed = 42 + i
        tm, pa, pb = MapModule.generate(12, 12, 16, 8, 6, random.Random(map_seed))
        for j in range(DICE_SEEDS):
            dice_seed = 42 + 100003 + 7 * j
            state = GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                              turn=Seat.A, mode=GameMode.NORMAL, turn_index=0)
            table = tuple(random.Random(dice_seed).randint(1, 6) for _ in range(500 * 2))
            human = HumanPolicy("GATHER", random.Random(dice_seed))

            class _IO:
                def render(self, view, state_view, events):  # noqa: ANN001
                    return None

                def next_action(self, seat, dice_N, view, state_view):  # noqa: ANN001
                    return tuple(human.choose(view, seat, dice_N, state_view))

                def log(self, message):  # noqa: ANN001
                    return None

            sim = Simulator(
                initial_state=state, io=_IO(), rng=random.Random(dice_seed),
                policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT,
                max_turns=500,
                ai=AiModule(depth=3, candidate_limit=24, partition_threshold=144, **kw),
                dice_seed_table=table,
            )
            res = sim.run_human_vs_ai(GameMode.NORMAL)
            rows.append(res)
    results[label] = rows
    norm = [r for r in rows if not r.draw_timeout]
    win = statistics.fmean(1.0 if r.winner == "B" else 0.0 for r in norm)
    diff = statistics.fmean(r.score_b - r.score_a for r in norm)
    std = statistics.stdev([r.score_b - r.score_a for r in norm]) if len(norm) > 1 else 0.0
    trap_b = statistics.fmean(r.metrics["trap_steps_b"] for r in rows)
    bat = statistics.fmean(r.metrics["battles"] for r in rows)
    el = statistics.median(r.metrics["ai_elapsed_ns"] / 1e6 for r in rows)
    print(f"{label:<22}{len(rows):>5}{win:>13.3f}{diff:>10.2f}{std:>9.2f}"
          f"{trap_b:>9.2f}{bat:>9.2f}{el:>13.1f}")

print()
print("=" * 94)
print("3) 同图同骰配对差值（heuristic − worst_case，正数表示 heuristic 更好）")
print("=" * 94)
h_rows, w_rows = results["heuristic(GATHER)"], results["worst_case"]
d_diff = [(h.score_b - h.score_a) - (w.score_b - w.score_a) for h, w in zip(h_rows, w_rows)]
d_bat = [h.metrics["battles"] - w.metrics["battles"] for h, w in zip(h_rows, w_rows)]
d_trap = [h.metrics["trap_steps_b"] - w.metrics["trap_steps_b"] for h, w in zip(h_rows, w_rows)]


def boot(xs, n=4000, seed=11):
    rng = random.Random(seed)
    out = sorted(statistics.fmean([xs[rng.randrange(len(xs))] for _ in range(len(xs))])
                 for _ in range(n))
    return out[int(0.025 * n)], out[int(0.975 * n)]


for name, xs in (("分差 (scoreB-scoreA)", d_diff), ("对战次数", d_bat), ("AI 踩陷阱", d_trap)):
    lo, hi = boot(xs)
    print(f"  {name:<24} 均值={statistics.fmean(xs):+7.3f}  95%CI=[{lo:+.3f}, {hi:+.3f}]  "
          f"跨0={'是' if lo <= 0 <= hi else '否'}")
