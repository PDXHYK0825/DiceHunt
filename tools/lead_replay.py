"""Lead 独立重放（v2）：直接复用真实 Simulator，用记录型 GameIO 捕获每次决策。

与 experiments/run_batch.py 的 runner **完全同构**（相同地图/骰表/IO/AiModule 构造），
因此结果可与实验 CSV 逐字段核对；行动序列来自包装后的 AI 与 IO。

用法：
    python Project/tools/lead_replay.py verify            # 保真度自检（对 CSV）
    python Project/tools/lead_replay.py consistency       # 逐决策动作一致率（单体 vs 分治）
    python Project/tools/lead_replay.py visibility        # NORMAL vs HARD 可见性证据
"""
from __future__ import annotations

import csv
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Project" / "src"))
RES = ROOT / "Project" / "experiments" / "results"

from treasure_game.ai_module import AiModule  # noqa: E402
from treasure_game.human_policy import HumanPolicy  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import GameMode, GameState, Seat  # noqa: E402
from treasure_game.simulator import SeatPolicy, Simulator  # noqa: E402

MAX_TURNS = 500


def make_dice_table(dice_seed: int) -> tuple[int, ...]:
    rng = random.Random(dice_seed)
    return tuple(rng.randint(1, 6) for _ in range(MAX_TURNS * 2))


class RecordingIO:
    """只读视图的人工代理 + 动作记录（GameIO 协议）。"""

    def __init__(self, strategy: str, dice_seed: int) -> None:
        self.policy = HumanPolicy(strategy=strategy, rng=random.Random(dice_seed))
        self.actions: list[tuple[str, ...]] = []
        self._view = None

    def render(self, view, state_view, events) -> None:  # noqa: ANN001
        self._view = view

    def next_action(self, seat, dice_N, view, state_view):  # noqa: ANN001
        seq = self.policy.choose(view, seat, dice_N, state_view)
        self.actions.append(tuple(str(d) for d in seq))
        return tuple(seq)

    def log(self, message: str) -> None:
        return None


class RecordingAI(AiModule):
    """记录 AI 每次决策；其余行为与 AiModule 完全一致。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.opt_actions: list[tuple[str, ...]] = []
        self.greedy_actions: list[tuple[str, ...]] = []

    def get_opt_move(self, state, dice_N, seat=Seat.B):  # noqa: ANN001
        seq = super().get_opt_move(state, dice_N, seat)
        self.opt_actions.append(tuple(str(d) for d in seq))
        return seq

    def get_greedy_move(self, state, dice_N, seat=Seat.B):  # noqa: ANN001
        seq = super().get_greedy_move(state, dice_N, seat)
        self.greedy_actions.append(tuple(str(d) for d in seq))
        return seq


def build(map_seed: int, h: int, w: int, walls: int, treasures: int, traps: int):
    return MapModule.generate(h, w, walls, treasures, traps, random.Random(map_seed),
                              treasure_value=2, trap_value=-2)


def replay(map_seed: int, dice_seed: int, mode: str, *, partition_threshold: int,
           human_strategy: str = "GATHER", h: int = 12, w: int = 12,
           walls: int = 16, treasures: int = 8, traps: int = 6, depth: int = 3, k: int = 24):
    tm, pa, pb = build(map_seed, h, w, walls, treasures, traps)
    state = GameState(true_map=tm, pos_a=pa, pos_b=pb, score_a=0, score_b=0,
                      turn=Seat.A, mode=GameMode(mode), turn_index=0)
    io = RecordingIO(human_strategy, dice_seed)
    ai = RecordingAI(depth=depth, candidate_limit=k, partition_threshold=partition_threshold)
    sim = Simulator(initial_state=state, io=io, rng=random.Random(dice_seed),
                    policy_a=SeatPolicy.HUMAN,
                    policy_b=SeatPolicy.GREEDY if mode == "EASY" else SeatPolicy.OPT,
                    max_turns=MAX_TURNS, ai=ai,
                    dice_seed_table=make_dice_table(dice_seed))
    result = sim.run_human_vs_ai(GameMode(mode))
    ai_actions = ai.greedy_actions if mode == "EASY" else ai.opt_actions
    trap_pos = sorted({(e.position.row, e.position.col) for e in result.events
                       if e.kind == "TRAP" and e.actor is Seat.A})
    return {
        "result": result,
        "human_actions": io.actions,
        "ai_actions": ai_actions,
        "trap_a": trap_pos,
        "score_a": result.score_a,
        "score_b": result.score_b,
        "turns": result.turns,
    }


def load(name: str) -> list[dict]:
    with (RES / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def verify() -> None:
    print("=" * 88)
    print("保真度自检：Lead 重放 vs 实验 CSV（分数 + 回合数）")
    print("=" * 88)
    for name, thr in (("batch_main_seed42_maps30_dice3_12x12_w16t8p6.csv", 144),
                      ("batch_main_seed42_maps30_dice3_12x12_w16t8p6_thr9999.csv", 10 ** 9)):
        rows = load(name)
        ok = 0
        for r in rows[:6]:
            rep = replay(int(r["map_seed"]), int(r["dice_seed"]), r["mode"],
                         partition_threshold=thr)
            match = (rep["score_a"] == int(r["score_a"]) and rep["score_b"] == int(r["score_b"])
                     and rep["turns"] == int(r["turns"]))
            ok += match
            if not match:
                print(f"  MISMATCH {name} {r['mode']} seed=({r['map_seed']},{r['dice_seed']})"
                      f" 重放={rep['score_a']}:{rep['score_b']}/{rep['turns']}回"
                      f" CSV={r['score_a']}:{r['score_b']}/{r['turns']}回")
        print(f"  {name}: {ok}/6 一致")


def consistency(limit: int = 15) -> None:
    mono = {(r["map_seed"], r["dice_seed"], r["mode"]): r
            for r in load("batch_main_seed42_maps30_dice3_12x12_w16t8p6_thr9999.csv")}
    part = {(r["map_seed"], r["dice_seed"], r["mode"]): r
            for r in load("batch_main_seed42_maps30_dice3_12x12_w16t8p6.csv")}
    keys = sorted(set(mono) & set(part))[:limit]
    same_all = ai_same = ai_total = 0
    lcp_sum = 0.0
    first_div: Counter[int] = Counter()
    for k in keys:
        ms, ds, mode = int(k[0]), int(k[1]), k[2]
        a = replay(ms, ds, mode, partition_threshold=10 ** 9)
        b = replay(ms, ds, mode, partition_threshold=1)
        sa, sb = a["ai_actions"], b["ai_actions"]
        n = min(len(sa), len(sb))
        lcp = 0
        for i in range(n):
            if sa[i] == sb[i]:
                lcp += 1
            else:
                break
        ai_total += n
        ai_same += lcp
        lcp_sum += (lcp / n) if n else 1.0
        if lcp == n and len(sa) == len(sb):
            same_all += 1
        else:
            first_div[lcp] += 1
    print("=" * 88)
    print("逐决策动作一致率：单体 OPT(thr=9999) vs 分治近似(thr=1)，同图同骰同人类策略")
    print("=" * 88)
    print(f"  对局数                        : {len(keys)}")
    print(f"  整局决策完全一致的对局        : {same_all}/{len(keys)} = {same_all / len(keys):.1%}")
    print(f"  AI 决策总次数                 : {ai_total}")
    print(f"  AI 逐决策动作一致数           : {ai_same}/{ai_total} = {ai_same / max(1, ai_total):.1%}")
    print(f"  平均最长公共前缀占比          : {lcp_sum / len(keys):.3f}")
    print(f"  首次分歧步位置分布            : {dict(sorted(first_div.items()))}")


def visibility(strategy: str = "GATHER", limit: int = 30, csv_name: str | None = None) -> None:
    name = csv_name or "batch_main_seed42_maps30_dice3_12x12_w16t8p6.csv"
    rows = load(name)
    units = sorted({(int(r["map_seed"]), int(r["dice_seed"])) for r in rows})
    print("=" * 88)
    print(f"可见性机制证据（人类策略={strategy}）：同一图同一骰序 NORMAL vs HARD")
    print("=" * 88)
    diff_units = 0
    only_hard_total = 0
    shown = 0
    for (ms, ds) in units[:limit]:
        rn = replay(ms, ds, "NORMAL", partition_threshold=144, human_strategy=strategy)
        rh = replay(ms, ds, "HARD", partition_threshold=144, human_strategy=strategy)
        tn, th = set(rn["trap_a"]), set(rh["trap_a"])
        only_hard = sorted(th - tn)
        only_hard_total += len(only_hard)
        if (rn["score_a"], rn["score_b"], rn["turns"]) != (rh["score_a"], rh["score_b"], rh["turns"]):
            diff_units += 1
        if only_hard and shown < 3:
            shown += 1
            print(f"  · map_seed={ms} dice_seed={ds} 仅 HARD 踩中的陷阱={only_hard}")
            print(f"      NORMAL 终局 {rn['score_a']}:{rn['score_b']} / {rn['turns']} 回合，"
                  f"人类踩陷阱 {sorted(tn)}")
            print(f"      HARD   终局 {rh['score_a']}:{rh['score_b']} / {rh['turns']} 回合，"
                  f"人类踩陷阱 {sorted(th)}")
            print(f"      HARD 人类前 6 次动作={rh['human_actions'][:6]}")
    print(f"  比较单元数                    : {min(limit, len(units))}")
    print(f"  NORMAL/HARD 终局不同的单元数   : {diff_units}")
    print(f"  「仅 HARD 踩中」的陷阱格总数   : {only_hard_total}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if what == "verify":
        verify()
    elif what == "consistency":
        consistency(int(sys.argv[2]) if len(sys.argv) > 2 else 15)
    elif what == "visibility":
        visibility(sys.argv[2] if len(sys.argv) > 2 else "GATHER",
                   int(sys.argv[3]) if len(sys.argv) > 3 else 30,
                   sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        print(__doc__)
