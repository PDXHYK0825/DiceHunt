"""Lead 调查：v2 NORMAL+CAUTIOUS 的异常对战次数与超时。"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "experiments" / "results"
rows = list(csv.DictReader((RES / "batch_main_seed42_maps30_dice3_12x12_w16t8p6_cautious.csv")
                           .open(encoding="utf-8")))

for mode in ("EASY", "NORMAL", "HARD"):
    rs = [r for r in rows if r["mode"] == mode]
    bat = [int(r["battles"]) for r in rs]
    turns = [int(r["turns"]) for r in rs]
    to = [r for r in rs if r["draw_timeout"] == "True"]
    print(f"{mode:<7} battles: max={max(bat):>4} mean={statistics.fmean(bat):>6.2f} "
          f"| turns: max={max(turns):>3} mean={statistics.fmean(turns):>5.2f} "
          f"| timeouts={len(to)} | battles>5 的局数={sum(1 for b in bat if b > 5)}")

print("\n最异常 3 局（NORMAL，按 battles 降序）：")
for r in sorted((r for r in rows if r["mode"] == "NORMAL"),
                key=lambda r: -int(r["battles"]))[:3]:
    print("  ", {k: r[k] for k in ("map_seed", "dice_seed", "score_a", "score_b", "winner",
                                   "turns", "battles", "trap_steps_a", "trap_steps_b",
                                   "draw_timeout")})

print("\n超时局（NORMAL）：")
for r in (r for r in rows if r["mode"] == "NORMAL" and r["draw_timeout"] == "True"):
    print("  ", {k: r[k] for k in ("map_seed", "dice_seed", "score_a", "score_b", "turns",
                                   "battles", "trap_steps_a", "trap_steps_b")})

print("\n对应对照：v1（GATHER）同模式的 battles 分布")
rows1 = list(csv.DictReader((RES / "batch_main_seed42_maps30_dice3_12x12_w16t8p6.csv")
                            .open(encoding="utf-8")))
for mode in ("EASY", "NORMAL", "HARD"):
    bat = [int(r["battles"]) for r in rows1 if r["mode"] == mode]
    print(f"  {mode:<7} max={max(bat):>3} mean={statistics.fmean(bat):>5.2f} "
          f"| timeouts={sum(1 for r in rows1 if r['mode'] == mode and r['draw_timeout'] == 'True')}")
