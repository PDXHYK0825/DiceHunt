"""Lead 独立读取实验 CSV/JSON，输出报告所需的结论级统计（不依赖队友自述口径）。"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "experiments" / "results"


def load(name: str):
    with (RES / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def as_int(row, key):
    v = row.get(key, "")
    return int(float(v)) if v not in ("", "None") else None


def as_float(row, key):
    v = row.get(key, "")
    return float(v) if v not in ("", "None") else None


def wins_b(row) -> float:
    """AI(B) 视角的胜率贡献（**主口径：平局计 0**，与驱动 winner_rates_note 一致）。

    超时局不属于正常胜负，返回 NaN 由调用方剔除。
    """
    if row["draw_timeout"] in ("True", "true", "1"):
        return float("nan")
    w = row["winner"]
    return 1.0 if w == "B" else 0.0


def wins_b_loose(row) -> float:
    """宽松口径：平局计 0.5（仅供对照，报告表里与主口径并列给出）。"""
    if row["draw_timeout"] in ("True", "true", "1"):
        return float("nan")
    w = row["winner"]
    return 1.0 if w == "B" else (0.0 if w == "A" else 0.5)


def summarize(rows, label):
    print(f"\n### {label}（n={len(rows)} 局）")
    by_mode = defaultdict(list)
    for r in rows:
        by_mode[r["mode"]].append(r)
    print(f"{'模式':<8}{'局数':>5}{'胜率B严':>8}{'胜率B松':>8}{'平局率':>8}{'超时':>5}"
          f"{'得分差均值':>11}{'样本std':>9}{'中位耗时ms':>11}{'p95ms':>9}"
          f"{'踩陷阱A/B':>11}{'对战均值':>9}{'缓存命中率':>11}")
    for mode in ("EASY", "NORMAL", "HARD"):
        rs = by_mode.get(mode, [])
        if not rs:
            continue
        normal = [r for r in rs if r["draw_timeout"] not in ("True", "true", "1")]
        timeouts = len(rs) - len(normal)
        win = [w for w in (wins_b(r) for r in normal) if not math.isnan(w)]
        win_loose = [w for w in (wins_b_loose(r) for r in normal) if not math.isnan(w)]
        draw_rate = sum(1 for r in normal if r["winner"] not in ("A", "B")) / max(1, len(normal))
        diffs = [as_int(r, "score_b") - as_int(r, "score_a") for r in normal]
        std = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
        el = sorted(as_float(r, "ai_elapsed_ns") / 1e6 for r in rs
                    if as_float(r, "ai_elapsed_ns") is not None)
        med = statistics.median(el) if el else float("nan")
        p95 = el[min(len(el) - 1, int(0.95 * len(el)))] if el else float("nan")
        ta = statistics.fmean(as_int(r, "trap_steps_a") for r in rs)
        tb = statistics.fmean(as_int(r, "trap_steps_b") for r in rs)
        bat = statistics.fmean(as_int(r, "battles") for r in rs)
        hits = sum(as_int(r, "cache_hits") for r in rs)
        miss = sum(as_int(r, "cache_misses") for r in rs)
        rate = f"{hits / (hits + miss):.1%}" if hits + miss else "n/a"
        print(f"{mode:<8}{len(rs):>5}{statistics.fmean(win):>8.3f}{statistics.fmean(win_loose):>8.3f}"
              f"{draw_rate:>8.3f}"
              f"{timeouts:>5}{statistics.fmean(diffs):>11.2f}{std:>9.2f}"
              f"{med:>11.1f}{p95:>9.1f}{f'{ta:.2f}/{tb:.2f}':>11}"
              f"{bat:>9.2f}{rate:>11}")


def paired(rows, mode_a, mode_b, label):
    """按 (map_seed, dice_seed) 配对，比较两种模式的得分差与踩陷阱。"""
    a = {(r["map_seed"], r["dice_seed"]): r for r in rows if r["mode"] == mode_a}
    b = {(r["map_seed"], r["dice_seed"]): r for r in rows if r["mode"] == mode_b}
    keys = sorted(set(a) & set(b))
    d_diff, d_trap, d_battle = [], [], []
    for k in keys:
        ra, rb = a[k], b[k]
        da = as_int(ra, "score_b") - as_int(ra, "score_a")
        db = as_int(rb, "score_b") - as_int(rb, "score_a")
        d_diff.append(db - da)
        d_trap.append(as_int(rb, "trap_steps_b") - as_int(ra, "trap_steps_b"))
        d_battle.append(as_int(rb, "battles") - as_int(ra, "battles"))

    def boot(xs, n=4000, seed=17):
        rng = random.Random(seed)
        out = []
        for _ in range(n):
            s = [xs[rng.randrange(len(xs))] for _ in range(len(xs))]
            out.append(statistics.fmean(s))
        out.sort()
        return out[int(0.025 * n)], out[int(0.975 * n)]

    print(f"\n### 配对差值 {label}（{mode_b} − {mode_a}，n={len(keys)} 配对单元）")
    for name, xs in (("得分差(scoreB−scoreA)", d_diff), ("AI踩陷阱次数", d_trap),
                     ("对战次数", d_battle)):
        lo, hi = boot(xs)
        print(f"  {name:<24} 均值={statistics.fmean(xs):+7.3f} "
              f"95%CI=[{lo:+.3f}, {hi:+.3f}]  跨0={'是' if lo <= 0 <= hi else '否'}")


def partition_ablation():
    mono = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6_thr9999.csv")
    part = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6.csv")
    print("\n### 分治消融（同一 12×12 网格、同一 seed 清单，仅改 partition 阈值）")
    print(f"{'模式':<8}{'单体中位ms':>12}{'分治中位ms':>12}{'加速比':>9}"
          f"{'单体均分差':>12}{'分治均分差':>12}{'分治use率':>11}")
    for mode in ("NORMAL", "HARD"):
        m = [r for r in mono if r["mode"] == mode]
        p = [r for r in part if r["mode"] == mode]
        tm = statistics.median(as_float(r, "ai_elapsed_ns") / 1e6 for r in m)
        tp = statistics.median(as_float(r, "ai_elapsed_ns") / 1e6 for r in p)
        dm = statistics.fmean(as_int(r, "score_b") - as_int(r, "score_a") for r in m)
        dp_ = statistics.fmean(as_int(r, "score_b") - as_int(r, "score_a") for r in p)
        used = statistics.fmean(1.0 if r["partition_enabled"] in ("True", "true", "1") else 0.0
                                for r in p)
        print(f"{mode:<8}{tm:>12.1f}{tp:>12.1f}{tm / tp:>8.2f}x{dm:>12.2f}{dp_:>12.2f}{used:>11.2f}")


print("=" * 100)
print("实验数据独立复核（Lead 直接读 CSV，不采用队友汇总口径）")
print("=" * 100)
main30 = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6.csv")
base30 = load("batch_baseline_seed42_maps30_dice3_10x10_w16t8p6.csv")
summarize(main30, "主批次 12×12（分治开启）")
summarize(base30, "基线 10×10（单体 OPT）")
paired(main30, "EASY", "NORMAL", "隔离算法差异（贪心 vs OPT，同图同骰）")
paired(main30, "NORMAL", "HARD", "隔离可见性差异（陷阱完全可见 vs 人工隐藏）")
partition_ablation()
print("\n### 超时与异常总体")
for name, rows in (("main", main30), ("baseline", base30)):
    to = sum(1 for r in rows if r["draw_timeout"] in ("True", "true", "1"))
    print(f"  {name}: 局数={len(rows)} 超时={to} "
          f"winner分布={dict((w, sum(1 for r in rows if r['winner'] == w)) for w in {r['winner'] for r in rows})}")

# --------------------------------------------------------------------------- #
# v2：座位 A 换成 CAUTIOUS（会利用视图上可见陷阱信息）
# --------------------------------------------------------------------------- #
print()
print("=" * 100)
print("v2 对照：座位 A = CAUTIOUS（利用视图可见陷阱避让）；v1 为 GATHER（陷阱盲）")
print("=" * 100)
try:
    caut_main = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6_cautious.csv")
    caut_mono = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6_cautious_thr9999.csv")
except FileNotFoundError as exc:
    print(f"  [跳过] 缺少 v2 样本：{exc}")
else:
    summarize(caut_main, "主批次 12×12 + CAUTIOUS（分治开启）")
    paired(caut_main, "EASY", "NORMAL", "v2 隔离算法差异（CAUTIOUS）")
    paired(caut_main, "NORMAL", "HARD", "v2 隔离可见性差异（CAUTIOUS）")

    print("\n### 分治消融（CAUTIOUS，同 12×12 网格、同 seed，仅改阈值）")
    print(f"{'模式':<8}{'单体中位ms':>12}{'分治中位ms':>12}{'加速比':>9}"
          f"{'单体均分差':>12}{'分治均分差':>12}")
    for mode in ("NORMAL", "HARD"):
        m = [r for r in caut_mono if r["mode"] == mode]
        p = [r for r in caut_main if r["mode"] == mode]
        tm = statistics.median(as_float(r, "ai_elapsed_ns") / 1e6 for r in m)
        tp = statistics.median(as_float(r, "ai_elapsed_ns") / 1e6 for r in p)
        dm = statistics.fmean(as_int(r, "score_b") - as_int(r, "score_a") for r in m)
        dp_ = statistics.fmean(as_int(r, "score_b") - as_int(r, "score_a") for r in p)
        print(f"{mode:<8}{tm:>12.1f}{tp:>12.1f}{tm / tp:>8.2f}x{dm:>12.2f}{dp_:>12.2f}")

    print("\n### 可见性机制：同一配对单元 NORMAL vs HARD（CAUTIOUS）")
    n_map = {(r["map_seed"], r["dice_seed"]): r for r in caut_main if r["mode"] == "NORMAL"}
    h_map = {(r["map_seed"], r["dice_seed"]): r for r in caut_main if r["mode"] == "HARD"}
    same = diff = 0
    only_hard_rows = []
    for k in sorted(set(n_map) & set(h_map)):
        rn, rh = n_map[k], h_map[k]
        ta_n, ta_h = as_int(rn, "trap_steps_a"), as_int(rh, "trap_steps_a")
        if (rn["score_a"], rn["score_b"], ta_n) == (rh["score_a"], rh["score_b"], ta_h):
            same += 1
        else:
            diff += 1
            if ta_h > ta_n:
                only_hard_rows.append((k, ta_n, ta_h, rn, rh))
    print(f"  配对单元数={len(set(n_map) & set(h_map))}  终局+踩陷阱完全相同={same}  不同={diff}")
    print(f"  「HARD 踩陷阱多于 NORMAL」的单元数={len(only_hard_rows)}")
    for k, ta_n, ta_h, rn, rh in only_hard_rows[:5]:
        print(f"    · map_seed={k[0]} dice_seed={k[1]}: "
              f"NORMAL 踩陷阱A={ta_n} 比分 {rn['score_a']}:{rn['score_b']} | "
              f"HARD 踩陷阱A={ta_h} 比分 {rh['score_a']}:{rh['score_b']}")

    # ---- 鲁棒性检查：均值是否被少数病态对局主导 -------------------------------
    print("\n### 稳健性：CAUTIOUS 下 NORMAL 的极端对局（往复循环导致的对战堆积）")
    n_rows = [r for r in caut_main if r["mode"] == "NORMAL"]
    b = sorted(as_int(r, "battles") for r in n_rows)
    to = [r for r in n_rows if r["draw_timeout"] in ("True", "true", "1")]
    heavy = [r for r in n_rows if as_int(r, "battles") > 20]
    print(f"  对战次数：中位={statistics.median(b)} 均值={statistics.fmean(b):.2f} "
          f"p90={b[int(0.9 * len(b))]} max={b[-1]}")
    print(f"  对战>20 的局数={len(heavy)}（占比 {len(heavy) / len(n_rows):.1%}）  "
          f"超时局数={len(to)}（占比 {len(to) / len(n_rows):.1%}）")
    keep = [r for r in n_rows if r not in heavy and r not in to]
    if keep:
        kv = [as_int(r, "trap_steps_a") for r in keep]
        kd = [as_int(r, "score_b") - as_int(r, "score_a") for r in keep]
        print(f"  剔除 {len(heavy) + len(to)} 个病态局后（n={len(keep)}）："
              f"人类踩陷阱均值={statistics.fmean(kv):.3f}  "
              f"得分差均值={statistics.fmean(kd):.2f}±{statistics.stdev(kd):.2f}")
    print("  说明：这些对局中陷阱恰好挡住剩余宝藏，CAUTIOUS 拒绝踩陷阱而反复绕行，"
          "与 AI 在瓶颈格每回合相遇 → 对战分数线性堆积；规则本身正确（max_turns 正常截断）。")

print()
print("=" * 100)
print("陷阱数量分层扫描（traps = 2 / 6 / 12，各 10 图 × 3 骰序，CAUTIOUS）")
print("=" * 100)
for traps in (2, 6, 12):
    try:
        rows = load(f"batch_main_seed42_maps10_dice3_12x12_w16t8p{traps}_cautious.csv")
    except FileNotFoundError:
        print(f"  traps={traps:>2}: [跳过] 样本尚未生成")
        continue
    n_map = {(r["map_seed"], r["dice_seed"]): r for r in rows if r["mode"] == "NORMAL"}
    h_map = {(r["map_seed"], r["dice_seed"]): r for r in rows if r["mode"] == "HARD"}
    keys = sorted(set(n_map) & set(h_map))
    d_trap = [as_int(h_map[k], "trap_steps_a") - as_int(n_map[k], "trap_steps_a") for k in keys]
    d_diff = [(as_int(h_map[k], "score_b") - as_int(h_map[k], "score_a"))
              - (as_int(n_map[k], "score_b") - as_int(n_map[k], "score_a")) for k in keys]
    hits_h = statistics.fmean(as_int(h_map[k], "trap_steps_a") for k in keys)
    hits_n = statistics.fmean(as_int(n_map[k], "trap_steps_a") for k in keys)
    print(f"  traps={traps:>2}: 单元={len(keys)}  "
          f"人类踩陷阱 NORMAL={hits_n:.2f} HARD={hits_h:.2f} "
          f"差值均值={statistics.fmean(d_trap):+.2f}  "
          f"得分差变化均值={statistics.fmean(d_diff):+.2f}")
