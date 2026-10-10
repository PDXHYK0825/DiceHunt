"""Lead 独立性能/等价性测量（用于实验报告，不依赖队友自述）。

测量项：
1. 8x8 / 12x12 / 14x14 上 OPT 单次决策耗时、节点数、缓存命中率
2. 剪枝开/关、缓存开/关 的根效用与动作一致性（等价性）
3. 单体 OPT vs 分治近似 的加速比与动作一致率
"""
from __future__ import annotations

import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from treasure_game.ai_module import AiModule  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, Seat  # noqa: E402
from treasure_game.path_module import PathModule  # noqa: E402


def make_state(h: int, w: int, seed: int, mode: GameMode = GameMode.NORMAL) -> GameState:
    walls = int(h * w * 0.12)
    treasures = max(3, h * w // 18)
    traps = max(2, h * w // 24)
    tm, pa, pb = MapModule.generate(h, w, walls, treasures, traps, random.Random(seed))
    return GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                     turn=Seat.B, mode=mode)


def timed_decide(ai: AiModule, st: GameState, n: int = 5) -> tuple[list[float], tuple]:
    times = []
    mv = ()
    for _ in range(n):
        t0 = time.perf_counter()
        mv = ai.get_opt_move(st, 3, Seat.B)
        times.append((time.perf_counter() - t0) * 1000.0)
    return times, mv


def replay(state: GameState, seq, seat=Seat.B):
    return PathModule.sim_player_multi_step(state.true_map,
                                            state.pos_b if seat is Seat.B else state.pos_a,
                                            tuple(seq), 3, actor=seat)


print("=" * 78)
print("1) 单次决策耗时 / 节点 / 缓存（depth=3, K=24, threshold=144）")
print("=" * 78)
print(f"{'场景':<14}{'中位ms':>9}{'p95ms':>9}{'nodes':>8}{'hits':>7}{'misses':>8}"
      f"{'命中率':>8}{'算法':>16}{'近似':>7}")
for (h, w) in [(8, 8), (12, 12), (14, 14)]:
    st = make_state(h, w, seed=7)
    ai = AiModule(depth=3, candidate_limit=24, partition_threshold=144)
    times, mv = timed_decide(ai, st, n=5)
    d = ai.get_diagnostics()
    total = d.cache_hits + d.cache_misses
    rate = f"{d.cache_hits / total:.1%}" if total else "n/a"
    print(f"{f'{h}x{w}':<14}{statistics.median(times):>9.1f}{max(times):>9.1f}"
          f"{d.nodes:>8}{d.cache_hits:>7}{d.cache_misses:>8}{rate:>8}"
          f"{d.algorithm:>16}{str(d.approximate):>7}")

print()
print("=" * 78)
print("2) 等价性：剪枝开/关、缓存开/关 的根效用与动作")
print("=" * 78)
for seed in (0, 1, 2, 3):
    st = make_state(6, 6, seed=seed)
    row = []
    for use_cache in (True, False):
        for use_pruning in (True, False):
            ai = AiModule(depth=2, candidate_limit=0, partition_threshold=10**9,
                          use_cache=use_cache, use_pruning=use_pruning,
                          exhaustive_candidates=True)
            mv = ai.get_opt_move(st, 2, Seat.B)
            d = ai.get_diagnostics()
            u = ai.get_root_utility()
            row.append((use_cache, use_pruning, round(u, 6), tuple(str(x) for x in mv), d.nodes))
    utils = {r[2] for r in row}
    moves = {r[3] for r in row}
    ok = len(utils) == 1 and len(moves) == 1
    print(f"seed={seed} 根U={row[0][2]:+.6f} 动作={'+'.join(row[0][3]) or '(空)'} "
          f"四组合一致={ok} 节点={[r[4] for r in row]}")

print()
print("=" * 78)
print("3) 单体 OPT vs 分治近似（同图同骰）")
print("=" * 78)
print(f"{'场景':<10}{'单体ms':>10}{'分治ms':>10}{'加速比':>9}{'动作一致':>10}{'单体U':>10}{'分治U':>10}")
for (h, w) in [(12, 12), (14, 14)]:
    st = make_state(h, w, seed=11)
    mono = AiModule(depth=3, candidate_limit=24, partition_threshold=10**9)
    part = AiModule(depth=3, candidate_limit=24, partition_threshold=1)
    tm_, mv_m = timed_decide(mono, st, n=3)
    tp_, mv_p = timed_decide(part, st, n=3)
    um, up = mono.get_root_utility(), part.get_root_utility()
    dp = part.get_diagnostics()
    print(f"{f'{h}x{w}':<10}{statistics.median(tm_):>10.1f}{statistics.median(tp_):>10.1f}"
          f"{statistics.median(tm_) / statistics.median(tp_):>8.2f}x"
          f"{str(tuple(mv_m) == tuple(mv_p)):>10}{um:>10.2f}{up:>10.2f}  approx={dp.approximate}")

print()
print("=" * 78)
print("4) 根预测 = 全图重放（决议可复现性）")
print("=" * 78)
allsame = True
for (h, w) in [(8, 8), (12, 12)]:
    for seed in (1, 2, 3):
        st = make_state(h, w, seed=seed)
        ai = AiModule(depth=3, candidate_limit=24, partition_threshold=144)
        mv = ai.get_opt_move(st, 3, Seat.B)
        d = ai.get_diagnostics()
        rep = replay(st, mv)
        same = (rep.final_pos == d.root_final_pos
                and rep.score_delta == d.root_score_delta)
        allsame = allsame and same
        if not same:
            print(f"  MISMATCH {h}x{w} seed={seed}: {rep.final_pos} vs {d.root_final_pos}")
print(f"全部 {6} 组「预测终点/得分增量 == 重放结果」: {allsame}")
