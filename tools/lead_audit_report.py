"""Lead 终验：审计报告正文引用的每个数字是否与原始 CSV/JSON 一致。

只做"读取原始数据 + 断言"，不修改任何文件。任何不一致都会打印 FAIL 并以非 0 退出。
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "experiments" / "results"
ok: list[str] = []
bad: list[str] = []


def load(name: str) -> list[dict]:
    with (RES / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_json(name: str) -> dict:
    with (RES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def check(label: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(label)
    print(("  OK   " if cond else "  FAIL ") + label + (f"  <- {detail}" if detail else ""))


def close(a: float, b: float, tol: float = 0.02) -> bool:
    return abs(a - b) <= tol


def rows_of(rows, mode):
    return [r for r in rows if r["mode"] == mode]


def win_rate(rs) -> float:
    """严格口径：wins / (wins+losses+ties)，平局计 0，超时局剔除（与驱动一致）。"""
    norm = [r for r in rs if r["draw_timeout"] not in ("True", "true", "1")]
    return statistics.fmean(1.0 if r["winner"] == "B" else 0.0 for r in norm)


def win_rate_loose(rs) -> float:
    """宽松口径：平局计 0.5。"""
    norm = [r for r in rs if r["draw_timeout"] not in ("True", "true", "1")]
    return statistics.fmean(
        1.0 if r["winner"] == "B" else (0.0 if r["winner"] == "A" else 0.5) for r in norm
    )


print("=" * 96)
print("A) v1 主批次（GATHER，12×12）—— 报告 §7.1")
print("=" * 96)
v1 = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6.csv")
for mode, exp_win, exp_win_loose, exp_diff in (("EASY", 0.389, 0.461, -1.44),
                                               ("NORMAL", 0.600, 0.650, 15.07),
                                               ("HARD", 0.600, 0.650, 15.07)):
    rs = rows_of(v1, mode)
    w = win_rate(rs)
    wl = win_rate_loose(rs)
    d = statistics.fmean(int(r["score_b"]) - int(r["score_a"]) for r in rs)
    to = sum(1 for r in rs if r["draw_timeout"] in ("True", "true", "1"))
    check(f"{mode} 胜率(严格) = {exp_win}", close(w, exp_win, 0.002), f"实测 {w:.4f}")
    check(f"{mode} 胜率(宽松) = {exp_win_loose}", close(wl, exp_win_loose, 0.002), f"实测 {wl:.4f}")
    check(f"{mode} 分差 ≈ {exp_diff}", close(d, exp_diff, 0.05), f"实测 {d:.2f}")
    check(f"{mode} 超时=0", to == 0, f"实测 {to}")

print("\n" + "=" * 96)
print("B) v2 主批次（CAUTIOUS，12×12）—— 报告 §7.3")
print("=" * 96)
v2 = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6_cautious.csv")
for mode, exp_win, exp_trap in (("EASY", 0.222, 0.00), ("NORMAL", 0.483, 0.00), ("HARD", 0.589, 1.01)):
    rs = rows_of(v2, mode)
    w = win_rate(rs)
    ta = statistics.fmean(int(r["trap_steps_a"]) for r in rs)
    check(f"{mode} 胜率(严格) ≈ {exp_win}", close(w, exp_win, 0.003), f"实测 {w:.4f}")
    check(f"{mode} 人工踩陷阱 ≈ {exp_trap}", close(ta, exp_trap, 0.01), f"实测 {ta:.2f}")

# NORMAL vs HARD 单元分歧数 + 全部偏向 HARD
n_map = {(r["map_seed"], r["dice_seed"]): r for r in rows_of(v2, "NORMAL")}
h_map = {(r["map_seed"], r["dice_seed"]): r for r in rows_of(v2, "HARD")}
keys = sorted(set(n_map) & set(h_map))
diff = [k for k in keys
        if (n_map[k]["score_a"], n_map[k]["score_b"], n_map[k]["trap_steps_a"])
        != (h_map[k]["score_a"], h_map[k]["score_b"], h_map[k]["trap_steps_a"])]
more_hard = [k for k in diff if int(h_map[k]["trap_steps_a"]) > int(n_map[k]["trap_steps_a"])]
check("配对单元数=90", len(keys) == 90, f"实测 {len(keys)}")
check("NORMAL/HARD 分歧单元=50", len(diff) == 50, f"实测 {len(diff)}")
check("50 个分歧单元全部 HARD 踩陷阱更多", len(more_hard) == 50, f"实测 {len(more_hard)}")

print("\n" + "=" * 96)
print("C) 10×10 基线（CAUTIOUS）—— 报告 §7.3 复现段")
print("=" * 96)
b2 = load("batch_baseline_seed42_maps30_dice3_10x10_w16t8p6_cautious.csv")
for mode, exp_win, exp_trap, exp_to in (("EASY", 0.200, 0.00, 0),
                                        ("NORMAL", 0.301, 0.00, 7),
                                        ("HARD", 0.411, 1.00, 0)):
    rs = rows_of(b2, mode)
    w = win_rate(rs)
    ta = statistics.fmean(int(r["trap_steps_a"]) for r in rs)
    to = sum(1 for r in rs if r["draw_timeout"] in ("True", "true", "1"))
    check(f"{mode} 胜率(严格) ≈ {exp_win}", close(w, exp_win, 0.003), f"实测 {w:.4f}")
    check(f"{mode} 人工踩陷阱 ≈ {exp_trap}", close(ta, exp_trap, 0.01), f"实测 {ta:.2f}")
    check(f"{mode} 超时 = {exp_to}", to == exp_to, f"实测 {to}")

print("\n" + "=" * 96)
print("D) 陷阱数量分层扫描 —— 报告 §7.5")
print("=" * 96)
for traps, exp_hard_trap, exp_robust in ((2, 0.30, 2.07), (6, 1.03, 4.47), (12, 2.13, 6.28)):
    rows = load(f"batch_main_seed42_maps10_dice3_12x12_w16t8p{traps}_cautious.csv")
    n = {(r["map_seed"], r["dice_seed"]): r for r in rows_of(rows, "NORMAL")}
    h = {(r["map_seed"], r["dice_seed"]): r for r in rows_of(rows, "HARD")}
    ks = sorted(set(n) & set(h))
    ta_n = statistics.fmean(int(n[k]["trap_steps_a"]) for k in ks)
    ta_h = statistics.fmean(int(h[k]["trap_steps_a"]) for k in ks)
    d = [(int(h[k]["score_b"]) - int(h[k]["score_a"])) - (int(n[k]["score_b"]) - int(n[k]["score_a"]))
         for k in ks]
    rob = [x for x in d if abs(x) < 100]
    check(f"traps={traps} NORMAL 踩陷阱=0.00", close(ta_n, 0.0, 0.001), f"实测 {ta_n:.2f}")
    check(f"traps={traps} HARD 踩陷阱 ≈ {exp_hard_trap}", close(ta_h, exp_hard_trap, 0.01),
          f"实测 {ta_h:.2f}")
    check(f"traps={traps} 稳健分差均值 ≈ {exp_robust}", close(statistics.fmean(rob), exp_robust, 0.05),
          f"实测 {statistics.fmean(rob):.2f} (n={len(rob)})")

print("\n" + "=" * 96)
print("E) 分治消融 + 异常案例 —— 报告 §7.4 / §7.7")
print("=" * 96)
mono = load("batch_main_seed42_maps30_dice3_12x12_w16t8p6_thr9999.csv")
part = v1
for mode in ("NORMAL", "HARD"):
    tm = statistics.median(int(r["ai_elapsed_ns"]) / 1e6 for r in rows_of(mono, mode))
    tp = statistics.median(int(r["ai_elapsed_ns"]) / 1e6 for r in rows_of(part, mode))
    check(f"{mode} 单体中位 ≈ 1158/1166 ms", 1100 < tm < 1220, f"实测 {tm:.1f}")
    check(f"{mode} 分治中位 ≈ 242/243 ms", 235 < tp < 250, f"实测 {tp:.1f}")
    check(f"{mode} 加速比 ≈ 4.8×", 4.7 < tm / tp < 4.9, f"实测 {tm / tp:.2f}×")

n_rows = rows_of(v2, "NORMAL")
bat = sorted(int(r["battles"]) for r in n_rows)
check("NORMAL 对战中位数 = 0", statistics.median(bat) == 0, f"实测 {statistics.median(bat)}")
check("NORMAL 对战 max = 736", bat[-1] == 736, f"实测 {bat[-1]}")
check("NORMAL 对战>20 的局数 = 2", sum(1 for b in bat if b > 20) == 2,
      f"实测 {sum(1 for b in bat if b > 20)}")

print("\n" + "=" * 96)
print("F) summary.json 自洽（checks 全绿 + 环境元数据）")
print("=" * 96)
for name in ("batch_main_seed42_maps30_dice3_12x12_w16t8p6.summary.json",
             "batch_main_seed42_maps30_dice3_12x12_w16t8p6_cautious.summary.json",
             "batch_baseline_seed42_maps30_dice3_10x10_w16t8p6_cautious.summary.json"):
    j = load_json(name)
    checks = j.get("checks", {})
    failed = {k: v for k, v in checks.items()
              if isinstance(v, dict) and v.get("failed", 0)}
    check(f"{name} 的 checks 无失败项", not failed, f"失败项 {failed}")
    check(f"{name} python=3.14.7", j["environment"]["python_version"] == "3.14.7",
          j["environment"]["python_version"])
    check(f"{name} 无第三方运行依赖",
          j["environment"]["runtime_dependencies"] == ["python-stdlib-only"],
          str(j["environment"]["runtime_dependencies"]))

print("\n" + "=" * 96)
print(f"审计结果：{len(ok)} OK / {len(bad)} FAIL")
print("=" * 96)
for label in bad:
    print("  FAILED:", label)
sys.exit(1 if bad else 0)
