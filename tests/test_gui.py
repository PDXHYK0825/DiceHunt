#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""W5-01 GUI 测试（Owner: gui-dev；spec 第三节 B 选做加分项）。

覆盖范围（与验收要求逐条对应）
------------------------------
1. **延迟导入**：源码级断言"模块顶层不 import PyQt6"；子进程里屏蔽 PyQt6 后
   ``import treasure_game`` / ``import treasure_game.gui`` 仍成功、``is_available()`` 为
   ``False``、``main`` 返回 2；控制台入口 ``python -m treasure_game`` 在无 PyQt6 环境照常跑。
2. **主窗口可构造**（offscreen）：模式下拉三项、输入区初始禁用、状态标签存在。
3. **地图编辑面板**：表单参数一键随机生成（格数/墙/宝/陷阱数量与参数一致）、点击按钮路径、
   单击格子循环编辑、出生点禁止改成墙、txt 保存/读取闭环。
4. **模式切换与 Simulator 路由**：EASY→贪心（``get_greedy_move``）、NORMAL/HARD→OPT
   （``get_opt_move``）；切到 HARD 后预览棋盘立即隐藏未触发陷阱。
5. **推进一回合后的标签**：比分标签、剩余有效步数标签、上一回合权威统计（按事件流计数）。
6. **报告导出**：临时目录落地、含模式/双方分数/回合数/指标/事件流。
7. **HARD 不泄露**：开局棋盘与视图完全一致且不含负值；踩中隐藏陷阱后弹窗 + 暴露负值。
8. **弹窗可测**：``_on_blocked`` / ``_on_trap`` / ``_on_battle`` 直接调用 → ``QMessageBox``
   被创建（``popups_enabled=True``）或只记录 ``Popup``（``popups_enabled=False``）。
9. **动画立即模式**：``animation_enabled=False`` 时同步完成；``True`` 时 ``QTimer`` 路径
   必须终止并落到终态。
10. **入口**：``main(['--smoke', ...])`` 返回 0 并写出报告；``--help``/参数错误路径。

无显示/无 PyQt6 环境
--------------------
* 文件顶部先设 ``QT_QPA_PLATFORM=offscreen``（在导入 PyQt6 之前）；
* 依赖 Qt 的用例一律用 ``@requires_qt`` 守卫，PyQt6 缺失时 **skip**（退出码仍为 0）；
* 纯逻辑层用例（``GuiIO`` / ``GameController`` / ``build_report`` / ``parse_action_text``）
  不依赖 Qt，照常运行；
* 设 ``TREASURE_GUI_TEST_FORCE_NO_QT=1`` 可在本机复现"没有 PyQt6"的路径：
  ``run_tests.py Project/tests/test_gui.py`` 应显示 skip 且退出码 0。
"""

from __future__ import annotations

import os
import sys

# 必须在导入 PyQt6 之前设置：无显示环境下用 offscreen 平台插件（整个 pytest 进程内生效）。
os.environ["QT_QPA_PLATFORM"] = "offscreen"


class _PyQt6Blocker:
    """meta_path 钩子：让 ``import PyQt6`` 失败（复现"未安装 PyQt6"的环境）。"""

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN201
        if fullname.split(".")[0] == "PyQt6":
            raise ImportError("PyQt6 被测试强制屏蔽（TREASURE_GUI_TEST_FORCE_NO_QT=1）")
        return None


if os.environ.get("TREASURE_GUI_TEST_FORCE_NO_QT") == "1":  # pragma: no cover - 环境开关
    sys.meta_path.insert(0, _PyQt6Blocker())

import pathlib  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

_HERE = pathlib.Path(__file__).resolve()
_PROJECT = _HERE.parents[1]
_ROOT = _PROJECT.parent
for _path in (str(_PROJECT), str(_PROJECT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import treasure_game.gui as gui  # noqa: E402
from treasure_game.models import (  # noqa: E402
    BattleResult,
    GameEvent,
    GameIO,
    GameMode,
    Position,
    PublicStateView,
    SearchDiagnostics,
    Seat,
    TrueMap,
)  # noqa: E402

HAVE_QT = gui.is_available()
requires_qt = pytest.mark.skipif(
    not HAVE_QT, reason="PyQt6 未安装：GUI 为选做加分项，跳过（控制台功能不受影响）"
)

#: 2×3 小地图：A(0,0) →(0,1) 宝藏 →(0,2)；B(1,0) →(1,2) 宝藏。
TINY_MAP = "2 3\n0 2 0\n0 0 2\nA 0 0\nB 1 0\n"
#: HARD 用小地图：(0,1) 是陷阱，(0,2) 是宝藏。
TRAP_MAP = "2 3\n0 -2 2\n0 0 0\nA 0 0\nB 1 0\n"


def _write_map(tmp_path: pathlib.Path, text: str, name: str = "map.txt") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _state_view(
    *,
    mode: GameMode = GameMode.NORMAL,
    pos_a: Position = Position(0, 0),
    pos_b: Position = Position(1, 0),
    score_a: int = 0,
    score_b: int = 0,
    turn: Seat = Seat.A,
    turn_index: int = 0,
    dice_N: int = 3,
    remaining: int = 2,
) -> PublicStateView:
    return PublicStateView(
        mode=mode,
        pos_a=pos_a,
        pos_b=pos_b,
        score_a=score_a,
        score_b=score_b,
        turn=turn,
        turn_index=turn_index,
        dice_N=dice_N,
        remaining_treasure_count=remaining,
    )


class _SpyAI:
    """记录 AI 入口被调用次数（验证 Simulator 的模式→AI 路由）。"""

    def __init__(self) -> None:
        self.greedy = 0
        self.opt = 0

    def get_greedy_move(self, state, dice_N, seat=Seat.B):  # noqa: ANN001, ANN201
        self.greedy += 1
        return ()

    def get_opt_move(self, state, dice_N, seat=Seat.B):  # noqa: ANN001, ANN201
        self.opt += 1
        return ()

    def get_diagnostics(self) -> SearchDiagnostics:
        return SearchDiagnostics(algorithm="greedy" if self.greedy else "opt")


class _Recorder:
    """把 ``GuiIO`` 的帧转发给窗口，同时记录**每一帧之后**的标签文本（"推进一回合"证据）。"""

    def __init__(self, window) -> None:  # noqa: ANN001
        self.window = window
        self.snapshots: list[dict[str, str]] = []

    def apply_frame(self, frame) -> None:  # noqa: ANN001
        self.window.apply_frame(frame)
        self.snapshots.append(
            {
                "turn": self.window.turn_label.text(),
                "score": self.window.score_label.text(),
                "steps": self.window.steps_label.text(),
                "last_turn": self.window.last_turn_label.text(),
                "treasure": self.window.treasure_label.text(),
            }
        )

    def append_log(self, message: str) -> None:
        self.window.append_log(message)

    def request_human_action(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise AssertionError("脚本模式下不应请求人工输入")


# --------------------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    if not HAVE_QT:
        pytest.skip("PyQt6 未安装：跳过所有 Qt 用例")
    return gui.create_app()


@pytest.fixture()
def make_window(qapp):
    """窗口工厂：默认关闭动画与弹窗（立即模式），用完统一关闭。"""
    created: list[object] = []

    def _make(**kwargs):  # noqa: ANN003, ANN202
        kwargs.setdefault("animation_enabled", False)
        kwargs.setdefault("popups_enabled", False)
        window = gui.MainWindow(**kwargs)
        created.append(window)
        return window

    yield _make
    for window in created:
        window.close()
        window.deleteLater()
    qapp.processEvents()


# --------------------------------------------------------------------------------------
# 1. 延迟导入与"无 PyQt6"契约
# --------------------------------------------------------------------------------------
def test_module_level_has_no_pyqt6_import() -> None:
    """源码级证据：模块顶层（列 0）不得出现 PyQt6 导入。"""
    source = pathlib.Path(gui.__file__).read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if line.startswith(("import PyQt6", "from PyQt6"))
    ]
    assert offenders == [], f"treasure_game.gui 顶层不得导入 PyQt6：{offenders}"
    # 延迟导入的封装必须存在
    assert callable(gui.qt_classes) and callable(gui.is_available)


def test_is_available_is_bool() -> None:
    assert isinstance(HAVE_QT, bool)


def test_import_and_console_survive_without_pyqt6_subprocess() -> None:
    """子进程屏蔽 PyQt6：包导入成功、GUI 自我降级（返回 2）、控制台入口不受影响。"""
    script = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'PyQt6':\n"
        "            raise ImportError('PyQt6 blocked by test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import treasure_game, treasure_game.gui as g\n"
        "print('import-ok', treasure_game.__version__, g.is_available())\n"
        "print('gui-main', g.main(['--smoke']))\n"
        "print('lazy-class', g.MainWindow if g.is_available() else 'unavailable')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_ROOT / ".pylibs"), str(_PROJECT / "src")])
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_ROOT),
        env=env,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "import-ok" in completed.stdout and "False" in completed.stdout
    assert "gui-main 2" in completed.stdout, completed.stdout
    assert "unavailable" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_is_available_false_when_pyqt6_blocked_inprocess(monkeypatch) -> None:
    """进程内 monkeypatch 掉 PyQt6：``is_available()`` → False，包导入与懒类缓存不受影响。"""
    import importlib

    cached = gui.qt_classes()["MainWindow"] if HAVE_QT else None  # 先填充懒加载缓存
    saved = {
        name: module for name, module in sys.modules.items() if name.split(".")[0] == "PyQt6"
    }
    for name in list(saved):
        sys.modules.pop(name, None)
    monkeypatch.setattr(sys, "meta_path", [_PyQt6Blocker(), *sys.meta_path])
    try:
        assert gui.is_available() is False
        assert importlib.import_module("treasure_game") is not None, "包导入必须照常成功"
        assert importlib.import_module("treasure_game.gui") is gui
        assert gui.main(["--smoke"]) == 2, "GUI 入口必须受控降级（返回 2）而非抛异常"
        if HAVE_QT:  # 已经缓存过的 Qt 类不受后续屏蔽影响
            assert gui.MainWindow is cached
    finally:
        sys.modules.update(saved)


def test_console_entry_runs_without_pyqt6_on_path() -> None:
    """控制台必做入口：``PYTHONPATH`` 只有 ``src``（无 .pylibs/PyQt6）时照常启动退出。"""
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
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "==== 寻宝博弈对战平台 ====" in completed.stdout
    assert "请选择功能：" in completed.stdout
    assert "Traceback" not in completed.stdout


# --------------------------------------------------------------------------------------
# 2. 纯逻辑层（无 Qt 也运行）：GuiIO / GameController / 报告 / 输入解析
# --------------------------------------------------------------------------------------
def test_guiio_satisfies_gameio_protocol() -> None:
    io = gui.GuiIO()
    assert isinstance(io, GameIO), "GuiIO 必须满足 GameIO 协议（06 §2）"
    for method in ("render", "next_action", "log"):
        assert callable(getattr(io, method))


def test_guiio_rejects_truth_objects() -> None:
    """视图-真值隔离：真值地图/非法状态对象在类型上就进不了渲染层。"""
    io = gui.GuiIO()
    truth = TrueMap.from_grid([[0, 2], [0, 0]])
    with pytest.raises(ValueError):
        io.render(truth, _state_view())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        io.render(  # type: ignore[arg-type]
            gui.MapModule(truth).get_view_map(GameMode.NORMAL), object()
        )
    with pytest.raises(ValueError):
        io.next_action(Seat.A, 3, None, object())  # type: ignore[arg-type]
    for bad in (0, 7, "3", None):
        with pytest.raises(ValueError):
            io.next_action(Seat.A, bad, None, _state_view())  # type: ignore[arg-type]


def test_guiio_log_records_transcript() -> None:
    io = gui.GuiIO()
    io.log("该方向为障碍物，无法通过，不消耗步数")
    assert io.transcript[-1] == "该方向为障碍物，无法通过，不消耗步数"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("d a w s", ("d", "a", "w", "s")),
        ("D W END", ("d", "w")),
        ("end", ()),
        ("d d end a w", ("d", "d")),
        ("  s  ", ("s",)),
    ],
)
def test_parse_action_text_ok(text: str, expected: tuple[str, ...]) -> None:
    steps, error = gui.parse_action_text(text, 4)
    assert error is None
    assert tuple(str(step) for step in steps) == expected


@pytest.mark.parametrize("text", ["", "   ", "x", "1 2 3", "up", "d x d"])
def test_parse_action_text_errors(text: str) -> None:
    steps, error = gui.parse_action_text(text, 4)
    assert error is not None and "非法输入" in error
    assert steps == ()


def test_parse_action_text_bad_dice() -> None:
    for bad in (0, 7, True, "3"):
        with pytest.raises(ValueError):
            gui.parse_action_text("d", bad)  # type: ignore[arg-type]


def test_headless_controller_pipeline_and_report(tmp_path: pathlib.Path) -> None:
    """无 Qt 也能跑的 GUI 逻辑主链：读取地图 → 脚本人工 → Simulator → 报告导出。"""
    path = _write_map(tmp_path, TINY_MAP)
    controller = gui.GameController()
    assert "已读取地图" in controller.load_txt(path)
    assert controller.authoring_view().mode is GameMode.NORMAL
    io = gui.GuiIO(script=["d"], fallback=())
    result = controller.run_human_vs_ai(
        GameMode.EASY, io=io, max_turns=6, dice_seed_table=(2, 2)
    )
    assert result.mode is GameMode.EASY
    assert io.render_calls == len(io.frames) >= 3
    assert io.frames[0].state_view.dice_N == 2
    assert result.score_a == 2, "A 第一步向右吃到 (0,1) 的宝藏"
    report = controller.export_report(tmp_path / "r.txt")
    text = report.read_text(encoding="utf-8")
    assert "模式：简单模式（EASY）" in text
    assert f"比分：A（人类）= {result.score_a}" in text
    assert f"回合数：{result.turns}" in text
    assert "【对局日志（事件流）】" in text
    default = gui.default_report_path(result)
    assert default.parent == gui.REPORT_DIR and default.suffix == ".txt"


def test_report_requires_a_finished_game() -> None:
    controller = gui.GameController()
    with pytest.raises(gui.IllegalStateError):
        controller.build_report_text()
    with pytest.raises(gui.IllegalStateError):
        controller.export_report()


def test_editing_preserves_one_point_treasure_and_cycles_rare_values(tmp_path, monkeypatch) -> None:
    controller = gui.GameController()
    controller.load_txt(_write_map(tmp_path, "2 4\n0 +1 0 0\n0 0 0 0\nA 0 0\nB 1 3\n"))
    controller.set_cell(1, 1, 1)
    assert controller.true_map.treasure_values == {Position(0, 1): 1}
    assert controller.true_map.terrain[0][1] == 0
    assert controller.true_map.terrain[1][1] == 1

    values = iter((1, 15, 20))
    monkeypatch.setattr(gui.MapModule, "draw_treasure_value", lambda rng: next(values))
    for value in (1, 15, 20):
        controller.set_cell(0, 2, 1)
        assert f"+{value}" in controller.cycle_cell(0, 2)
        assert controller.true_map.treasure_values[Position(0, 2)] == value
        controller.cycle_cell(0, 2)
        assert Position(0, 2) not in controller.true_map.treasure_values
        assert controller.true_map.trap_values[Position(0, 2)] == -2


@requires_qt
def test_board_distinguishes_one_point_treasure_from_wall(make_window, tmp_path) -> None:
    window = make_window(mode=GameMode.NORMAL)
    window.on_load(_write_map(tmp_path, "2 4\n0 +1 1 15\n0 20 0 0\nA 0 0\nB 1 3\n"))
    assert window.board.cell_text(0, 1) == "+1"
    assert window.board.cell_text(0, 2) == "#"
    assert window.board.cell_text(0, 3) == "+15"
    assert window.board.cell_text(1, 1) == "+20"
    assert window.board.item(0, 1).background() != window.board.item(0, 2).background()
    window.board.set_tokens(Position(0, 1), Position(1, 3))
    window.board.set_tokens(Position(0, 0), Position(1, 3))
    assert window.board.cell_text(0, 1) == "+1"


# --------------------------------------------------------------------------------------
# 3. 主窗口可构造（offscreen）
# --------------------------------------------------------------------------------------
@requires_qt
def test_main_window_constructs_offscreen(make_window) -> None:
    window = make_window(mode=GameMode.NORMAL)
    assert window.mode_combo.count() == 3
    assert [window.mode_combo.itemData(i) for i in range(3)] == ["EASY", "NORMAL", "HARD"]
    assert window.current_mode() is GameMode.NORMAL
    assert window.board.rowCount() == 0, "未加载地图时棋盘为空"
    assert not window.input_edit.isEnabled(), "未轮到人工时输入区禁用"
    assert window.score_label.text().startswith("比分")
    assert "本回合剩余有效步数" in window.steps_label.text()
    assert window.export_button.isEnabled() and window.start_button.isEnabled()
    assert gui.MainWindow is gui.qt_classes()["MainWindow"]
    with pytest.raises(AttributeError):
        _ = gui.NotARealGuiAttribute


@requires_qt
def test_guiio_render_drives_window_frame(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.NORMAL)
    window.on_load(_write_map(tmp_path, TINY_MAP))
    view = window.controller.authoring_view()
    io = gui.GuiIO(sink=window)
    io.render(view, _state_view(score_a=3, score_b=-1, dice_N=5, turn_index=3), ())
    assert len(window.frames) == 1 and io.render_calls == 1
    assert "3" in window.score_label.text() and "-1" in window.score_label.text()
    assert "5 / 5" in window.steps_label.text()
    assert "第 4 回合" in window.turn_label.text()
    assert window.board.rowCount() == view.height and window.board.columnCount() == view.width


# --------------------------------------------------------------------------------------
# 4. 地图编辑面板：一键随机生成 / 点击编辑 / 读写闭环
# --------------------------------------------------------------------------------------
@requires_qt
def test_generate_from_form_matches_params(make_window) -> None:
    window = make_window()
    window.width_spin.setValue(6)
    window.height_spin.setValue(5)
    window.walls_spin.setValue(5)
    window.treasures_spin.setValue(2)
    window.traps_spin.setValue(1)
    message = window.on_generate(seed=42)
    assert "已随机生成" in message
    grid = window.controller.grid
    assert len(grid) == 5 and all(len(row) == 6 for row in grid), "格数必须与表单参数一致"
    flat = [value for row in grid for value in row]
    true_map = window.controller.true_map
    assert sum(row.count(1) for row in true_map.terrain) == 5
    assert len(true_map.treasure_values) == 2 and flat.count(-2) == 1
    assert set(true_map.treasure_values.values()) <= {*range(1, 11), 15, 20}
    # 棋盘控件尺寸与地图一致（5 行 × 6 列）
    assert (window.board.rowCount(), window.board.columnCount()) == (5, 6)
    assert window.controller.pos_a != window.controller.pos_b, "两出生点不得重合"


@requires_qt
def test_generate_button_click_path(make_window) -> None:
    """真实点击「一键随机生成地图」按钮（不带 seed，随机地图）。"""
    window = make_window()
    window.width_spin.setValue(4)
    window.height_spin.setValue(4)
    window.walls_spin.setValue(4)
    window.treasures_spin.setValue(1)
    window.traps_spin.setValue(1)
    window.generate_button.click()
    assert window.controller.loaded
    assert (window.board.rowCount(), window.board.columnCount()) == (4, 4)


@requires_qt
def test_cell_edit_cycles_and_birth_point_wall_rejected(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.EASY)
    window.on_load(_write_map(tmp_path, "3 3\n0 0 2\n0 0 0\n0 0 0\nA 0 0\nB 2 2\n"))
    assert window.controller.grid[1][1] == 0
    window._on_cell_clicked(1, 1)
    assert window.controller.grid[1][1] == 1 and window.board.cell_text(1, 1) == "#"
    window._on_cell_clicked(1, 1)
    value = window.controller.true_map.treasure_values[Position(1, 1)]
    assert value in {*range(1, 11), 15, 20}
    assert window.controller.grid[1][1] == value and window.board.cell_text(1, 1) == f"+{value}"
    window._on_cell_clicked(1, 1)
    assert window.controller.grid[1][1] == -2 and window.board.cell_text(1, 1) == "-2"
    window._on_cell_clicked(1, 1)
    assert window.controller.grid[1][1] == 0 and window.board.cell_text(1, 1) == gui.CELL_EMPTY
    # 出生点 (0,0) 不能改成墙：编辑被拒绝且状态栏给出原因
    window._on_cell_clicked(0, 0)
    assert window.controller.grid[0][0] == 0
    assert "编辑失败" in window.statusBar().currentMessage()


@requires_qt
def test_txt_save_and_reload_roundtrip(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.EASY)
    window.width_spin.setValue(5)
    window.height_spin.setValue(4)
    window.walls_spin.setValue(3)
    window.treasures_spin.setValue(2)
    window.traps_spin.setValue(1)
    window.on_generate(seed=3)
    target = tmp_path / "saved_map.txt"
    assert "地图已保存" in window.on_save(target)
    assert target.is_file()
    other = make_window(mode=GameMode.NORMAL)
    assert "已读取地图" in other.on_load(target)
    assert (other.board.rowCount(), other.board.columnCount()) == (4, 5)
    assert len(other.controller.true_map.treasure_values) == 2


# --------------------------------------------------------------------------------------
# 5. 模式切换与 Simulator 路由（EASY→贪心；NORMAL/HARD→OPT）
# --------------------------------------------------------------------------------------
@requires_qt
@pytest.mark.parametrize(
    "mode,expected",
    [(GameMode.EASY, "greedy"), (GameMode.NORMAL, "opt"), (GameMode.HARD, "opt")],
)
def test_mode_switch_routes_simulator_ai(
    make_window, tmp_path: pathlib.Path, mode: GameMode, expected: str
) -> None:
    window = make_window(mode=mode)
    window.set_mode(mode)
    assert window.current_mode() is mode
    assert gui.MODE_LABELS[mode] in window.mode_combo.currentText()
    window.on_load(_write_map(tmp_path, TINY_MAP))
    spy = _SpyAI()
    io = gui.GuiIO(sink=window, script=[], fallback=())  # 人工每回合 end
    result = window.controller.run_human_vs_ai(mode, io=io, ai=spy, max_turns=3)
    assert result.mode is mode
    if expected == "greedy":
        assert spy.greedy > 0 and spy.opt == 0, "EASY 必须走贪心 AI（Simulator 路由）"
    else:
        assert spy.opt > 0 and spy.greedy == 0, "NORMAL/HARD 必须走 OPT AI（Simulator 路由）"


@requires_qt
def test_switch_to_hard_hides_traps_in_preview(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.NORMAL)
    window.on_load(_write_map(tmp_path, TRAP_MAP))
    assert "-2" in window.board.board_text(), "NORMAL 完全信息：陷阱可见"
    window.set_mode(GameMode.HARD)
    assert window.current_mode() is GameMode.HARD
    assert "-2" not in window.board.board_text(), "HARD：未触发陷阱必须伪装为空地"


# --------------------------------------------------------------------------------------
# 6. 推进一回合：分数 / 剩余有效步数标签
# --------------------------------------------------------------------------------------
@requires_qt
def test_apply_frame_updates_labels(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.NORMAL)
    window.on_load(_write_map(tmp_path, TINY_MAP))
    view = window.controller.authoring_view()
    window.apply_frame(
        gui.Frame(
            index=0,
            view=view,
            state_view=_state_view(score_a=8, score_b=-4, dice_N=6, turn_index=4, turn=Seat.B),
            events=(),
        )
    )
    assert "A（人类）= 8" in window.score_label.text()
    assert "B（OPT 最优 AI）= -4" in window.score_label.text()
    assert "6 / 6" in window.steps_label.text(), "本回合剩余有效步数 = 骰子 N"
    assert "第 5 回合" in window.turn_label.text()
    assert "B（OPT 最优 AI）" in window.turn_label.text()
    assert window.last_turn_label.text() == "上一回合：（本局第一步）"


@requires_qt
def test_one_turn_updates_score_and_remaining_steps(make_window, tmp_path: pathlib.Path) -> None:
    """真实推进一回合（骰子固定 N=1）：A 吃到宝藏 +2，标签在**该回合结束后**同步更新。"""
    window = make_window(mode=GameMode.NORMAL)
    window.on_load(_write_map(tmp_path, TINY_MAP))
    recorder = _Recorder(window)
    io = gui.GuiIO(sink=recorder, script=[("d",)], fallback=(), seat_labels=window.seat_labels)
    result = window.controller.run_human_vs_ai(
        GameMode.NORMAL, io=io, max_turns=6, dice_seed_table=(1, 1)
    )
    assert result.score_a == 2, "A 第一步向右拾取 (0,1) 的宝藏"
    assert len(recorder.snapshots) == len(window.frames) >= 3

    # 第 1 帧（A 的回合）之后：比分仍为 0，剩余步数 = N
    assert "A（人类）= 0" in recorder.snapshots[0]["score"]
    assert "1 / 1" in recorder.snapshots[0]["steps"]
    # 第 2 帧（B 的回合）携带 A 的回合事件 → 比分与上一回合统计必须已更新
    after_a = recorder.snapshots[1]
    assert "A（人类）= 2" in after_a["score"], "A 拾取宝藏后比分标签必须更新"
    assert "1 / 1" in after_a["steps"], "新回合剩余有效步数 = 骰子 N"
    assert "A（人类） 有效移动 1/1" in after_a["last_turn"]
    assert "剩余有效步数 0" in after_a["last_turn"]
    assert "撞障碍物 0 次" in after_a["last_turn"]

    # 终局帧：标签与公开状态一致
    last = window.frames[-1].state_view
    assert f"A（人类）= {last.score_a}" in window.score_label.text()
    assert f"B（OPT 最优 AI）= {last.score_b}" in window.score_label.text()
    assert f"剩余宝藏：{last.remaining_treasure_count}" in window.treasure_label.text()
    assert f"{last.dice_N} / {last.dice_N}" in window.steps_label.text()
    assert window.dice_label.text() == f"骰子：{last.dice_N}"
    assert "-/-" not in window.steps_label.text(), "占位文案必须已被真实数据替换"


@requires_qt
def test_blocked_step_is_counted_without_consuming_steps(make_window, tmp_path: pathlib.Path) -> None:
    """撞墙不消耗步数：事件流统计里"撞障碍物"计数 +1、剩余步数不因撞墙减少。"""
    window = make_window(mode=GameMode.NORMAL)
    window.on_load(_write_map(tmp_path, "2 3\n0 0 2\n0 0 0\nA 0 0\nB 1 0\n"))
    recorder = _Recorder(window)
    # A 在 (0,0)：先向上撞出界（BLOCKED），再向右进入 (0,1)
    io = gui.GuiIO(sink=recorder, script=[("w", "d")], fallback=())
    window.controller.run_human_vs_ai(
        GameMode.NORMAL, io=io, max_turns=6, dice_seed_table=(2, 2)
    )
    blocked_popups = [popup for popup in window.popups if popup.kind == "BLOCKED"]
    assert blocked_popups, "撞障碍物必须触发弹窗记录"
    assert blocked_popups[0].text == "该方向为障碍物，无法通过，不消耗步数"
    after_a = recorder.snapshots[1]
    assert "有效移动 1/2" in after_a["last_turn"], "撞墙那次不计入有效移动"
    assert "剩余有效步数 1" in after_a["last_turn"], "撞墙不消耗有效步数"
    assert "撞障碍物 1 次" in after_a["last_turn"]


# --------------------------------------------------------------------------------------
# 7. 报告导出
# --------------------------------------------------------------------------------------
@requires_qt
def test_export_report_from_window(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.EASY)
    window.on_load(_write_map(tmp_path, TINY_MAP))
    io = gui.GuiIO(sink=window, script=[("d",)], fallback=())
    result = window.controller.run_human_vs_ai(
        GameMode.EASY, io=io, max_turns=6, dice_seed_table=(2, 2)
    )
    target = tmp_path / "report.txt"
    message = window.on_export_report(target)
    assert "对局报告已导出" in message
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "模式：简单模式（EASY）" in text
    assert f"比分：A（人类）= {result.score_a}" in text
    assert f"B（贪心 AI）= {result.score_b}" in text
    assert f"回合数：{result.turns}" in text
    assert "【对局结果】" in text and "【对局日志（事件流）】" in text
    assert "拾取宝藏 +2 分" in text, "报告必须含对局事件流"


@requires_qt
def test_export_report_without_game_is_graceful(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window()
    message = window.on_export_report(tmp_path / "none.txt")
    assert "还没有对局结果" in message
    assert not (tmp_path / "none.txt").exists()


# --------------------------------------------------------------------------------------
# 8. HARD 不泄露 + 陷阱暴露
# --------------------------------------------------------------------------------------
@requires_qt
def test_hard_board_matches_view_and_hides_traps(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.HARD)
    window.on_load(_write_map(tmp_path, TRAP_MAP))
    assert len(window.controller.true_map.trap_values) == 1, "真值里确实有陷阱"
    view = window.controller.authoring_view()
    assert view.mode is GameMode.HARD
    assert view.cell_value(0, 1) == 0, "HARD 未触发陷阱在视图里就是空地"
    assert "-2" not in window.board.board_text(), "开局棋盘不得出现陷阱负值"
    assert "-2" not in window.log_view.toPlainText()
    # 逐格与视图一致（玩家令牌格除外）
    tokens = (window.controller.pos_a, window.controller.pos_b)
    for row in range(view.height):
        for col in range(view.width):
            if Position(row, col) in tokens:
                continue
            assert window.board.cell_text(row, col) == gui.cell_text(view.cell_value(row, col))


@requires_qt
def test_hard_trap_reveal_end_to_end(make_window, tmp_path: pathlib.Path) -> None:
    """A 踩隐藏陷阱 → 弹窗说明触发玩家 → 陷阱暴露（棋盘出现负值）。"""
    window = make_window(mode=GameMode.HARD)
    window.on_load(_write_map(tmp_path, TRAP_MAP))
    assert "-2" not in window.board.board_text()
    io = gui.GuiIO(sink=window, script=[("d", "d")], fallback=())
    result = window.controller.run_human_vs_ai(
        GameMode.HARD, io=io, max_turns=3, dice_seed_table=(2, 1)
    )
    assert any(
        popup.text == "触发玩家：A（人类）\n【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】"
        for popup in window.popups
    ), "HARD 首次踩中隐藏陷阱必须有弹窗提示"
    assert result.score_a == 0, "-2（陷阱）+2（宝藏）= 0"
    assert "-2" in window.board.board_text(), "暴露后棋盘显示真实负值"


# --------------------------------------------------------------------------------------
# 9. 弹窗（可测形式）与动画立即模式
# --------------------------------------------------------------------------------------
@requires_qt
def test_interactive_human_input_via_nested_event_loop(make_window, tmp_path: pathlib.Path) -> None:
    """真实交互路径：``request_human_action`` 用嵌套事件循环阻塞，QTimer 模拟用户点击。"""
    from PyQt6.QtCore import QTimer

    window = make_window(mode=GameMode.EASY)
    window.on_load(_write_map(tmp_path, TINY_MAP))
    calls = {"n": 0}

    def submit() -> None:
        calls["n"] += 1
        if calls["n"] > 200:  # 兜底：任何异常都不至于让测试永久阻塞
            window.on_abort()
            return
        if not window.input_edit.isEnabled():
            return
        if calls["n"] == 1:
            window.input_edit.setText("d")
            window.on_submit_input()
        else:
            window.on_end_turn()

    timer = QTimer(window)
    timer.setInterval(5)
    timer.timeout.connect(submit)
    timer.start()
    io = gui.GuiIO(sink=window)  # 无 script → 走 request_human_action（嵌套事件循环）
    try:
        result = window.controller.run_human_vs_ai(
            GameMode.EASY, io=io, max_turns=4, dice_seed_table=(1, 1)
        )
    finally:
        timer.stop()
    assert calls["n"] >= 1
    assert len(io.requests) >= 1, "人工座位必须通过 request_human_action 取指令"
    assert result.score_a == 2, "第一次交互输入 'd' 必须真实生效"
    assert window._action_loop is None, "对局结束后不得残留事件循环"
    assert not window.input_edit.isEnabled()


@requires_qt
def test_full_game_with_popups_enabled(make_window, tmp_path: pathlib.Path) -> None:
    """整局开启弹窗：隐藏陷阱弹窗真实创建 QMessageBox（``show()`` 不阻塞对局）。"""
    window = make_window(mode=GameMode.HARD, popups_enabled=True)
    window.on_load(_write_map(tmp_path, TRAP_MAP))
    io = gui.GuiIO(sink=window, script=[("d", "d")], fallback=())
    result = window.controller.run_human_vs_ai(
        GameMode.HARD, io=io, max_turns=3, dice_seed_table=(2, 1)
    )
    trap_popups = [popup for popup in window.popups if popup.kind == "TRAP"]
    assert trap_popups and trap_popups[0].title == "踩到隐藏陷阱！"
    assert trap_popups[0].shown is True
    assert isinstance(window.last_message_box, gui.qt_classes()["QMessageBox"])
    assert result.score_a == 0 and "-2" in window.board.board_text()
    window.close()


@requires_qt
def test_popup_methods_create_message_box(qapp, make_window) -> None:
    window = make_window(mode=GameMode.NORMAL, popups_enabled=True)
    window._on_blocked(GameEvent.blocked(Seat.A, Position(0, 0)))
    assert window.popups[-1].kind == "BLOCKED" and window.popups[-1].shown
    assert window.popups[-1].text == "该方向为障碍物，无法通过，不消耗步数"
    box_class = gui.qt_classes()["QMessageBox"]
    assert isinstance(window.last_message_box, box_class), "撞墙必须真的弹出 QMessageBox"

    window._on_trap(GameEvent.trap(Seat.A, Position(0, 1), -2))
    assert window.popups[-1].kind == "TRAP" and window.popups[-1].title == "踩到陷阱"
    assert window.popups[-1].text == "触发玩家：A（人类）\n踩到陷阱 -2 分"
    assert window.last_message_box.text() == window.popups[-1].text

    window._on_trap(GameEvent.trap(Seat.B, Position(0, 1), -2, hidden=True))
    assert window.popups[-1].title == "踩到隐藏陷阱！"
    assert window.popups[-1].text == "触发玩家：B（OPT 最优 AI）\n【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】"
    assert window.last_message_box.text() == window.popups[-1].text

    window._on_battle(GameEvent.battle(Seat.A, Seat.B))
    assert window.popups[-1].kind == "BATTLE"
    assert window.popups[-1].text == "触发对战：A +5 分，B -5 分"
    assert isinstance(window.last_message_box, box_class)

    battle = BattleResult(
        triggered=True,
        pos_a=Position(0, 0),
        pos_b=Position(0, 0),
        score_a=7,
        score_b=-3,
        events=(),
    )
    window._on_battle(battle)
    assert "A 胜" in window.popups[-1].text
    with pytest.raises(ValueError):
        window._on_battle(object())  # type: ignore[arg-type]
    window.close()


@requires_qt
def test_popups_disabled_only_records(make_window) -> None:
    window = make_window(popups_enabled=False)
    window._on_blocked(GameEvent.blocked(Seat.A, Position(0, 0)))
    assert window.popups[-1].shown is False
    assert window.last_message_box is None


@requires_qt
def test_animation_immediate_mode_is_synchronous(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.NORMAL, animation_enabled=False)
    window.on_load(_write_map(tmp_path, TINY_MAP))
    view = window.controller.authoring_view()
    events = (
        GameEvent.move(Seat.A, Position(0, 1)),
        GameEvent.blocked(Seat.A, Position(0, 1)),
    )
    window.apply_frame(
        gui.Frame(
            index=0,
            view=view,
            state_view=_state_view(pos_a=Position(0, 1), dice_N=3),
            events=events,
        )
    )
    assert any(step.startswith("DICE:") for step in window.animation_steps)
    assert "MOVE:A:0,1" in window.animation_steps
    assert "BLOCK:A" in window.animation_steps
    assert window.animation_steps[-1] == "SETTLE"
    assert window.board.position_texts().get("A") == "0,1"
    assert window.dice_rolls[-1] == 3


@requires_qt
def test_animation_timer_path_terminates(make_window, tmp_path: pathlib.Path) -> None:
    """``animation_enabled=True`` 时走 QTimer + 嵌套事件循环，必须返回并落到终态。"""
    window = make_window(
        mode=GameMode.NORMAL, animation_enabled=True, animation_interval_ms=1
    )
    window.on_load(_write_map(tmp_path, TINY_MAP))
    view = window.controller.authoring_view()
    events = tuple(GameEvent.move(Seat.A, Position(0, col)) for col in (1, 2))
    window.apply_frame(
        gui.Frame(
            index=0,
            view=view,
            state_view=_state_view(pos_a=Position(0, 2), dice_N=2),
            events=events,
        )
    )
    assert window.board.position_texts().get("A") == "0,2"
    assert window.dice_rolls[-1] == 2
    assert window.animation_steps[-1] == "SETTLE"


# --------------------------------------------------------------------------------------
# 10. 入口 main / 人工输入回调
# --------------------------------------------------------------------------------------
@requires_qt
def test_main_smoke_entry_writes_report(qapp, tmp_path: pathlib.Path, capsys) -> None:
    report = tmp_path / "smoke.txt"
    code = gui.main(["--smoke", "--seed", "7", "--report", str(report)], app=qapp)
    assert code == 0
    out = capsys.readouterr().out
    assert "GUI 冒烟通过" in out
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "模式：" in text and "比分：A" in text and "回合数：" in text


def test_main_help_and_bad_args() -> None:
    assert gui.main(["--help"]) == 0
    assert gui.main(["-h"]) == 0
    assert gui.main(["--nope"]) == 2
    assert gui.main(["--mode", "SUPER"]) == 2
    assert gui.main(["--seed", "abc"]) == 2
    assert gui.main(["--params", "8", "8"]) == 2


@requires_qt
def test_submit_and_end_turn_callbacks(make_window, tmp_path: pathlib.Path) -> None:
    window = make_window(mode=GameMode.NORMAL)
    window.on_load(_write_map(tmp_path, TINY_MAP))
    window._awaiting_dice = 3  # 模拟"正等待人工输入"
    window.input_edit.setText("d x")
    assert window.on_submit_input() is None
    assert "非法输入" in window.hint_label.text()
    window.input_edit.setText("d d end a w")
    assert window.on_submit_input() == (gui.Direction.RIGHT, gui.Direction.RIGHT)
    window.input_edit.setText("d d d d")
    assert window.on_submit_input() is not None
    assert "最多 3 次有效移动" in window.hint_label.text()
    assert window.on_end_turn() == ()
    window._awaiting_dice = None
    assert window.on_submit_input() is None
    assert "不在等待人工输入" in window.hint_label.text()
