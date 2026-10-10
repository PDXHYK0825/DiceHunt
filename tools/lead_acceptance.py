"""Lead 独立验收脚本（不依赖队友的测试文件）——对照 spec 第七节用例与核心不变量。"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from treasure_game.models import (  # noqa: E402
    Direction, GameMode, GameState, Position, Seat, TrueMap,
)
from treasure_game.path_module import PathModule  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.battle_module import BattleModule  # noqa: E402

OK = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (OK if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   -> {detail}" if detail else ""))


def make_map(rows: list[str], treasure: dict[Position, int] | None = None,
             trap: dict[Position, int] | None = None) -> TrueMap:
    terrain = tuple(tuple(1 if ch == "#" else 0 for ch in r) for r in rows)
    height, width = len(terrain), len(terrain[0])
    tv = dict(treasure or {})
    tp = dict(trap or {})
    import hashlib
    blob = repr((terrain, sorted(tv.items()), sorted(tp.items()))).encode()
    return TrueMap(
        height=height, width=width, terrain=terrain,
        treasure_values=tv, trap_values=tp,
        remaining_treasures=frozenset(tv), active_traps=frozenset(tp),
        revealed_traps=frozenset(), map_id=hashlib.sha1(blob).hexdigest()[:16],
    )


print("=== 用例 4：障碍物不消耗步数（骰 N=3，序列 右/上/左/下，第 1 条撞墙）===")
# 起点 (2,1)，墙 (2,2)(2,3)：RIGHT 撞墙(不耗步) -> UP -> LEFT -> DOWN，有效步 3
m = make_map([
    "....",
    "....",
    "..##",
    "....",
])
r = PathModule.sim_player_multi_step(m, Position(2, 1),
                                     (Direction.RIGHT, Direction.UP, Direction.LEFT, Direction.DOWN), 3)
check("valid_steps == 3", r.valid_steps == 3, f"实际 {r.valid_steps}")
check("attempted_steps == 4", r.attempted_steps == 4, f"实际 {r.attempted_steps}")
check("final_pos == (2,0)", r.final_pos == Position(2, 0), f"实际 {r.final_pos}")
check("score_delta == 0", r.score_delta == 0, f"实际 {r.score_delta}")
blocked = [e for e in r.events if e.kind == "BLOCKED"]
check("恰好 1 条 BLOCKED", len(blocked) == 1, f"实际 {len(blocked)}")
check("BLOCKED 文案逐字一致",
      bool(blocked) and blocked[0].message == "该方向为障碍物，无法通过，不消耗步数",
      blocked[0].message if blocked else "无事件")

print("=== 用例 5：对战边界（败方四周全障碍）===")
box = make_map([
    "###",
    "#.#",
    "###",
])
b = BattleModule.trigger_battle(box, Position(1, 1), Position(1, 1), 10, 5)
check("triggered", b.triggered is True)
check("A 10 -> 15", b.score_a == 15, f"实际 {b.score_a}")
check("B 5 -> 0", b.score_b == 0, f"实际 {b.score_b}")
check("败方原地(围死)", b.pos_b == Position(1, 1), f"实际 {b.pos_b}")
b3 = BattleModule.trigger_battle(box, Position(1, 1), Position(1, 1), 5, 5)
check("同分不结算不改分", b3.score_a == 5 and b3.score_b == 5)
b4 = BattleModule.trigger_battle(
    make_map(["...", "...", "..."]), Position(0, 0), Position(1, 1), 5, 5)
check("未重合 triggered=False 且全不变",
      b4.triggered is False and b4.score_a == 5 and b4.score_b == 5
      and b4.pos_a == Position(0, 0) and b4.pos_b == Position(1, 1))

print("=== 陷阱语义：效果一次性 + 永久暴露 ===")
tm = make_map(["...", "...", "..."], treasure={Position(0, 2): 6}, trap={Position(0, 1): -2})
res = PathModule.sim_player_multi_step(tm, Position(0, 0), (Direction.RIGHT,), 6)
check("踩陷阱扣分", res.score_delta == -2, f"实际 {res.score_delta}")
check("陷阱进入 revealed_traps", Position(0, 1) in res.true_map.revealed_traps)
check("陷阱移出 active_traps", Position(0, 1) not in res.true_map.active_traps)
check("原图不可变", Position(0, 1) in tm.active_traps and Position(0, 1) not in tm.revealed_traps)
back = PathModule.sim_player_multi_step(res.true_map, Position(0, 1), (Direction.LEFT, Direction.RIGHT), 6)
check("再踩已暴露陷阱不再扣分", back.score_delta == 0, f"实际 {back.score_delta}")

print("=== 用例 3：HARD 视图隐藏未触发陷阱 / 触发后暴露 ===")
v_hard = MapModule(tm).get_view_map(GameMode.HARD)
v_norm = MapModule(tm).get_view_map(GameMode.NORMAL)
check("HARD 未触发陷阱视图中为 0", v_hard.cell_value(0, 1) == 0, f"实际 {v_hard.cell_value(0,1)}")
check("NORMAL 一开始可见 -2", v_norm.cell_value(0, 1) == -2, f"实际 {v_norm.cell_value(0,1)}")
v_hard_after = MapModule(res.true_map).get_view_map(GameMode.HARD)
check("HARD 触发后暴露原始负值", v_hard_after.cell_value(0, 1) == -2, f"实际 {v_hard_after.cell_value(0,1)}")
check("HARD 触发后 spent_traps 标记", Position(0, 1) in v_hard_after.spent_traps)

print("=== 随机地图生成：连通 / 出生点不重合 / 可复现 ===")
g1 = MapModule.generate(12, 12, 24, 8, 6, random.Random(42))
g2 = MapModule.generate(12, 12, 24, 8, 6, random.Random(42))
check("同 seed 可复现（含出生点）", g1[0].map_id == g2[0].map_id and g1[1] == g2[1] and g1[2] == g2[2])
check("出生点不重合", g1[1] != g1[2])
dist = PathModule.bfs_distances(g1[0], g1[1])
nonwall = [Position(r, c) for r in range(g1[0].height) for c in range(g1[0].width)
           if g1[0].terrain[r][c] == 0]
check("所有非墙格与 A 连通", all(p in dist for p in nonwall),
      f"不可达 {len([p for p in nonwall if p not in dist])} 格")
check("宝藏全部可达", all(p in dist for p in g1[0].remaining_treasures))

print("=== 撞墙尝试为自由动作：撞墙不消耗步数且不影响后续有效位移 ===")
m2 = make_map(["....", ".#..", "...."])
# 两个地图仅墙位置不同，都远离实际路径；同一条序列必须得到完全一致的成功位移
seq = (Direction.RIGHT, Direction.DOWN, Direction.LEFT, Direction.UP, Direction.DOWN)
with_wall = PathModule.sim_player_multi_step(m2, Position(0, 0), seq, 3)
no_wall = PathModule.sim_player_multi_step(
    make_map(["....", "....", "...."]), Position(0, 0), seq, 3)
check("墙不影响成功位移（墙是自由动作）",
      with_wall.final_pos == no_wall.final_pos
      and with_wall.valid_steps == no_wall.valid_steps == 3,
      f"wall pos={with_wall.final_pos} steps={with_wall.valid_steps} | "
      f"nowall pos={no_wall.final_pos} steps={no_wall.valid_steps}")
check("墙只增加 blocked_steps", with_wall.blocked_steps >= 1 and no_wall.blocked_steps == 0,
      f"wall blocked={with_wall.blocked_steps} nowall blocked={no_wall.blocked_steps}")
# 达到 N 后追加指令无影响
after = PathModule.sim_player_multi_step(m2, Position(0, 0), seq + (Direction.RIGHT,) * 3, 3)
check("达到 N 后追加指令不改变结果",
      after.final_pos == with_wall.final_pos and after.valid_steps == with_wall.valid_steps)

print("=== 选做加分项核查（spec 第六节 ✨）===")
import random as _random  # noqa: E402
import pathlib as _pathlib  # noqa: E402

from treasure_game.ai_module import AiModule as _Ai  # noqa: E402
from treasure_game.simulator import SeatPolicy as _SP, Simulator as _Sim  # noqa: E402

_flat = make_map(["...", "...", "..."], treasure={Position(2, 2): 2})
_b = BattleModule.trigger_battle(_flat, Position(0, 0), Position(0, 0), 10, 5, win_score=9)
check("对战奖惩可配置（win_score=9 → +9/−9）",
      (_b.score_a, _b.score_b) == (19, -4), f"实际 {(_b.score_a, _b.score_b)}")

_st = GameState(true_map=_flat, pos_a=Position(0, 0), pos_b=Position(0, 2),
                score_a=0, score_b=0, turn=Seat.A, mode=GameMode.NORMAL, turn_index=0)
_sim = _Sim(initial_state=_st, rng=_random.Random(4), policy_a=_SP.GREEDY,
            policy_b=_SP.OPT, max_turns=40, dice_min=2, dice_max=4)
_seq = list(_sim.run_machine_vs_machine().metrics["dice_sequence"])
check("骰子范围可配置（2-4 内取值）", bool(_seq) and all(2 <= v <= 4 for v in _seq),
      f"取值范围 {sorted(set(_seq))}")
try:
    _Sim(initial_state=_st, dice_min=6, dice_max=1)
    check("非法骰子范围被拒绝", False, "未抛 ValueError")
except ValueError:
    check("非法骰子范围被拒绝", True)
try:
    _Sim(initial_state=_st, battle_win_score=-1)
    check("非法奖惩分数被拒绝", False, "未抛 ValueError")
except ValueError:
    check("非法奖惩分数被拒绝", True)

_short, _long = _Ai(depth=1), _Ai(depth=3)
_short.get_opt_move(_st, 3, Seat.B)
_long.get_opt_move(_st, 3, Seat.B)
check("搜索深度可配置且节点数随深度增加",
      _long.get_diagnostics().nodes >= _short.get_diagnostics().nodes,
      f"depth1={_short.get_diagnostics().nodes} depth3={_long.get_diagnostics().nodes}")

_sim2 = _Sim(initial_state=_st, rng=_random.Random(4), policy_a=_SP.GREEDY,
             policy_b=_SP.OPT, max_turns=40)
_res2 = _sim2.run_machine_vs_machine()
check("对局结果含事件流（回放基础）",
      isinstance(_res2.events, tuple) and len(_res2.events) > 0,
      f"事件数 {len(_res2.events)}")
check("metrics 冻结键齐备（实验/回放依赖）",
      {"battles", "dice_sequence", "ai_calls", "cache_hits", "trap_steps_a",
       "trap_steps_b", "partition_used", "timed_out"} <= set(_res2.metrics),
      f"实际键数 {len(_res2.metrics)}")

# GUI（选做第 1 项）：延迟导入，缺失 PyQt6 时必须安全降级
try:
    from treasure_game import gui as _gui
    check("GUI 模块可导入且 is_available() 为真", bool(_gui.is_available()))

    # 用 AST 而非文本扫描判断"顶层是否 import PyQt6"（注释/文档串里的字样不算）
    import ast as _ast  # noqa: E402

    _gui_path = (_pathlib.Path(__file__).resolve().parents[1] / "src" / "treasure_game"
                 / "gui.py")
    _tree = _ast.parse(_gui_path.read_text(encoding="utf-8"))
    _top_imports: list[str] = []
    for _node in _tree.body:
        if isinstance(_node, _ast.Import):
            _top_imports += [_a.name for _a in _node.names]
        elif isinstance(_node, _ast.ImportFrom):
            _top_imports.append(_node.module or "")
    _pyqt_top = [m for m in _top_imports if m.startswith("PyQt")]
    check("gui.py 模块顶层不 import PyQt6（AST 检查，延迟加载）",
          not _pyqt_top, f"顶层出现 {_pyqt_top}")
    check("gui.py 顶层只依赖标准库与 treasure_game",
          all(m.split(".")[0] in {"__future__", "argparse", "collections", "dataclasses",
                                 "datetime", "enum", "importlib", "os", "pathlib", "random",
                                 "sys", "time", "typing", "treasure_game"}
              for m in _top_imports if m),
          f"顶层导入 {sorted(set(_top_imports))}")

    # 在"PyQt6 完全不可导入"的模拟环境下：模块本身 import 成功、is_available() 必须为 False。
    # 必须同时堵三条路：sys.modules 缓存、__import__、importlib.import_module（gui 用的是后者）。
    import builtins as _bi  # noqa: E402
    import importlib as _il  # noqa: E402

    _real_import = _bi.__import__
    _real_import_module = _il.import_module
    _saved_pyqt = {k: v for k, v in sys.modules.items() if k.split(".")[0] == "PyQt6"}

    def _block_pyqt(name, *a, **kw):
        if str(name).split(".")[0] == "PyQt6":
            raise ImportError("PyQt6 blocked by lead_acceptance (simulated)")
        return _real_import(name, *a, **kw)

    def _block_import_module(name, *a, **kw):
        if str(name).split(".")[0] == "PyQt6":
            raise ImportError("PyQt6 blocked by lead_acceptance (simulated)")
        return _real_import_module(name, *a, **kw)

    for _k in list(_saved_pyqt):
        del sys.modules[_k]
    _bi.__import__ = _block_pyqt
    _il.import_module = _block_import_module
    try:
        # 同时清掉 PyQt6 为可用时的缓存判定（若实现里有 functools.lru_cache，需一并清）
        _cache_clear = getattr(getattr(_gui, "is_available", None), "cache_clear", None)
        if callable(_cache_clear):
            _cache_clear()
        _unavailable = _gui.is_available() is False
        _main_code = _gui.main(["--smoke"])
    finally:
        _bi.__import__ = _real_import
        _il.import_module = _real_import_module
        sys.modules.update(_saved_pyqt)
    check("模拟 PyQt6 缺失时 is_available() 为 False", _unavailable)
    check("模拟 PyQt6 缺失时 main() 受控返回 2（不冒裸 traceback）", _main_code == 2,
          f"实际退出码 {_main_code}")
except ImportError as _exc:
    check("GUI 模块可导入（PyQt6 缺失时应降级）", False, str(_exc))

print(f"\n=== 结果：{len(OK)} PASS / {len(FAIL)} FAIL ===")
for name in FAIL:
    print("  FAILED:", name)
raise SystemExit(1 if FAIL else 0)
