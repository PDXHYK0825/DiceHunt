#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""W3-02 控制台测试（Owner: simulator-dev）。

覆盖范围
--------
* **主菜单逐字复刻**：直接读取 ``Project/docs/spec.md`` 第三节 A 的代码块，与 `MENU_TEXT`
  和实际输出逐行比对（不是实现自证）。
* ``main(argv) -> int`` 的可测形式：注入假 stdin/stdout，**不真正阻塞等待输入**；
  ``--help``/未知参数/``--choice``/``--mode``/``--seed``/``--log-dir``/``--no-log``。
* 非法输入重新提示、``end`` 截断生效、EOF 受控中止（不产生假胜负、不冒裸 traceback）。
* 地图工具 6/7 端到端（随机生成 + 保存、读取 txt），人人对战 & HARD 隐藏陷阱提示端到端。
* ``python -m treasure_game`` 子进程真实启动（验收命令等价物）。
"""

from __future__ import annotations

import builtins
import io as _io_module
import pathlib
import subprocess
import sys
import types

import pytest

_HERE = pathlib.Path(__file__).resolve()
_PROJECT = _HERE.parents[1]
for _path in (str(_PROJECT), str(_PROJECT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from treasure_game.console_io import MENU_ITEMS, MENU_TEXT, ConsoleIO  # noqa: E402
from treasure_game.models import Direction, GameMode, Position, PublicStateView, Seat, TrueMap  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.simulator import HumanAbortError  # noqa: E402

import treasure_game.__main__ as cli  # noqa: E402

SPEC_PATH = _PROJECT / "docs" / "spec.md"


def StringIO(initial: str = "") -> _io_module.StringIO:
    return _io_module.StringIO(initial)


def spec_menu_lines() -> list[str]:
    """从 ``docs/spec.md`` 抽出"主菜单："后的围栏代码块（逐字原文）。"""
    lines = SPEC_PATH.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip().startswith("主菜单"))
    fence = next(index for index in range(start, len(lines)) if lines[index].strip() == "```")
    end = next(
        index for index in range(fence + 1, len(lines)) if lines[index].strip() == "```"
    )
    return lines[fence + 1 : end]


def run_cli(args, stdin_text: str = ""):
    """执行 ``main`` 并返回 ``(exit_code, 输出文本)``。"""
    out = StringIO()
    code = cli.main(list(args), stdin=StringIO(stdin_text), stdout=out)
    return code, out.getvalue()


# --------------------------------------------------------------------------------------
# 1. 七项主菜单：逐字复刻 + 非法输入重提示
# --------------------------------------------------------------------------------------
def test_menu_text_matches_spec_verbatim_line_by_line() -> None:
    expected = spec_menu_lines()
    assert expected, "spec.md 里应当能抽到主菜单代码块"
    assert MENU_TEXT.splitlines() == expected, "主菜单必须与 spec 第三节 A 逐字一致"
    # 七项标题都在菜单文本里
    for index, title in MENU_ITEMS.items():
        assert title in MENU_TEXT, f"菜单缺少第 {index} 项"
    assert len(MENU_ITEMS) == 7
    assert MENU_TEXT.startswith("==== 寻宝博弈对战平台 ====")
    assert MENU_TEXT.rstrip().endswith("请选择功能：")


def test_menu_output_is_verbatim_and_invalid_input_reprompts() -> None:
    code, out = run_cli(["--no-log"], stdin_text="9\nx\n7\n\n0\n")
    assert code == 0
    lines = out.splitlines()
    # 菜单块整体出现在输出里，且与冻结文本一致
    assert "==== 寻宝博弈对战平台 ====" in lines
    assert "【人机对抗】" in lines and "【算法测试模式】" in lines and "【地图工具】" in lines
    menu_start = lines.index("==== 寻宝博弈对战平台 ====")
    assert lines[menu_start : menu_start + len(MENU_TEXT.splitlines())] == MENU_TEXT.splitlines()
    # 非法输入必须重新提示（9 与 x 都不是 1-7），且不崩溃
    assert out.count("非法输入：请输入 1-7 之间的数字") >= 2
    assert "Traceback" not in out
    # 第七项被选中（空路径 → 受控提示，不崩）
    assert "非法输入：路径不能为空" in out
    assert "已退出对战平台。" in out


def test_quit_and_eof_are_graceful() -> None:
    assert run_cli(["--no-log"], stdin_text="0\n")[0] == 0
    code, out = run_cli(["--no-log"], stdin_text="")
    assert code == 0
    assert "输入已结束（EOF），退出对战平台。" in out
    for token in ("q", "quit", "exit", "退出"):
        assert run_cli(["--no-log"], stdin_text=f"{token}\n")[0] == 0


def test_help_and_invalid_arguments() -> None:
    code, out = run_cli(["--help"])
    assert code == 0 and "用法：python -m treasure_game" in out
    assert run_cli(["-h"])[0] == 0
    code, out = run_cli(["--choice", "9"])
    assert code == 2 and "参数错误" in out
    assert run_cli(["--nope"])[0] == 2
    assert run_cli(["--seed", "abc"])[0] == 2
    assert run_cli(["--mode", "SUPER"])[0] == 2
    assert run_cli(["--choice"])[0] == 2


def test_main_defaults_to_sys_streams_when_not_injected(monkeypatch) -> None:
    """不注入流时走 ``sys.stdin/stdout``（monkeypatch 即可测，不阻塞真实输入）。"""
    out = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO("0\n"))
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["--no-log"]) == 0
    assert "==== 寻宝博弈对战平台 ====" in out.getvalue()


def _import_hook_raising_for_gui():
    """构造一个只在导入 ``treasure_game.gui`` 时抛 ImportError 的 ``__import__``。"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.endswith("gui") and ("treasure_game" in name or name == "gui"):
            raise ImportError("PyQt6 not installed (test injection)")
        return real_import(name, *args, **kwargs)

    return fake_import


def test_gui_flag_forwards_argv_and_falls_back_without_pyqt6(monkeypatch) -> None:
    """`--gui` 是选做加分项入口：必须把后续参数原样转交 GUI；PyQt6 缺失时受控回退。

    这条由 Lead 添加，覆盖 ``__main__.py`` 里 Lead 引入的 ``--gui`` 转发逻辑，
    防止"选做项把必做控制台入口带崩"。
    """
    captured: dict[str, object] = {}
    fake = types.ModuleType("treasure_game.gui")
    fake.is_available = lambda: True

    def _fake_main(argv=None) -> int:
        captured["argv"] = list(argv or [])
        return 0

    fake.main = _fake_main
    monkeypatch.setitem(sys.modules, "treasure_game.gui", fake)
    # ``from treasure_game import gui`` 会先走包属性：必须同时把包属性指向假模块，
    # 否则真实 GUI（已被其它用例导入并缓存）会被拉起，测试将失去意义。
    import treasure_game as _tg  # noqa: PLC0415

    monkeypatch.setattr(_tg, "gui", fake, raising=False)

    out = StringIO()
    code = cli.main(["--gui", "--smoke", "--mode", "HARD"], stdout=out)
    assert code == 0
    forwarded = captured["argv"]
    assert "--smoke" in forwarded                       # gui 之后的参数原样转交
    assert forwarded[forwarded.index("--mode") + 1] == "HARD"

    # --gui 之后的 GUI 专属参数不得让控制台解析失败（它们属于 GUI）
    captured.clear()
    assert cli.main(["--gui", "--no-animation", "--interval", "10"], stdout=StringIO()) == 0
    assert "--no-animation" in captured["argv"]

    # PyQt6 缺失：受控回退到提示 + 退出码 2，且不冒裸 traceback。
    # 注意 ``from treasure_game import gui`` 会先走 getattr(package, "gui")，
    # 所以除了堵住 import，还必须堵住包属性（否则会把真实 GUI 拉起来并阻塞）。
    out = StringIO()
    monkeypatch.delitem(sys.modules, "treasure_game.gui", raising=False)
    monkeypatch.delattr(sys.modules["treasure_game"], "gui", raising=False)

    def _no_gui_attr(name: str):
        if name == "gui":
            raise ImportError("PyQt6 not installed (test injection)")
        raise AttributeError(name)

    monkeypatch.setattr(sys.modules["treasure_game"], "__getattr__", _no_gui_attr, raising=False)
    monkeypatch.setattr(builtins, "__import__", _import_hook_raising_for_gui(), raising=True)
    code = cli.main(["--gui"], stdout=out)
    assert code == 2
    assert "图形界面" in out.getvalue()


# --------------------------------------------------------------------------------------
# 2. ConsoleIO：输入解析 / end 截断 / 非法重提示 / EOF 中止
# --------------------------------------------------------------------------------------
def _public_view(view, seat: Seat = Seat.A, dice: int = 3) -> PublicStateView:
    return PublicStateView(
        mode=GameMode.NORMAL, pos_a=Position(0, 0), pos_b=Position(2, 2), score_a=0, score_b=0,
        turn=seat, turn_index=0, dice_N=dice, remaining_treasure_count=1,
    )


def test_console_next_action_parses_wasd_and_truncates_at_end() -> None:
    view = MapModule(TrueMap.from_grid([[0, 0, 0]])).get_view_map(GameMode.NORMAL)
    io = ConsoleIO(stdin=StringIO("d a w s\n"), stdout=StringIO(), save_log=False)
    assert io.next_action(Seat.A, 4, view, _public_view(view)) == (
        Direction.RIGHT,
        Direction.LEFT,
        Direction.UP,
        Direction.DOWN,
    )
    # end 截断其后全部输入
    io = ConsoleIO(stdin=StringIO("d d end a w\n"), stdout=StringIO(), save_log=False)
    assert io.next_action(Seat.A, 4, view, _public_view(view)) == (
        Direction.RIGHT,
        Direction.RIGHT,
    )
    # 单独 end = 放弃本回合
    io = ConsoleIO(stdin=StringIO("end\n"), stdout=StringIO(), save_log=False)
    assert io.next_action(Seat.A, 4, view, _public_view(view)) == ()
    # 大小写不敏感
    io = ConsoleIO(stdin=StringIO("D W END\n"), stdout=StringIO(), save_log=False)
    assert io.next_action(Seat.A, 4, view, _public_view(view)) == (Direction.RIGHT, Direction.UP)


@pytest.mark.parametrize("bad_input", ["x\n", "\n", "1 2 3\n", "up\n", "d x d\n"])
def test_console_invalid_input_reprompts_until_valid(bad_input: str) -> None:
    view = MapModule(TrueMap.from_grid([[0, 0, 0]])).get_view_map(GameMode.NORMAL)
    out = StringIO()
    io = ConsoleIO(stdin=StringIO(f"{bad_input}d\n"), stdout=out, save_log=False)
    assert io.next_action(Seat.A, 1, view, _public_view(view)) == (Direction.RIGHT,)
    assert "非法输入" in out.getvalue()
    assert "请输入移动指令" in out.getvalue()


def test_console_eof_and_ctrl_c_are_controlled_abort() -> None:
    view = MapModule(TrueMap.from_grid([[0, 0, 0]])).get_view_map(GameMode.NORMAL)
    io = ConsoleIO(stdin=StringIO(""), stdout=StringIO(), save_log=False)
    with pytest.raises(HumanAbortError):
        io.next_action(Seat.A, 1, view, _public_view(view))

    class _CtrlC:
        def readline(self):  # noqa: ANN202
            raise KeyboardInterrupt

    io = ConsoleIO(stdin=_CtrlC(), stdout=StringIO(), save_log=False)
    with pytest.raises(HumanAbortError):
        io.next_action(Seat.A, 1, view, _public_view(view))


def test_console_bad_dice_is_rejected() -> None:
    view = MapModule(TrueMap.from_grid([[0, 0, 0]])).get_view_map(GameMode.NORMAL)
    io = ConsoleIO(stdin=StringIO("d\n"), stdout=StringIO(), save_log=False)
    for bad in (0, 7, "3", None):
        with pytest.raises(ValueError):
            io.next_action(Seat.A, bad, view, _public_view(view))


def test_console_board_distinguishes_one_point_treasure_from_wall() -> None:
    true_map = TrueMap.create(
        3, 4, ((0, 0, 1, 0), (0, 0, 0, 0), (0, 0, 0, 0)),
        {Position(0, 1): 1, Position(0, 3): 15, Position(1, 1): 20},
    )
    view = MapModule(true_map).get_view_map(GameMode.NORMAL)
    out = StringIO()
    ConsoleIO(stdout=out, save_log=False).render_board(view, _public_view(view))
    rows = out.getvalue().splitlines()
    assert rows[1].split() == ["0", "A", "1", "#", "15"]
    assert rows[2].split() == ["1", ".", "20", ".", "."]


def test_console_render_shows_board_and_keeps_hard_traps_hidden() -> None:
    true_map = TrueMap.from_grid([[0, 0, 2], [0, -2, 0], [0, 0, 0]])
    out = StringIO()
    io = ConsoleIO(stdin=StringIO(""), stdout=out, save_log=False)
    hard_view = MapModule(true_map).get_view_map(GameMode.HARD)
    io.render(hard_view, _public_view(hard_view))
    text = out.getvalue()
    assert "A" in text and "2" in text  # 玩家与宝藏可见
    assert "-2" not in text, "HARD 未触发陷阱不得出现在人工视图/图例里"
    assert "(1, 1)" not in text, "不得泄露陷阱坐标"
    assert "骰子最大有效步数 N = 3" in text
    assert io.render_calls == 1 and len(io.views) == 1

    out2 = StringIO()
    io2 = ConsoleIO(stdin=StringIO(""), stdout=out2, save_log=False)
    normal_view = MapModule(true_map).get_view_map(GameMode.NORMAL)
    io2.render(normal_view, _public_view(normal_view))
    assert "-2" in out2.getvalue(), "NORMAL 完全信息下陷阱可见"


def test_console_log_and_save_log_file(tmp_path: pathlib.Path) -> None:
    io = ConsoleIO(stdin=StringIO(), stdout=StringIO(), save_log=True, log_dir=tmp_path)
    io.log("该方向为障碍物，无法通过，不消耗步数")
    assert io.transcript[-1] == "该方向为障碍物，无法通过，不消耗步数"
    path = io.save_log()
    assert path is not None and path.parent == tmp_path
    assert "该方向为障碍物，无法通过，不消耗步数" in path.read_text(encoding="utf-8")

    # --no-log 等价物：允许保存接口存在，但不写盘
    off = ConsoleIO(stdin=StringIO(), stdout=StringIO(), save_log=False, log_dir=tmp_path)
    off.log("x")
    assert off.save_log() is None
    assert len(list(tmp_path.glob("*.txt"))) == 1


# --------------------------------------------------------------------------------------
# 3. 地图工具 6/7 与对局端到端（管道喂输入）
# --------------------------------------------------------------------------------------
def test_item6_generates_and_saves_map(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "generated"
    code, out = run_cli(
        ["--choice", "6", "--seed", "42", "--no-log"],
        stdin_text=f"6 6 4 2 1\n{target}\n0\n",
    )
    assert code == 0
    assert "随机生成地图" in out
    assert "地图已生成：6x6" in out
    saved = target.with_suffix(".txt")
    assert saved.is_file(), "应当按用户输入路径保存 txt 地图"
    lines = saved.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "6 6"
    assert lines[-2].startswith("A ") and lines[-1].startswith("B ")
    assert "地图已保存" in out
    # 保存后的地图可再次读取（读写闭环）
    true_map, pos_a, pos_b = MapModule.load_txt(saved)
    assert (true_map.height, true_map.width) == (6, 6)
    assert len(true_map.treasure_values) == 2 and len(true_map.trap_values) == 1
    assert pos_a != pos_b


def test_item6_invalid_parameters_cancel_without_crash() -> None:
    code, out = run_cli(["--choice", "6", "--seed", "1", "--no-log"], stdin_text="8 8\n0\n")
    assert code == 0
    assert "非法输入：需要 5 个整数" in out
    assert "Traceback" not in out


def test_item7_loads_txt_map_and_human_vs_human_plays_a_round(tmp_path: pathlib.Path) -> None:
    """人人对战一回合：A 走到宝藏 → 终局；end 生效；菜单与日志齐备。"""
    map_path = tmp_path / "tiny.txt"
    map_path.write_text("3 3\n0 2 0\n0 0 0\n0 0 0\nA 0 0\nB 2 2\n", encoding="utf-8")
    code, out = run_cli(
        ["--choice", "7", "--seed", "5", "--no-log"],
        stdin_text=f"{map_path}\n5\nd\n0\n",
    )
    assert code == 0
    assert "读取本地 txt 地图" in out
    assert f"地图已读取：{map_path}" in out
    assert "人人对战 MANUAL VS MANUAL" in out
    # 一回合：A 拾取最后一枚宝藏 → 终局
    assert "拾取宝藏 +2 分" in out
    assert "游戏结束：宝藏已全部拾取" in out
    assert "【对局结束】" in out and "比分 A=2  B=0" in out
    assert "【性能统计】" in out and "【对局统计】" in out
    assert "Traceback" not in out


def test_item5_hard_mode_prints_hidden_trap_hint_end_to_end(tmp_path: pathlib.Path) -> None:
    """HARD + 人人对战：踩中隐藏陷阱打印提示，暴露后视图显示负值。"""
    map_path = tmp_path / "hard.txt"
    map_path.write_text("2 3\n0 -2 2\n0 0 0\nA 0 0\nB 1 0\n", encoding="utf-8")
    code, out = run_cli(
        ["--choice", "7", "--mode", "HARD", "--seed", "7", "--no-log"],
        stdin_text=f"{map_path}\n5\nd\nend\nd\n0\n",
    )
    assert code == 0
    assert "【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】" in out
    assert "游戏结束：宝藏已全部拾取" in out
    assert "踩陷阱 A/B=1/0" in out
    assert "Traceback" not in out
    # 提示所在那一帧开始输出之前，人工界面不得出现任何负值/陷阱坐标
    # （临时目录路径可能自带 "-2"，先归一化；帧边界用渲染头 "---- 第" 定位）
    hint_at = out.index("【踩到隐藏陷阱！")
    before = out[
        out.index("===== 【算法测试模式】") : out.rindex("---- 第", 0, hint_at)
    ].replace(str(map_path), "<MAP>")
    assert "-2" not in before, "踩中前的人工界面泄露了陷阱负值"
    assert "移动到 (0, 1)" not in before, "踩中前泄露了陷阱坐标的移动日志"
    assert "比分：A = -2" not in before, "踩中前出现了异常扣分"


def test_human_vs_human_eof_mid_game_is_controlled(tmp_path: pathlib.Path) -> None:
    """对局中途 EOF：受控中止、回到主菜单、退出码 0，不产生假胜负。"""
    map_path = tmp_path / "eof.txt"
    map_path.write_text("3 3\n0 2 0\n0 0 0\n0 0 0\nA 0 0\nB 2 2\n", encoding="utf-8")
    code, out = run_cli(["--choice", "7", "--no-log"], stdin_text=f"{map_path}\n5\n")
    assert code == 0
    assert "已中止当前对局" in out
    assert "输入已结束（EOF），退出对战平台。" in out
    assert "Traceback" not in out
    assert "【对局结束】" not in out, "中止不得伪造对局结果"


# --------------------------------------------------------------------------------------
# 4. 三种人机模式 + 机机：真实 AI 端到端，并写对局日志
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("choice", ["1", "2", "3"])
def test_human_vs_ai_three_modes_end_to_end(choice: str, tmp_path: pathlib.Path) -> None:
    """人工每回合 end，机器（贪心/OPT）收集全部宝藏 → 正常终局 + 统计 + 日志落盘。"""
    if sys.modules.get("treasure_game.ai_module") is None:
        try:
            import treasure_game.ai_module  # noqa: F401
        except Exception:  # noqa: BLE001
            pytest.skip("ai_module 尚未落地")
    stdin_text = "end\n" * 60 + "0\n"
    code, out = run_cli(
        ["--choice", choice, "--seed", "11", "--log-dir", str(tmp_path)],
        stdin_text=stdin_text,
    )
    assert code == 0
    assert "【人机对抗】" in out
    assert "【对局结束】" in out
    assert "【性能统计】" in out and "DP 缓存命中" in out
    assert "Traceback" not in out
    logs = list(tmp_path.glob("game_*.txt"))
    assert len(logs) == 1, "对局文本日志必须落盘"
    log_text = logs[0].read_text(encoding="utf-8")
    assert "【对局结束】" in log_text and "拾取宝藏" in log_text


def test_machine_vs_machine_item4_runs_to_completion(tmp_path: pathlib.Path) -> None:
    try:
        import treasure_game.ai_module  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("ai_module 尚未落地")
    code, out = run_cli(["--choice", "4", "--seed", "11", "--no-log"], stdin_text="0\n")
    assert code == 0
    assert "机器对机器" in out
    assert "【对局结束】" in out
    assert "TriggerError" not in out and "Traceback" not in out
    # 机机对局不需要任何人工输入
    assert "请输入移动指令" not in out


def test_ai_module_missing_is_reported_without_crash(monkeypatch) -> None:
    """AI 模块不可用时给出受控提示并回到菜单（不允许裸 traceback）。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # noqa: ANN001
        if name == "treasure_game.ai_module":
            raise ImportError("simulated missing ai_module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    code, out = run_cli(["--choice", "1", "--seed", "11", "--no-log"], stdin_text="0\n")
    assert code == 0
    assert "操作失败：IllegalStateError" in out
    assert "Traceback" not in out


# --------------------------------------------------------------------------------------
# 5. 真实子进程：验收命令等价物（不依赖 pytest 注入）
# --------------------------------------------------------------------------------------
def test_python_dash_m_treasure_game_subprocess() -> None:
    """cwd=Project、PYTHONPATH=src 时 ``python -m treasure_game`` 必须能启动并正常退出。"""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_PROJECT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "treasure_game", "--no-log"],
        input="0\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_PROJECT),
        env=env,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "==== 寻宝博弈对战平台 ====" in completed.stdout
    assert "请选择功能：" in completed.stdout
    for title in MENU_ITEMS.values():
        assert title in completed.stdout
    assert "Traceback" not in completed.stdout
