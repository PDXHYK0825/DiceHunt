"""PyQt6 GUI 可视化版本（spec 第三节 B「加分项」；W5-01，Owner: gui-dev）。

运行方式
--------
仓库根目录执行（``.pylibs`` 内含 PyQt6 6.11）::

    $env:PYTHONPATH = "<repo>\\.pylibs;<repo>\\Project\\src"
    python -m treasure_game.gui                # 打开 GUI
    python -m treasure_game.gui --smoke        # 无显示冒烟（offscreen，不进入事件循环）

延迟导入约束（硬性）
--------------------
本模块**顶层不导入 PyQt6**：文件顶部只有标准库与 ``treasure_game.*``。所有 Qt 类都在
:func:`_build_qt_classes` 内部定义（首次访问时才导入 PyQt6）。因此在 PyQt6 缺失的环境里：

* ``import treasure_game`` / ``import treasure_game.gui`` 照常成功；
* ``python -m treasure_game``（控制台必做项）完全不受影响；
* :func:`is_available` 返回 ``False``，:func:`main` 返回退出码 2 并给出提示，而不是抛异常。

视图-真值隔离（06 §4.6）
------------------------
* :class:`GuiIO` 与 :class:`BoardWidget` / :class:`MainWindow` 是**表现层**：只接收
  ``ViewMap`` + ``PublicStateView`` + ``GameEvent``（``GuiIO.render`` 强制校验类型，
  真值地图对象在类型上就进不来），从不访问 ``TrueMap`` / ``GameState`` / ``trap_values`` /
  ``active_traps``，因此 HARD 模式未触发陷阱无法被泄露。
* :class:`GameController` 是**编排层**（等价 ``__main__._Session``）：唯一持有真值地图与
  ``GameState`` 的对象，负责调 ``MapModule`` 生成/读写地图、调 ``Simulator`` 推进对局。
  它交给界面的也只有 ``ViewMap``（``authoring_view``）。

复用而非重写
------------
地图生成 = ``MapModule.generate`` / ``MapModule.load_txt``；对局推进 = ``Simulator``
（``run_human_vs_ai`` / ``run_machine_vs_machine``）；AI = ``AiModule``。本文件不复制任何
移动语义、对战规则或 AI 逻辑；GUI 只把事件流"翻译"成动画与弹窗。

测试友好
--------
* ``animation_enabled=False`` → 立即模式：动画步骤同步执行，不进入任何事件循环；
* ``popups_enabled=False`` → 只记录 :class:`Popup`，不创建 ``QMessageBox``；
* ``GuiIO(script=[...], fallback=())`` → 脚本化人工座位，可在无显示环境同步跑完整局。
"""

from __future__ import annotations

import dataclasses
import datetime
import importlib
import os
import pathlib
import random
import sys
import typing

from treasure_game.map_module import MapModule
from treasure_game.models import (
    BattleResult,
    Direction,
    GameEvent,
    GameMode,
    GameResult,
    GameState,
    IllegalStateError,
    MapFormatError,
    Position,
    PublicStateView,
    Seat,
    TrueMap,
    ViewMap,
)
from treasure_game.simulator import HumanAbortError, SeatPolicy, Simulator

__all__ = [
    # 纯 Python（无 PyQt6 也可用）
    "GameController",
    "GuiIO",
    "GuiUnavailableError",
    "Frame",
    "Popup",
    "MODE_ORDER",
    "MODE_LABELS",
    "SEAT_LABELS_HUMAN_VS_AI",
    "DEFAULT_MAP_PARAMS",
    "SMOKE_MAP_PARAMS",
    "EDIT_CYCLE",
    "REPORT_DIR",
    "USAGE",
    "HIDDEN_TRAP_PREFIX",
    "is_available",
    "parse_action_text",
    "build_report",
    "create_app",
    "create_window",
    "qt_classes",
    "main",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 模式下拉框顺序（spec 第三节 B：简单/普通/困难）。
MODE_ORDER: tuple[GameMode, ...] = (GameMode.EASY, GameMode.NORMAL, GameMode.HARD)

MODE_LABELS: dict[GameMode, str] = {
    GameMode.EASY: "简单模式",
    GameMode.NORMAL: "普通模式",
    GameMode.HARD: "困难模式",
}

#: 机器座位显示名（EASY→贪心；NORMAL/HARD→OPT）。
MACHINE_LABELS: dict[GameMode, str] = {
    GameMode.EASY: "贪心 AI",
    GameMode.NORMAL: "OPT 最优 AI",
    GameMode.HARD: "OPT 最优 AI",
}

#: 人机对抗默认座位名（与 ``__main__`` 的措辞一致）。
SEAT_LABELS_HUMAN_VS_AI: dict[GameMode, dict[Seat, str]] = {
    mode: {Seat.A: "人类", Seat.B: MACHINE_LABELS[mode]} for mode in MODE_ORDER
}

#: 表单默认参数（高 宽 墙 宝藏 陷阱），与 ``__main__.DEFAULT_MAP_PARAMS`` 同口径。
DEFAULT_MAP_PARAMS: tuple[int, int, int, int, int] = (8, 8, 8, 3, 2)

#: 冒烟模式使用的小地图（1 枚宝藏 → 机器捡完即终局，几秒内可完成）。
SMOKE_MAP_PARAMS: tuple[int, int, int, int, int] = (4, 4, 3, 1, 1)

#: 地图编辑面板的类型循环：空地 → 墙 → 宝藏（2 为类型标记，分值随机）→ 陷阱 → 空地。
EDIT_CYCLE: tuple[int, ...] = (0, 1, 2, -2)

#: 交付目录：报告默认落点 ``Project/reports/``。
REPORT_DIR = pathlib.Path(__file__).resolve().parents[2] / "reports"

#: HARD 隐藏陷阱文案前缀（06 §5 冻结文案，Simulator 负责生成）。
HIDDEN_TRAP_PREFIX = "【踩到隐藏陷阱！扣除"

#: 单元格显示字符（只由 ``ViewMap.cell_value`` 决定）。
CELL_WALL = "#"
CELL_EMPTY = "·"

USAGE = """用法：python -m treasure_game.gui [选项]
  对战地图寻宝策略分析平台 —— PyQt6 图形界面（spec 第三节 B 加分项）

选项：
  -h, --help                 显示本帮助并退出
  --mode {EASY,NORMAL,HARD}  启动时的游戏模式（默认 NORMAL）
  --seed N                   启动时随机地图的种子（默认 20260928，便于复现）
  --params H W WALLS TREASURES TRAPS
                             启动时的地图表单参数（默认 8 8 8 3 2）
  --load PATH                启动即读取本地 txt 地图
  --no-animation             关闭动画（立即模式，适合录屏/远程桌面）
  --interval MS              动画步进间隔毫秒（默认 90）
  --report PATH              冒烟/自动模式的报告输出路径
  --smoke                    冒烟自检：offscreen 构造界面 → 随机地图 → 推进一回合 →
                             导出对局报告 → 退出（返回 0/1），不进入事件循环
  --show                     冒烟时也使用真实显示（默认 --smoke 走 offscreen）

界面功能：地图编辑面板（点击格子循环 空地/墙/宝藏/陷阱）、表单参数一键随机生成、
模式下拉（简单/普通/困难）、骰子投点与玩家逐格移动动画、撞墙/踩陷阱/对战胜负弹窗、
双方分数与剩余有效步数展示、导出对局报告。"""

_GUI_MISSING_TEXT = (
    "未检测到 PyQt6：GUI 是选做加分项，控制台版本不受影响。\n"
    "请把 PyQt6 加入 PYTHONPATH（本仓库：<repo>\\.pylibs），或用 python -m treasure_game 使用控制台版本。"
)


class GuiUnavailableError(IllegalStateError):
    """PyQt6 不可用（未安装/导入失败）时抛出；``main`` 会把它翻译成退出码 2。"""


# --------------------------------------------------------------------------- #
# 纯 Python 数据结构与工具（无 Qt 依赖，可独立测试）
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True, slots=True)
class Popup:
    """一次弹窗记录（动画弹窗的可测形式；``shown`` 表示是否真的创建了 ``QMessageBox``）。"""

    kind: str
    title: str
    text: str
    shown: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class Frame:
    """``GameIO.render`` 的一帧：脱敏视图 + 公开状态 + 该帧携带的事件（上一回合的）。"""

    index: int
    view: ViewMap
    state_view: PublicStateView
    events: tuple[GameEvent, ...] = ()


def is_available() -> bool:
    """PyQt6 是否可用（延迟导入：只在这里真正 import，模块顶层永不导入）。"""
    try:
        importlib.import_module("PyQt6.QtWidgets")
    except Exception:  # noqa: BLE001 - 任何导入失败（缺包/DLL/平台插件）都视为不可用
        return False
    return True


def is_hidden_trap(event: object) -> bool:
    """判断一条 ``TRAP`` 事件是否为 HARD 隐藏陷阱提示（文案由 ``Simulator`` 改写）。"""
    return (
        isinstance(event, GameEvent)
        and event.kind == GameEvent.TRAP
        and str(event.message).startswith(HIDDEN_TRAP_PREFIX)
    )


def cell_text(value: int, *, treasure: bool = False) -> str:
    """格值 → 单元格文本：``#`` 墙 / ``+v`` 宝藏 / ``-v`` 陷阱 / ``·`` 空地。"""
    if value == 1 and not treasure:
        return CELL_WALL
    if value == 0:
        return CELL_EMPTY
    if value > 0:
        return f"+{value}"
    return str(value)


def parse_action_text(
    text: str, dice_N: int | None = None
) -> tuple[tuple[Direction, ...], str | None]:
    """解析控制台风格指令串 → ``(方向序列, 错误信息)``（错误信息为 ``None`` 表示合法）。

    与 ``ConsoleIO.next_action`` 的输入语义**逐条一致**（这是 IO 层约定，不是游戏规则）：

    * ``w/a/s/d`` 空格分隔，大小写不敏感；
    * ``end`` 结束回合并**截断**其后全部输入；
    * 空输入 / 非法字符 → 返回错误信息，调用方应重新提示；
    * 步数多于 ``dice_N`` 不算错误（多出的步数由 ``Simulator`` 按规则忽略），
      调用方可据 ``len(steps) > dice_N`` 自行给出提示。
    """
    if not isinstance(text, str):
        raise ValueError(f"text 必须是 str，收到 {type(text).__name__}")
    if dice_N is not None and (
        isinstance(dice_N, bool) or not isinstance(dice_N, int) or not 1 <= dice_N <= 6
    ):
        raise ValueError(f"dice_N 必须在 1..6，收到 {dice_N!r}")
    stripped = text.strip()
    if not stripped:
        return (), "非法输入：内容为空。请输入 w/a/s/d（空格分隔）或 end 结束回合。"
    steps: list[Direction] = []
    for token in stripped.split():
        lowered = token.lower()
        if lowered == "end":
            break  # end 之后的所有输入被截断
        try:
            steps.append(Direction.from_char(lowered))
        except ValueError:
            return (
                (),
                f"非法输入：{token!r} 不是合法指令（只接受 w/a/s/d 与 end）。请重新输入。",
            )
    return tuple(steps), None


# --------------------------------------------------------------------------- #
# GameIO 适配器（对照 console_io.ConsoleIO 的结构；纯 Python，可无 Qt 测试）
# --------------------------------------------------------------------------- #
class GuiIO:
    """GUI 版 ``GameIO``：把模拟器的渲染/输入翻译成界面帧与人工指令。

    :param sink: 视图层对象（``MainWindow``）；需要 ``apply_frame(frame)`` 与可选的
        ``append_log(message)`` / ``request_human_action(seat, dice_N, view, state_view)``。
    :param script: 脚本化人工输入（列表，每项是方向序列或 ``"d d end"`` 字符串）；
        提供 ``script`` 时 ``next_action`` **立即返回**，测试/冒烟无需事件循环。
    :param fallback: ``script`` 用尽后的动作序列（默认 ``()`` = 放弃本回合）。
    :param seat_labels: 座位显示名（仅用于日志）。
    """

    def __init__(
        self,
        *,
        sink: object | None = None,
        script: typing.Sequence[typing.Any] | None = None,
        fallback: typing.Any = (),
        seat_labels: typing.Mapping[Seat, str] | None = None,
    ) -> None:
        self._sink = sink
        self._script: list[typing.Any] | None = None if script is None else list(script)
        self._fallback = fallback
        self._seat_labels: dict[Seat, str] = dict(seat_labels or {})
        #: 所有渲染帧（只含脱敏视图与公开状态）
        self.frames: list[Frame] = []
        #: 所有 ``next_action`` 请求（``seat, dice_N, view, state_view``）
        self.requests: list[tuple[Seat, int, ViewMap, PublicStateView]] = []
        #: 界面侧文本日志（每帧一行公开状态 + ``GameIO.log`` 消息；供报告导出）
        self.transcript: list[str] = []
        self.render_calls = 0
        self.aborted = False

    # ------------------------------------------------------------------ GameIO
    def render(
        self,
        view: ViewMap,
        state_view: PublicStateView,
        events: tuple[GameEvent, ...] = (),
    ) -> None:
        """渲染一帧。**类型守卫**：只接受 ``ViewMap``（真值地图/状态对象在类型上进不来）。"""
        if not isinstance(view, ViewMap):
            raise ValueError(
                f"render 只接受 ViewMap（视图-真值隔离，06 §4.6），收到 {type(view).__name__}"
            )
        if not isinstance(state_view, PublicStateView):
            raise ValueError(
                f"render 只接受 PublicStateView，收到 {type(state_view).__name__}"
            )
        self.render_calls += 1
        frame = Frame(
            index=len(self.frames),
            view=view,
            state_view=state_view,
            events=tuple(events),
        )
        self.frames.append(frame)
        # 界面侧日志（对照 ConsoleIO.render 的文本记录）：只记"每帧公开状态"这一行，
        # 事件本身由 GameResult.events 权威记录，避免报告里重复两遍。
        self.transcript.append(
            f"第 {state_view.turn_index + 1} 回合｜{state_view.turn.value}｜"
            f"骰子 N={state_view.dice_N}｜比分 A={state_view.score_a} B={state_view.score_b}｜"
            f"剩余宝藏={state_view.remaining_treasure_count}｜模式={state_view.mode.value}"
        )
        if self._sink is not None and hasattr(self._sink, "apply_frame"):
            self._sink.apply_frame(frame)  # type: ignore[attr-defined]

    def next_action(
        self,
        seat: Seat,
        dice_N: int,
        view: ViewMap,
        state_view: PublicStateView,
    ) -> tuple[Direction, ...]:
        """取人工动作：脚本模式立即返回；否则阻塞式交给视图层（``request_human_action``）。"""
        if not isinstance(state_view, PublicStateView):
            raise ValueError(
                f"state_view 必须是 PublicStateView（06 §2），收到 {type(state_view).__name__}"
            )
        if isinstance(dice_N, bool) or not isinstance(dice_N, int) or not 1 <= dice_N <= 6:
            raise ValueError(f"dice_N 必须在 1..6，收到 {dice_N!r}")
        # 视图已在 render 阶段展示；这里只记录请求（全部是脱敏视图 + 公开状态），
        # 真正的输入解析交给 parse_action_text / 视图层。
        self.requests.append((seat, dice_N, view, state_view))

        if self._script is not None:
            batch = self._script.pop(0) if self._script else self._fallback
            return self._normalize(batch, dice_N)

        request = getattr(self._sink, "request_human_action", None)
        if callable(request):
            raw = request(seat, dice_N, view, state_view)
            if raw is None:
                self.aborted = True
                raise HumanAbortError("人工输入已取消：本局不计胜负")
            return self._normalize(raw, dice_N)
        return self._normalize(self._fallback, dice_N)

    def log(self, message: str) -> None:
        """逐字记录消息（冻结文案不加工、不加前缀）。"""
        text = str(message)
        self.transcript.append(text)
        if self._sink is not None and hasattr(self._sink, "append_log"):
            self._sink.append_log(text)  # type: ignore[attr-defined]

    def set_seat_labels(self, labels: typing.Mapping[Seat, str]) -> None:
        """更新座位显示名（与 ``ConsoleIO.set_seat_labels`` 同口径）。"""
        self._seat_labels.update(labels)

    # ------------------------------------------------------------------ 工具
    def _normalize(self, raw: typing.Any, dice_N: int | None = None) -> tuple[Direction, ...]:
        """把脚本项规范化成 ``tuple[Direction, ...]``；接受字符串/``Direction`` 序列。"""
        if isinstance(raw, str):
            steps, error = parse_action_text(raw, dice_N)
            if error is not None:
                raise ValueError(f"GuiIO 脚本项非法：{error}")
            return steps
        if raw is None:
            return ()
        normalized: list[Direction] = []
        for index, item in enumerate(raw):
            if isinstance(item, Direction):
                normalized.append(item)
                continue
            if isinstance(item, str):
                normalized.append(Direction.from_char(item))
                continue
            raise ValueError(
                f"GuiIO 脚本项第 {index} 项非法：{item!r}（只接受 Direction 或方向字符）"
            )
        return tuple(normalized)

    @property
    def seat_labels(self) -> dict[Seat, str]:
        return dict(self._seat_labels)


# --------------------------------------------------------------------------- #
# 编排层：唯一持有真值（TrueMap / GameState）的对象
# --------------------------------------------------------------------------- #
class GameController:
    """GUI 编排层（等价 ``__main__._Session`` + 三个对局入口）。

    职责：地图生成/读写/编辑（持有真值 + 授权网格）、构造 ``GameState``、调 ``Simulator``、
    生成对局报告。对视图层只暴露 ``authoring_view()``（``ViewMap``）。
    """

    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        ai_factory: typing.Callable[[], object] | None = None,
        max_turns: int = 500,
    ) -> None:
        self.rng = rng if rng is not None else random.Random()
        #: 机器 AI 工厂（测试可注入 spy；``None`` → 延迟导入 ``AiModule``）
        self.ai_factory = ai_factory
        self.max_turns = max_turns
        self.mode = GameMode.NORMAL
        self.true_map: TrueMap | None = None
        self.pos_a: Position | None = None
        self.pos_b: Position | None = None
        self.source = ""
        self.last_result: GameResult | None = None
        self.last_io: GuiIO | None = None
        #: 授权用整数网格（仅供地图编辑面板；由真值地图的**完全信息**视图导出）
        self._grid: list[list[int]] | None = None

    # ------------------------------------------------------------------ 状态
    @property
    def loaded(self) -> bool:
        return self.true_map is not None

    def set_mode(self, mode: GameMode) -> None:
        if not isinstance(mode, GameMode):
            raise ValueError(f"mode 必须是 GameMode，收到 {type(mode).__name__}")
        self.mode = mode

    @property
    def grid(self) -> list[list[int]]:
        """授权网格副本（供测试与编辑面板读取；视图层不直接使用）。"""
        if self._grid is None:
            raise IllegalStateError("尚未加载地图")
        return [list(row) for row in self._grid]

    def require_map(self) -> tuple[TrueMap, Position, Position]:
        if not self.loaded:
            raise IllegalStateError("尚未加载地图：请先随机生成或读取 txt 地图")
        assert self.true_map is not None and self.pos_a is not None and self.pos_b is not None
        return self.true_map, self.pos_a, self.pos_b

    def describe(self) -> str:
        """一行地图摘要（状态栏/日志用）。"""
        true_map, pos_a, pos_b = self.require_map()
        return (
            f"地图 {true_map.height}×{true_map.width}｜墙={len(_wall_cells(true_map))} "
            f"宝藏={len(true_map.treasure_values)} 陷阱={len(true_map.trap_values)}｜"
            f"A={_fmt_pos(pos_a)} B={_fmt_pos(pos_b)}｜map_id={true_map.map_id}｜来源：{self.source}"
        )

    def authoring_view(self) -> ViewMap:
        """编辑/预览视图：按当前模式脱敏（HARD 未触发陷阱伪装为空地）。"""
        true_map, _, _ = self.require_map()
        return MapModule(true_map).get_view_map(self.mode)

    def initial_state(self, mode: GameMode | None = None) -> GameState:
        true_map, pos_a, pos_b = self.require_map()
        return GameState(
            true_map=true_map,
            pos_a=pos_a,
            pos_b=pos_b,
            score_a=0,
            score_b=0,
            turn=Seat.A,
            mode=self.mode if mode is None else mode,
            turn_index=0,
        )

    # ------------------------------------------------------------------ 地图
    def _adopt(
        self,
        true_map: TrueMap,
        pos_a: Position,
        pos_b: Position,
        *,
        source: str,
        grid: list[list[int]] | None = None,
    ) -> None:
        self.true_map = true_map
        self.pos_a = pos_a
        self.pos_b = pos_b
        self.source = source
        self.last_result = None
        if grid is None:
            # 授权网格来自 NORMAL（完全信息）视图：编辑面板需要完整布局，不是对局视图。
            view = MapModule(true_map).get_view_map(GameMode.NORMAL)
            grid = [list(row) for row in view.cells]
        self._grid = grid

    def generate_map(
        self,
        height: int,
        width: int,
        walls: int,
        treasures: int,
        traps: int,
        *,
        seed: int | None = None,
        treasure_value: int | None = None,
        trap_value: int = -2,
    ) -> str:
        """一键随机生成地图（复用 ``MapModule.generate``：参数校验 + BFS 连通校验）。"""
        rng = random.Random(seed) if seed is not None else self.rng
        true_map, pos_a, pos_b = MapModule.generate(
            height,
            width,
            walls,
            treasures,
            traps,
            rng,
            treasure_value=treasure_value,
            trap_value=trap_value,
        )
        label = f"随机生成 {height}x{width}（墙={walls} 宝藏={treasures} 陷阱={traps}）"
        self._adopt(true_map, pos_a, pos_b, source=label)
        return f"已随机生成：{self.describe()}"

    def load_txt(self, path: pathlib.Path | str) -> str:
        """读取本地 txt 地图（复用 ``MapModule.load_txt``）。"""
        target = pathlib.Path(path)
        true_map, pos_a, pos_b = MapModule.load_txt(target)
        self._adopt(true_map, pos_a, pos_b, source=str(target))
        return f"已读取地图：{self.describe()}"

    def save_txt(self, path: pathlib.Path | str) -> pathlib.Path:
        """保存当前地图为 txt（复用 ``MapModule.save_txt``）。"""
        true_map, pos_a, pos_b = self.require_map()
        target = pathlib.Path(path)
        if target.suffix.lower() != ".txt":
            target = target.with_suffix(".txt")
        MapModule.save_txt(target, true_map, pos_a, pos_b)
        return target

    def set_cell(self, row: int, col: int, value: int, *, treasure: bool = False) -> str:
        """编辑格子；``treasure=True`` 可设置 1 分宝藏。出生点不允许改成墙。"""
        if self._grid is None:
            raise IllegalStateError("尚未加载地图：请先生成或读取地图再编辑")
        if not (0 <= row < len(self._grid) and 0 <= col < len(self._grid[0])):
            raise ValueError(f"格子越界：({row}, {col})（地图 {len(self._grid)}×{len(self._grid[0])}）")
        if type(value) is not int:
            raise ValueError(f"格值必须是 int，收到 {type(value).__name__}")
        pos = Position(row, col)
        if value == 1 and not treasure and pos in (self.pos_a, self.pos_b):
            raise ValueError(f"出生点 ({row}, {col}) 不能改成障碍物")
        grid = self.grid
        grid[row][col] = value
        current_map, _, _ = self.require_map()
        terrain = [list(line) for line in current_map.terrain]
        treasures = dict(current_map.treasure_values)
        traps = dict(current_map.trap_values)
        treasures.pop(pos, None)
        traps.pop(pos, None)
        terrain[row][col] = 1 if value == 1 and not treasure else 0
        if treasure or value >= 2:
            treasures[pos] = value
        elif value < 0:
            traps[pos] = value
        true_map = TrueMap.create(
            current_map.height, current_map.width, terrain, treasures, traps
        )
        self._adopt(true_map, self.pos_a, self.pos_b, source=f"{self.source}（已编辑）", grid=grid)
        return f"已编辑 ({row}, {col}) → {cell_text(value, treasure=treasure)}"

    def cycle_cell(self, row: int, col: int) -> str:
        """单击循环：空地 → 墙 → 宝藏 → 陷阱 → 空地。"""
        if self._grid is None:
            raise IllegalStateError("尚未加载地图：请先生成或读取地图再编辑")
        current = self._grid[row][col]
        if Position(row, col) in self.true_map.treasure_values:
            return self.set_cell(row, col, -2)
        if current == 1:
            return self.set_cell(row, col, MapModule.draw_treasure_value(self.rng), treasure=True)
        if current in EDIT_CYCLE:
            nxt = EDIT_CYCLE[(EDIT_CYCLE.index(current) + 1) % len(EDIT_CYCLE)]
        else:  # 非标准陷阱分值（例如 -5）→ 回到空地
            nxt = 0
        return self.set_cell(row, col, nxt)

    # ------------------------------------------------------------------ 对局
    def _resolve_ai(self, ai: object | None) -> object:
        if ai is not None:
            return ai
        if self.ai_factory is not None:
            return self.ai_factory()
        try:
            from treasure_game.ai_module import AiModule
        except ImportError as exc:  # pragma: no cover - 取决于队友落地进度
            raise IllegalStateError(
                "AI 模块不可用：无法导入 treasure_game.ai_module.AiModule（人机/机机模式需要它）"
            ) from exc
        return AiModule()

    def make_io(self, **kwargs: typing.Any) -> GuiIO:
        return GuiIO(**kwargs)

    def run_human_vs_ai(
        self,
        mode: GameMode | None = None,
        *,
        io: GuiIO | None = None,
        ai: object | None = None,
        max_turns: int | None = None,
        dice_seed_table: typing.Sequence[int] | None = None,
        policy_b: SeatPolicy = SeatPolicy.OPT,
        seat_labels: typing.Mapping[Seat, str] | None = None,
    ) -> GameResult:
        """人机对抗：复用 ``Simulator.run_human_vs_ai``（模式→AI 路由由 Simulator 负责）。"""
        game_mode = self.mode if mode is None else mode
        self.set_mode(game_mode)
        labels = dict(seat_labels) if seat_labels else dict(SEAT_LABELS_HUMAN_VS_AI[game_mode])
        if io is None:
            io = self.make_io(seat_labels=labels)
        elif seat_labels:
            io.set_seat_labels(labels)
        simulator = Simulator(
            initial_state=self.initial_state(game_mode),
            io=io,
            rng=self.rng,
            policy_a=SeatPolicy.HUMAN,
            policy_b=policy_b,
            max_turns=self.max_turns if max_turns is None else max_turns,
            ai=self._resolve_ai(ai),
            dice_seed_table=dice_seed_table,
        )
        result = simulator.run_human_vs_ai(game_mode)
        self.last_result = result
        self.last_io = io
        return result

    def run_machine_vs_machine(
        self,
        *,
        io: GuiIO | None = None,
        ai: object | None = None,
        max_turns: int | None = None,
        dice_seed_table: typing.Sequence[int] | None = None,
    ) -> GameResult:
        """机器对机器演示（贪心 A VS OPT B）；复用 ``Simulator.run_machine_vs_machine``。"""
        if io is None:
            io = self.make_io(seat_labels={Seat.A: "贪心 AI", Seat.B: "OPT 最优 AI"})
        simulator = Simulator(
            initial_state=self.initial_state(),
            io=io,
            rng=self.rng,
            policy_a=SeatPolicy.GREEDY,
            policy_b=SeatPolicy.OPT,
            max_turns=self.max_turns if max_turns is None else max_turns,
            ai=self._resolve_ai(ai),
            dice_seed_table=dice_seed_table,
        )
        result = simulator.run_machine_vs_machine()
        self.last_result = result
        self.last_io = io
        return result

    # ------------------------------------------------------------------ 报告
    def build_report_text(
        self,
        result: GameResult | None = None,
        *,
        seat_labels: typing.Mapping[Seat, str] | None = None,
        generated_at: datetime.datetime | None = None,
    ) -> str:
        """生成对局报告文本（含双方分数/回合数/模式；纯函数 :func:`build_report` 的包装）。"""
        outcome = result if result is not None else self.last_result
        if outcome is None:
            raise IllegalStateError("还没有对局结果：请先运行一局再导出报告")
        true_map, _, _ = self.require_map()
        transcript = tuple(self.last_io.transcript) if self.last_io is not None else ()
        return build_report(
            result=outcome,
            seat_labels=dict(seat_labels) if seat_labels else dict(SEAT_LABELS_HUMAN_VS_AI[outcome.mode]),
            source=self.source,
            map_id=true_map.map_id,
            generated_at=generated_at,
            transcript=transcript,
        )

    def export_report(
        self,
        path: pathlib.Path | str | None = None,
        *,
        result: GameResult | None = None,
        seat_labels: typing.Mapping[Seat, str] | None = None,
        generated_at: datetime.datetime | None = None,
    ) -> pathlib.Path:
        """导出对局报告到磁盘；``path`` 为空时落到 ``Project/reports/``。"""
        outcome = result if result is not None else self.last_result
        text = self.build_report_text(
            outcome, seat_labels=seat_labels, generated_at=generated_at
        )
        target = pathlib.Path(path) if path is not None else default_report_path(outcome)
        if target.suffix.lower() not in (".txt", ".md", ".log"):
            target = target.with_suffix(".txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target


def default_report_path(result: GameResult | None) -> pathlib.Path:
    """``Project/reports/gui_report_<时间戳>_<模式>.txt``。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    mode = f"_{result.mode.value}" if result is not None else ""
    return REPORT_DIR / f"gui_report_{stamp}{mode}.txt"


def build_report(
    *,
    result: GameResult,
    seat_labels: typing.Mapping[Seat, str] | None = None,
    source: str = "",
    map_id: str = "",
    generated_at: datetime.datetime | None = None,
    transcript: typing.Sequence[str] = (),
) -> str:
    """对局报告文本（纯函数，可无 Qt 测试）。

    只读取 ``GameResult`` 的公开结果字段（模式/比分/回合数/metrics/events），
    ``map_id`` 由编排层显式传入——本函数不接触真值地图对象。
    """
    if not isinstance(result, GameResult):
        raise ValueError(f"result 必须是 GameResult，收到 {type(result).__name__}")
    labels = dict(seat_labels or {})
    name_a = labels.get(Seat.A, "A")
    name_b = labels.get(Seat.B, "B")
    moment = generated_at if generated_at is not None else datetime.datetime.now()
    metrics = result.metrics
    calls = int(metrics.get("ai_calls", 0) or 0)
    elapsed = int(metrics.get("ai_elapsed_ns", 0) or 0)
    hits = int(metrics.get("cache_hits", 0) or 0)
    misses = int(metrics.get("cache_misses", 0) or 0)
    average = f"{elapsed / calls / 1e6:.3f} ms" if calls else "n/a（无机器决策）"
    hit_rate = f"{hits / (hits + misses) * 100:.1f}%" if (hits + misses) else "n/a"
    winner = result.winner if result.winner is not None else "平局"
    if result.draw_timeout:
        winner += "（超时未分胜负）"

    lines: list[str] = []
    lines.append("===== 对战地图寻宝策略分析平台 · 对局报告（GUI）=====")
    lines.append(f"生成时间：{moment.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"模式：{MODE_LABELS[result.mode]}（{result.mode.value}）")
    lines.append(f"地图来源：{source or '（未记录）'}")
    lines.append(f"地图指纹：{map_id or '（未记录）'}")
    lines.append(f"座位：A = {name_a}｜B = {name_b}")
    lines.append("")
    lines.append("【对局结果】")
    lines.append(f"比分：A（{name_a}）= {result.score_a}    B（{name_b}）= {result.score_b}")
    lines.append(f"回合数：{result.turns}")
    lines.append(f"胜者：{winner}")
    lines.append("")
    lines.append("【对局统计】")
    lines.append(f"触发对战：{int(metrics.get('battles', 0) or 0)} 次")
    lines.append(
        f"拾取宝藏：A={int(metrics.get('treasure_steps_a', 0) or 0)} "
        f"B={int(metrics.get('treasure_steps_b', 0) or 0)}"
    )
    lines.append(
        f"踩中陷阱：A={int(metrics.get('trap_steps_a', 0) or 0)} "
        f"B={int(metrics.get('trap_steps_b', 0) or 0)}"
    )
    lines.append(
        f"机器决策：{calls} 次｜累计思考={elapsed / 1e6:.3f} ms｜平均={average}｜"
        f"DP 缓存命中/未命中={hits}/{misses}（命中率 {hit_rate}）｜"
        f"搜索节点={int(metrics.get('nodes', 0) or 0)}｜"
        f"分治近似={'是' if metrics.get('partition_used') else '否'}"
    )
    lines.append(f"骰序：{list(metrics.get('dice_sequence', []) or [])}")
    lines.append("")
    lines.append("【对局日志（事件流）】")
    if result.events:
        for index, event in enumerate(result.events, start=1):
            actor = event.actor.value if event.actor is not None else "-"
            lines.append(f"{index:04d} [{event.kind}][{actor}] {event.message}")
    else:
        lines.append("（无事件）")
    if transcript:
        lines.append("")
        lines.append("【界面日志（每帧公开状态快照）】")
        lines.extend(str(item) for item in transcript)
    lines.append("")
    lines.append("===== 报告结束 =====")
    return "\n".join(lines) + "\n"


def _wall_cells(true_map: TrueMap) -> frozenset[Position]:
    """真值图里的墙坐标集合（仅编排层/测试使用，视图层不可见）。"""
    return frozenset(
        Position(row, col)
        for row in range(true_map.height)
        for col in range(true_map.width)
        if true_map.terrain[row][col] == 1
    )


def _fmt_pos(pos: Position) -> str:
    return f"({pos.row}, {pos.col})"


# --------------------------------------------------------------------------- #
# Qt 层：类定义延迟到首次使用时（模块顶层不 import PyQt6）
# --------------------------------------------------------------------------- #
_QT_CLASSES: dict[str, type] | None = None
_LAZY_EXPORTS = ("BoardWidget", "MainWindow")
#: ``QApplication`` 的模块级引用（防止被 GC 回收导致 "Must construct a QApplication"）。
_APP: typing.Any = None


def qt_classes() -> dict[str, type]:
    """返回 GUI 类表（首次调用时导入 PyQt6 并定义类；失败抛 :class:`GuiUnavailableError`）。"""
    global _QT_CLASSES
    if _QT_CLASSES is None:
        try:
            _QT_CLASSES = _build_qt_classes()
        except GuiUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - 缺包/DLL/平台插件都统一降级
            raise GuiUnavailableError(f"PyQt6 导入失败：{type(exc).__name__}: {exc}") from exc
    return _QT_CLASSES


def __getattr__(name: str) -> typing.Any:
    """PEP 562：``gui.MainWindow`` / ``gui.BoardWidget`` 首次访问时才导入 PyQt6。"""
    if name in _LAZY_EXPORTS:
        return qt_classes()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _build_qt_classes() -> dict[str, type]:
    """导入 PyQt6 并定义 ``BoardWidget`` / ``MainWindow``（只在函数内 import，保证延迟）。"""
    try:
        from PyQt6.QtCore import QEventLoop, Qt, QTimer, pyqtSignal
        from PyQt6.QtGui import QColor, QFont
        from PyQt6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QCheckBox,
            QComboBox,
            QFileDialog,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QHeaderView,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QSpinBox,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except Exception as exc:  # noqa: BLE001 - 统一降级为"GUI 不可用"
        raise GuiUnavailableError(f"PyQt6 导入失败：{type(exc).__name__}: {exc}") from exc

    _COLOR_WALL = QColor("#c9ccd1")
    _COLOR_TREASURE = QColor("#ffe6a8")
    _COLOR_TRAP = QColor("#ffc9c9")
    _COLOR_EMPTY = QColor("#ffffff")
    _COLOR_A = QColor("#bcd8ff")
    _COLOR_B = QColor("#c8f0c8")
    _COLOR_BLOCKED = QColor("#ffd0d0")

    class BoardWidget(QTableWidget):
        """棋盘控件：**只**从 ``ViewMap`` 取格值（真值对象在类型上进不来）。

        单元格文本：``#`` 墙｜``+v`` 宝藏｜``-v`` 陷阱｜``·`` 空地｜``A``/``B``/``AB`` 玩家。
        """

        cell_clicked = pyqtSignal(int, int)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(0, 0, parent)
            self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.setShowGrid(True)
            self.horizontalHeader().setVisible(False)
            self.verticalHeader().setVisible(False)
            self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            self.horizontalHeader().setMinimumSectionSize(12)
            self.verticalHeader().setMinimumSectionSize(12)
            self.horizontalHeader().setDefaultSectionSize(36)
            self.verticalHeader().setDefaultSectionSize(30)
            self.setFont(QFont("Microsoft YaHei", 9))
            self._height = 0
            self._width = 0
            self._last_view: ViewMap | None = None
            self._texts: dict[tuple[int, int], str] = {}
            self.cellClicked.connect(self._on_cell_clicked_qt)

        # ---------------------------------------------------------- 数据接口
        def set_frame(
            self,
            view: ViewMap,
            pos_a: Position | None = None,
            pos_b: Position | None = None,
        ) -> None:
            """按 ``ViewMap`` 重画棋盘（``pos_a``/``pos_b`` 为令牌位置，可为 ``None``）。"""
            if not isinstance(view, ViewMap):
                raise ValueError(
                    f"棋盘只接受 ViewMap（视图-真值隔离），收到 {type(view).__name__}"
                )
            self._ensure_shape(view.height, view.width)
            for row in range(view.height):
                for col in range(view.width):
                    value = view.cell_value(row, col)
                    item = self.item(row, col)
                    if item is None:  # pragma: no cover - _ensure_shape 保证存在
                        item = QTableWidgetItem("")
                        self.setItem(row, col, item)
                    text = cell_text(value, treasure=view.is_treasure(row, col))
                    item.setText(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setBackground(
                        _COLOR_WALL
                        if view.is_wall(row, col)
                        else _COLOR_TREASURE
                        if value > 0
                        else _COLOR_TRAP
                        if value < 0
                        else _COLOR_EMPTY
                    )
                    item.setForeground(QColor("#000000"))
                    self._texts[(row, col)] = text
            self._paint_tokens(view, pos_a, pos_b)
            self._apply_cell_size()

        def set_tokens(
            self, pos_a: Position | None, pos_b: Position | None
        ) -> None:
            """只更新玩家令牌（动画用），不动静态格值。"""
            view = self._last_view
            if view is None:  # pragma: no cover - 未渲染时调用
                return
            self._paint_tokens(view, pos_a, pos_b)

        def cell_text(self, row: int, col: int) -> str:
            """当前显示文本（测试断言用；只反映 ``ViewMap`` + 令牌）。"""
            item = self.item(row, col)
            return "" if item is None else item.text()

        def board_text(self) -> str:
            """整块棋盘的文本快照（HARD 泄露断言用）。"""
            lines: list[str] = []
            for row in range(self.rowCount()):
                lines.append(" ".join(self.cell_text(row, col) for col in range(self.columnCount())))
            return "\n".join(lines)

        def position_texts(self) -> dict[str, str]:
            """``{"A": "r,c", "B": "r,c"}`` 形式的令牌位置（测试辅助）。"""
            found: dict[str, str] = {}
            for row in range(self.rowCount()):
                for col in range(self.columnCount()):
                    text = self.cell_text(row, col)
                    if text == "AB":
                        found["A"] = found["B"] = f"{row},{col}"
                    elif text == "A":
                        found["A"] = f"{row},{col}"
                    elif text == "B":
                        found["B"] = f"{row},{col}"
            return found

        def clear_board(self) -> None:
            self.setRowCount(0)
            self.setColumnCount(0)
            self._height = self._width = 0
            self._texts.clear()
            self._last_view = None

        # ---------------------------------------------------------- 内部
        def _ensure_shape(self, height: int, width: int) -> None:
            if (height, width) != (self._height, self._width):
                self.setRowCount(height)
                self.setColumnCount(width)
                self._texts.clear()
                for row in range(height):
                    for col in range(width):
                        self.setItem(row, col, QTableWidgetItem(""))
                self._height, self._width = height, width
                self._apply_cell_size()

        def _apply_cell_size(self) -> None:
            """小地图按面板尺寸放大单元格（上限 64px）；大地图固定 36/30px + 滚动条。"""
            if not (0 < self._height <= 14 and 0 < self._width <= 14):
                self.horizontalHeader().setDefaultSectionSize(36)
                self.verticalHeader().setDefaultSectionSize(30)
                return
            avail_w = self.viewport().width()
            avail_h = self.viewport().height()
            if avail_w <= 40 or avail_h <= 40:  # 布局尚未完成
                size = 36
            else:
                size = max(24, min(64, avail_w // self._width, avail_h // self._height))
            self.horizontalHeader().setDefaultSectionSize(size)
            self.verticalHeader().setDefaultSectionSize(size)

        def resizeEvent(self, event: typing.Any) -> None:  # noqa: N802 - Qt 命名
            super().resizeEvent(event)
            self._apply_cell_size()

        def _paint_tokens(
            self,
            view: ViewMap,
            pos_a: Position | None,
            pos_b: Position | None,
        ) -> None:
            self._last_view = view
            for row in range(view.height):
                for col in range(view.width):
                    item = self.item(row, col)
                    if item is None:  # pragma: no cover
                        continue
                    pos = Position(row, col)
                    text = self._texts.get(
                        (row, col),
                        cell_text(view.cell_value(row, col), treasure=view.is_treasure(row, col)),
                    )
                    here_a = pos == pos_a
                    here_b = pos == pos_b
                    if here_a and here_b:
                        item.setText("AB")
                        item.setBackground(_COLOR_A)
                    elif here_a:
                        item.setText("A")
                        item.setBackground(_COLOR_A)
                    elif here_b:
                        item.setText("B")
                        item.setBackground(_COLOR_B)
                    else:
                        item.setText(text)

        def _on_cell_clicked_qt(self, row: int, col: int) -> None:
            self.cell_clicked.emit(row, col)

    class MainWindow(QMainWindow):
        """GUI 主窗口：地图编辑面板 + 模式下拉 + 动画/弹窗 + 状态展示 + 报告导出。

        本类属于**表现层**：所有渲染数据来自 ``GuiIO.render`` 的 ``ViewMap`` +
        ``PublicStateView`` + ``GameEvent``；它不持有真值地图（真值在 ``GameController``）。
        """

        def __init__(
            self,
            *,
            controller: GameController | None = None,
            mode: GameMode = GameMode.NORMAL,
            params: typing.Sequence[int] = DEFAULT_MAP_PARAMS,
            animation_enabled: bool = True,
            popups_enabled: bool = True,
            animation_interval_ms: int = 90,
            parent: QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            if QApplication.instance() is None:  # pragma: no cover - 使用方必须先建 QApplication
                raise GuiUnavailableError("必须先创建 QApplication 再构造 MainWindow")
            self.controller = controller if controller is not None else GameController()
            self.controller.set_mode(mode)
            #: 动画开关（``False`` = 立即模式：所有动画步骤同步完成，不进入事件循环）
            self.animation_enabled = bool(animation_enabled)
            #: 弹窗开关（``False`` 时只记录 :class:`Popup`，不创建 ``QMessageBox``）
            self.popups_enabled = bool(popups_enabled)
            self.animation_interval_ms = max(1, int(animation_interval_ms))
            self.seat_labels: dict[Seat, str] = dict(SEAT_LABELS_HUMAN_VS_AI[mode])
            #: 渲染过的帧 / 弹窗记录 / 骰子投点记录（测试与报告用）
            self.frames: list[Frame] = []
            self.popups: list[Popup] = []
            self.battle_count = 0
            self.dice_rolls: list[int] = []
            #: 动画计划（每一步的记录，测试用）
            self.animation_steps: list[str] = []
            self.last_message_box: QMessageBox | None = None
            self._open_boxes: list[QMessageBox] = []
            self._game_running = False
            self._closing = False
            self._awaiting_dice: int | None = None
            self._pending_action: tuple[Direction, ...] | None = None
            self._action_loop: QEventLoop | None = None
            self._ai_factory_backup = self.controller.ai_factory

            self._build_ui(params)
            self._append_log("欢迎使用对战地图寻宝策略分析平台（GUI）。")
            self._append_log(
                "操作：先「一键随机生成地图」或「读取 txt 地图」，选择模式后点「开始对局」；"
                "轮到人工座位时在输入框键入 w/a/s/d（空格分隔）或 end，点「执行指令」。"
            )
            self._set_input_enabled(False)
            if self.controller.loaded:  # pragma: no cover - 供注入 controller 的场景
                self.refresh_board()

        # -------------------------------------------------------------- 界面构建
        def _build_ui(self, params: typing.Sequence[int]) -> None:
            self.setWindowTitle("对战地图寻宝策略分析平台 ")
            central = QWidget(self)
            root = QVBoxLayout(central)
            root.addWidget(self._build_toolbar(params))

            middle = QHBoxLayout()
            board_box = QGroupBox("地图视图（只显示当前模式的脱敏视图）", central)
            board_layout = QVBoxLayout(board_box)
            self.board = BoardWidget(board_box)
            self.board.cell_clicked.connect(self._on_cell_clicked)
            board_layout.addWidget(self.board, 1)
            self.legend_label = QLabel(
                "图例：# 障碍物｜+v 宝藏｜-v 陷阱（HARD 未触发陷阱伪装为空地）｜A/B 玩家｜"
                "单击格子可编辑：空地 → 墙 → 宝藏 → 陷阱 → 空地",
                board_box,
            )
            self.legend_label.setWordWrap(True)
            board_layout.addWidget(self.legend_label, 0)
            middle.addWidget(board_box, 3)

            side = QVBoxLayout()
            side.addWidget(self._build_status_box())
            side.addWidget(self._build_input_box())
            side.addWidget(self._build_log_box())
            middle.addLayout(side, 2)
            root.addLayout(middle)
            self.setCentralWidget(central)
            self.resize(1080, 720)

        def _build_toolbar(self, params: typing.Sequence[int]) -> QGroupBox:
            box = QGroupBox("地图编辑面板 / 参数与模式", self)
            grid = QGridLayout(box)
            height, width, walls, treasures, traps = tuple(params)[:5]
            self.height_spin = self._spin(1, 64, int(height), "高")
            self.width_spin = self._spin(1, 64, int(width), "宽")
            self.walls_spin = self._spin(0, 4096, int(walls), "墙")
            self.treasures_spin = self._spin(0, 4096, int(treasures), "宝藏")
            self.traps_spin = self._spin(0, 4096, int(traps), "陷阱")
            for index, (label, widget) in enumerate(
                (
                    ("宽（列）", self.width_spin),
                    ("高（行）", self.height_spin),
                    ("墙数量", self.walls_spin),
                    ("宝藏数量", self.treasures_spin),
                    ("陷阱数量", self.traps_spin),
                )
            ):
                grid.addWidget(QLabel(label, box), 0, index * 2)
                grid.addWidget(widget, 0, index * 2 + 1)

            self.mode_combo = QComboBox(box)
            for mode in MODE_ORDER:
                self.mode_combo.addItem(f"{MODE_LABELS[mode]}（{mode.value}）", mode.value)
            self.mode_combo.setCurrentIndex(MODE_ORDER.index(self.controller.mode))
            self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
            grid.addWidget(QLabel("游戏模式", box), 1, 0)
            grid.addWidget(self.mode_combo, 1, 1, 1, 3)

            self.generate_button = QPushButton("一键随机生成地图", box)
            self.generate_button.clicked.connect(lambda: self.on_generate())
            self.load_button = QPushButton("读取 txt 地图", box)
            self.load_button.clicked.connect(lambda: self.on_load())
            self.save_button = QPushButton("保存 txt 地图", box)
            self.save_button.clicked.connect(lambda: self.on_save())
            self.start_button = QPushButton("开始对局（人机）", box)
            self.start_button.clicked.connect(lambda _checked=False: self.on_start_game())
            self.demo_button = QPushButton("机器对机器演示", box)
            self.demo_button.clicked.connect(self.on_start_machine_demo)
            self.abort_button = QPushButton("中止对局", box)
            self.abort_button.clicked.connect(self.on_abort)
            self.export_button = QPushButton("导出对局报告", box)
            self.export_button.clicked.connect(lambda: self.on_export_report())
            for index, button in enumerate(
                (
                    self.generate_button,
                    self.load_button,
                    self.save_button,
                    self.start_button,
                    self.demo_button,
                    self.abort_button,
                    self.export_button,
                )
            ):
                grid.addWidget(button, 2, index)

            self.animation_check = QCheckBox("动画（骰子投点 + 逐格移动）", box)
            self.animation_check.setChecked(self.animation_enabled)
            self.animation_check.toggled.connect(self._on_animation_toggled)
            self.popups_check = QCheckBox("弹窗提示（撞墙/陷阱/对战）", box)
            self.popups_check.setChecked(self.popups_enabled)
            self.popups_check.toggled.connect(self._on_popups_toggled)
            grid.addWidget(self.animation_check, 3, 0, 1, 3)
            grid.addWidget(self.popups_check, 3, 3, 1, 4)
            return box

        def _build_status_box(self) -> QGroupBox:
            box = QGroupBox("对局状态", self)
            layout = QVBoxLayout(box)
            self.turn_label = QLabel("第 - 回合｜等待开始", box)
            self.dice_label = QLabel("骰子：-", box)
            self.score_label = QLabel("比分：A = 0｜B = 0", box)
            self.steps_label = QLabel("本回合剩余有效步数：-/-", box)
            self.last_turn_label = QLabel("上一回合：（尚未开始）", box)
            self.treasure_label = QLabel("剩余宝藏：-｜模式：-", box)
            for widget in (
                self.turn_label,
                self.dice_label,
                self.score_label,
                self.steps_label,
                self.last_turn_label,
                self.treasure_label,
            ):
                widget.setWordWrap(True)
                layout.addWidget(widget)
            return box

        def _build_input_box(self) -> QGroupBox:
            box = QGroupBox("人工指令（w/a/s/d 空格分隔，end 结束回合）", self)
            layout = QVBoxLayout(box)
            self.prompt_label = QLabel("（当前未轮到人工座位）", box)
            self.prompt_label.setWordWrap(True)
            layout.addWidget(self.prompt_label)
            row = QHBoxLayout()
            self.input_edit = QLineEdit(box)
            self.input_edit.setPlaceholderText("例如：d d s end")
            self.input_edit.returnPressed.connect(self.on_submit_input)
            self.input_edit.textChanged.connect(self._on_input_changed)
            self.submit_button = QPushButton("执行指令", box)
            self.submit_button.clicked.connect(self.on_submit_input)
            self.end_turn_button = QPushButton("结束回合(end)", box)
            self.end_turn_button.clicked.connect(self.on_end_turn)
            row.addWidget(self.input_edit, 3)
            row.addWidget(self.submit_button, 1)
            row.addWidget(self.end_turn_button, 1)
            layout.addLayout(row)
            self.hint_label = QLabel("", box)
            self.hint_label.setWordWrap(True)
            layout.addWidget(self.hint_label)
            return box

        def _build_log_box(self) -> QGroupBox:
            box = QGroupBox("对局日志（冻结文案逐字输出）", self)
            layout = QVBoxLayout(box)
            self.log_view = QPlainTextEdit(box)
            self.log_view.setReadOnly(True)
            layout.addWidget(self.log_view)
            return box

        @staticmethod
        def _spin(low: int, high: int, value: int, _name: str) -> QSpinBox:
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setValue(max(low, min(high, value)))
            return spin

        # -------------------------------------------------------------- 表单
        def params(self) -> tuple[int, int, int, int, int]:
            """表单参数：``(高, 宽, 墙, 宝藏, 陷阱)``。"""
            return (
                self.height_spin.value(),
                self.width_spin.value(),
                self.walls_spin.value(),
                self.treasures_spin.value(),
                self.traps_spin.value(),
            )

        def current_mode(self) -> GameMode:
            data = self.mode_combo.currentData()
            return GameMode(str(data)) if data is not None else self.controller.mode

        def set_mode(self, mode: GameMode) -> None:
            """代码侧切换模式（同步下拉框 → 触发 ``_on_mode_changed``）。"""
            index = MODE_ORDER.index(mode)
            if self.mode_combo.currentIndex() == index:
                self._on_mode_changed(index)
            else:
                self.mode_combo.setCurrentIndex(index)

        def _on_mode_changed(self, _index: int) -> None:
            mode = self.current_mode()
            self.controller.set_mode(mode)
            self.seat_labels = dict(SEAT_LABELS_HUMAN_VS_AI[mode])
            self._append_log(f"已切换模式：{MODE_LABELS[mode]}（{mode.value}）")
            self.refresh_board()  # HARD：预览图立即隐藏未触发陷阱

        def _on_animation_toggled(self, checked: bool) -> None:
            self.animation_enabled = bool(checked)
            self._append_log("动画已开启。" if checked else "动画已关闭（立即模式）。")

        def _on_popups_toggled(self, checked: bool) -> None:
            self.popups_enabled = bool(checked)

        # -------------------------------------------------------------- 地图操作
        def on_generate(self, seed: int | None = None) -> str:
            """一键随机生成地图（表单参数）→ 刷新棋盘 → 返回摘要文本。"""
            try:
                message = self.controller.generate_map(*self.params(), seed=seed)
            except (MapFormatError, IllegalStateError, ValueError) as exc:
                message = f"随机生成失败：{type(exc).__name__}: {exc}"
                self._append_log(message)
                self._set_status(message)
                return message
            self._append_log(message)
            self._set_status(message)
            self.refresh_board()
            return message

        def on_load(self, path: pathlib.Path | str | None = None) -> str:
            """读取 txt 地图；``path`` 为空时弹文件选择框。"""
            target = path if path is not None else self._ask_open_path()
            if target is None:
                return "（已取消读取）"
            try:
                message = self.controller.load_txt(target)
            except (MapFormatError, IllegalStateError, OSError, ValueError) as exc:
                message = f"读取失败：{type(exc).__name__}: {exc}"
                self._append_log(message)
                self._set_status(message)
                return message
            self._append_log(message)
            self._set_status(message)
            self.refresh_board()
            return message

        def on_save(self, path: pathlib.Path | str | None = None) -> str:
            """保存当前地图为 txt；``path`` 为空时弹保存对话框。"""
            target = path if path is not None else self._ask_save_path("保存 txt 地图", ".txt")
            if target is None:
                return "（已取消保存）"
            try:
                saved = self.controller.save_txt(target)
            except (IllegalStateError, OSError, ValueError) as exc:
                message = f"保存失败：{type(exc).__name__}: {exc}"
                self._append_log(message)
                self._set_status(message)
                return message
            message = f"地图已保存：{saved}"
            self._append_log(message)
            self._set_status(message)
            return message

        def on_export_report(self, path: pathlib.Path | str | None = None) -> str:
            """导出对局报告；``path`` 为空时弹保存对话框（默认 ``Project/reports/``）。"""
            if self.controller.last_result is None:
                message = "还没有对局结果：请先运行一局再导出报告。"
                self._append_log(message)
                self._set_status(message)
                return message
            if path is None:
                default = str(default_report_path(self.controller.last_result))
                chosen = self._ask_save_path("导出对局报告", ".txt", default)
                if chosen is None:
                    return "（已取消导出）"
                path = chosen
            try:
                saved = self.controller.export_report(path)
            except (IllegalStateError, OSError, ValueError) as exc:
                message = f"导出失败：{type(exc).__name__}: {exc}"
                self._append_log(message)
                self._set_status(message)
                return message
            message = f"对局报告已导出：{saved}"
            self._append_log(message)
            self._set_status(message)
            return message

        def _on_cell_clicked(self, row: int, col: int) -> None:
            """地图编辑：单击循环格值（对局进行中禁止编辑）。"""
            if self._game_running:
                self._set_status("对局进行中：不能编辑地图。")
                return
            if not self.controller.loaded:
                self._set_status("尚未加载地图：请先随机生成或读取 txt 地图。")
                return
            try:
                message = self.controller.cycle_cell(row, col)
            except (IllegalStateError, MapFormatError, ValueError) as exc:
                message = f"编辑失败：{type(exc).__name__}: {exc}"
                self._append_log(message)
                self._set_status(message)
                return
            self._append_log(message)
            self._set_status(message)
            self.refresh_board()

        def refresh_board(self) -> None:
            """按当前模式重画预览棋盘（HARD 隐藏未触发陷阱）。"""
            if not self.controller.loaded:
                self.board.clear_board()
                return
            view = self.controller.authoring_view()
            self.board.set_frame(view, self.controller.pos_a, self.controller.pos_b)

        # -------------------------------------------------------------- 对局
        def on_start_game(self, mode: GameMode | None = None) -> GameResult | None:
            """开始人机对抗（人工座位通过输入框给指令）。"""
            if self._game_running:
                self._set_status("对局进行中：请先完成或用「中止对局」退出。")
                return None
            game_mode = mode if mode is not None else self.current_mode()
            if not self.controller.loaded:
                self.on_generate(seed=20260928)
                if not self.controller.loaded:
                    return None
            self.controller.set_mode(game_mode)
            labels = dict(SEAT_LABELS_HUMAN_VS_AI[game_mode])
            self.seat_labels = labels
            io = GuiIO(sink=self, seat_labels=labels)
            self._begin_game()
            try:
                result = self.controller.run_human_vs_ai(
                    game_mode, io=io, seat_labels=labels
                )
            except HumanAbortError as exc:
                self._append_log(f"已中止当前对局：{exc}")
                self._set_status("对局已中止（不计胜负）")
                return None
            except (IllegalStateError, MapFormatError, OSError, ValueError) as exc:
                self._append_log(f"操作失败：{type(exc).__name__}: {exc}")
                self._set_status(f"操作失败：{type(exc).__name__}")
                return None
            finally:
                self._end_game()
            self._on_game_result(result)
            return result

        def on_start_machine_demo(self) -> GameResult | None:
            """机器对机器演示（贪心 A VS OPT B），无需人工输入。"""
            if self._game_running:
                self._set_status("对局进行中：请先完成或用「中止对局」退出。")
                return None
            if not self.controller.loaded:
                self.on_generate(seed=20260928)
                if not self.controller.loaded:
                    return None
            labels = {Seat.A: "贪心 AI", Seat.B: "OPT 最优 AI"}
            self.seat_labels = labels
            io = GuiIO(sink=self, seat_labels=labels)
            self._begin_game()
            try:
                result = self.controller.run_machine_vs_machine(io=io)
            except (IllegalStateError, MapFormatError, OSError, ValueError) as exc:
                self._append_log(f"操作失败：{type(exc).__name__}: {exc}")
                self._set_status(f"操作失败：{type(exc).__name__}")
                return None
            finally:
                self._end_game()
            self._on_game_result(result)
            return result

        def on_abort(self) -> None:
            """中止对局：仅在正等待人工输入时生效（受控中止，不产生假胜负）。"""
            if self._action_loop is not None:
                self._pending_action = None
                self._action_loop.quit()
                self._set_status("已请求中止对局……")
            else:
                self._set_status("（当前不在等待人工输入，无法立即中止）")

        def _begin_game(self) -> None:
            self._game_running = True
            self.frames.clear()
            self.popups.clear()
            self.battle_count = 0
            self.dice_rolls.clear()
            self.animation_steps.clear()
            self._set_game_controls_enabled(False)
            self._set_status("对局进行中……")

        def _end_game(self) -> None:
            self._game_running = False
            self._action_loop = None
            self._pending_action = None
            self._awaiting_dice = None
            self._set_input_enabled(False)
            self._set_game_controls_enabled(True)

        def _on_game_result(self, result: GameResult) -> None:
            winner = result.winner if result.winner is not None else "平局"
            if result.draw_timeout:
                winner += "（超时未分胜负）"
            text = (
                f"模式={MODE_LABELS[result.mode]}  回合数={result.turns}  "
                f"比分 A={result.score_a}  B={result.score_b}  胜者={winner}"
            )
            self._show_popup("GAME_RESULT", "对局结束", text)
            self._append_log(f"【对局结束】{text}")
            self._set_status(f"对局结束：{text}（可点「导出对局报告」）")

        # -------------------------------------------------------------- GameIO 回调
        def apply_frame(self, frame: Frame) -> None:
            """渲染一帧：状态标签 → 事件（弹窗/日志）→ 静态棋盘 → 令牌动画（落到终态）。"""
            if not isinstance(frame, Frame):
                raise ValueError(f"apply_frame 只接受 Frame，收到 {type(frame).__name__}")
            self.frames.append(frame)
            state_view = frame.state_view
            self.mode_combo.setCurrentIndex(MODE_ORDER.index(state_view.mode))
            self.controller.set_mode(state_view.mode)
            self.turn_label.setText(
                f"第 {state_view.turn_index + 1} 回合｜轮到 "
                f"{self._seat_name(state_view.turn)}｜骰子 N = {state_view.dice_N}"
            )
            self.score_label.setText(
                f"比分：A（{self.seat_labels.get(Seat.A, 'A')}）= {state_view.score_a}｜"
                f"B（{self.seat_labels.get(Seat.B, 'B')}）= {state_view.score_b}"
            )
            self.treasure_label.setText(
                f"剩余宝藏：{state_view.remaining_treasure_count}｜模式："
                f"{MODE_LABELS[state_view.mode]}"
            )
            self._awaiting_dice = state_view.dice_N
            self.steps_label.setText(self._steps_text(state_view.dice_N, 0))
            self._update_last_turn_label(frame)
            self._dispatch_events(frame.events)
            # 先画静态格值（不含令牌），再由动画把令牌逐格移到本帧位置（最后一步落到终态）。
            self.board.set_frame(frame.view)
            self._animate(frame)

        def append_log(self, message: str) -> None:
            """文本日志（``GameIO.log`` 通道）。"""
            self._append_log(str(message))

        def _update_last_turn_label(self, frame: Frame) -> None:
            """按事件流统计上一回合的**有效移动**与剩余有效步数（权威数据来自事件）。"""
            if len(self.frames) < 2:
                self.last_turn_label.setText("上一回合：（本局第一步）")
                return
            previous = self.frames[-2].state_view
            actor = previous.turn
            valid = sum(
                1 for event in frame.events if event.kind == GameEvent.MOVE and event.actor == actor
            )
            blocked = sum(
                1
                for event in frame.events
                if event.kind == GameEvent.BLOCKED and event.actor == actor
            )
            remaining = max(0, previous.dice_N - valid)
            self.last_turn_label.setText(
                f"上一回合：{self._seat_name(actor)} 有效移动 {valid}/{previous.dice_N}，"
                f"剩余有效步数 {remaining}，撞障碍物 {blocked} 次"
            )

        def _steps_text(self, dice_N: int, used: int) -> str:
            return f"本回合剩余有效步数：{max(0, dice_N - used)} / {dice_N}"

        def _dispatch_events(self, events: typing.Sequence[GameEvent]) -> None:
            """事件 → 日志 + 弹窗（撞障碍物/踩陷阱/对战胜负/终局）。"""
            for event in events:
                self._append_log(f"[{event.kind}] {event.message}")
                if event.kind == GameEvent.BLOCKED:
                    self._on_blocked(event)
                elif event.kind == GameEvent.TRAP:
                    self._on_trap(event)
                elif event.kind == GameEvent.TREASURE:
                    self._on_treasure(event)
                elif event.kind == GameEvent.BATTLE:
                    self._on_battle(event)
                elif event.kind == GameEvent.GAME_OVER:
                    self._on_game_over(event)

        # -------------------------------------------------------------- 弹窗（可测）
        def _show_popup(
            self, kind: str, title: str, text: str, *, warning: bool = False, show: bool = True
        ) -> Popup:
            """记录一次弹窗；``popups_enabled`` 且 ``show`` 时才真正创建 ``QMessageBox``。"""
            popup = Popup(kind=kind, title=title, text=text)
            if self.popups_enabled and show and not self._closing:
                box = QMessageBox(self)
                box.setWindowTitle(title)
                box.setText(text)
                box.setIcon(
                    QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information
                )
                box.setStandardButtons(QMessageBox.StandardButton.Ok)
                box.show()  # 非阻塞：游戏不因弹窗停摆（K 键/点击 OK 关闭）
                self._open_boxes.append(box)
                while len(self._open_boxes) > 8:  # 防止长对局堆积弹窗
                    old = self._open_boxes.pop(0)
                    try:
                        old.close()
                    except RuntimeError:  # pragma: no cover - 已被 Qt 回收
                        pass
                self.last_message_box = box
                popup = Popup(kind=kind, title=title, text=text, shown=True)
            self.popups.append(popup)
            return popup

        def _on_blocked(self, event: GameEvent) -> None:
            """撞障碍物弹窗（文案 = 06 §5 冻结文案，逐字不加工）。"""
            self._show_popup("BLOCKED", "障碍物提示", str(event.message), warning=True)

        def _on_trap(self, event: GameEvent) -> None:
            """踩陷阱弹窗；HARD 首次踩中隐藏陷阱走"隐藏陷阱"标题（文案含"陷阱已暴露"）。"""
            hidden = is_hidden_trap(event)
            text = str(event.message)
            if event.actor is not None:
                text = f"触发玩家：{self._seat_name(event.actor)}\n{text}"
            self._show_popup(
                "TRAP",
                "踩到隐藏陷阱！" if hidden else "踩到陷阱",
                text,
                warning=True,
            )

        def _on_treasure(self, event: GameEvent) -> None:
            """拾取宝藏：记录到日志与弹窗记录（默认不弹窗，避免刷屏）。"""
            self._show_popup("TREASURE", "拾取宝藏", str(event.message), show=False)

        def _on_game_over(self, event: GameEvent) -> None:
            """终局事件：记录日志（最终结果弹窗由 :meth:`_on_game_result` 统一给出）。"""
            self._show_popup("GAME_OVER", "游戏结束", str(event.message), show=False)

        def _on_battle(self, result: object) -> None:
            """对战胜负弹窗；接受 ``GameEvent``（事件流）或 ``BattleResult``（直接调用/测试）。"""
            if isinstance(result, BattleResult):
                if not result.triggered:
                    text = f"未触发对战（A={result.score_a}，B={result.score_b}）"
                elif result.score_a > result.score_b:
                    text = f"对战结算：A 胜（A={result.score_a}，B={result.score_b}）"
                elif result.score_b > result.score_a:
                    text = f"对战结算：B 胜（A={result.score_a}，B={result.score_b}）"
                else:
                    text = f"对战结算：平局（A={result.score_a}，B={result.score_b}）"
            elif isinstance(result, GameEvent):
                if result.kind != GameEvent.BATTLE:
                    raise ValueError(f"_on_battle 只接受 BATTLE 事件，收到 {result.kind}")
                text = str(result.message)
            else:
                raise ValueError(
                    f"_on_battle 只接受 GameEvent 或 BattleResult，收到 {type(result).__name__}"
                )
            self.battle_count += 1
            title = f"对战胜负（第 {self.battle_count} 次）"
            self._show_popup("BATTLE", title, text)

        # -------------------------------------------------------------- 动画
        def _animate(self, frame: Frame) -> None:
            """播放动画：骰子投点 + 玩家逐格移动（``animation_enabled=False`` 时同步完成）。

            动画**纯装饰**：棋盘/标签/弹窗的权威状态在调用本方法之前就已写好，
            因此动画被中断也不会影响对局正确性（最后一步一定会落到终态）。
            """
            if self._closing:
                self.board.set_tokens(frame.state_view.pos_a, frame.state_view.pos_b)
                return
            plan = self._build_animation_plan(frame)
            self.animation_steps.extend(plan[1])
            self._play_animation(plan[0])

        def _build_animation_plan(self, frame: Frame) -> tuple[list[typing.Callable[[], None]], list[str]]:
            steps: list[typing.Callable[[], None]] = []
            names: list[str] = []
            dice_N = frame.state_view.dice_N
            for tick in range(3):  # 骰子滚动 3 帧后定格
                face = self._roll_face(dice_N) if tick < 2 else dice_N

                def show_dice(value: int = face) -> None:
                    self.dice_rolls.append(value)
                    self.dice_label.setText(f"骰子：{value}")

                steps.append(show_dice)
                names.append(f"DICE:{face}")
            for event in frame.events:
                if event.kind == GameEvent.MOVE and event.position is not None:
                    if event.actor is not None:
                        steps.append(self._token_step(event))
                        names.append(f"MOVE:{event.actor.value}:{event.position.row},{event.position.col}")
                elif event.kind == GameEvent.BLOCKED and event.actor is not None:
                    steps.append(self._blocked_step(event))
                    names.append(f"BLOCK:{event.actor.value}")
            def settle() -> None:
                self.board.set_tokens(frame.state_view.pos_a, frame.state_view.pos_b)
                self.dice_label.setText(f"骰子：{dice_N}")

            steps.append(settle)
            names.append("SETTLE")
            return steps, names

        def _token_step(self, event: GameEvent) -> typing.Callable[[], None]:
            actor = event.actor
            position = event.position
            assert actor is not None and position is not None

            def step() -> None:
                if actor is Seat.A:
                    self.board.set_tokens(position, None)
                else:
                    self.board.set_tokens(None, position)

            return step

        def _blocked_step(self, event: GameEvent) -> typing.Callable[[], None]:
            actor = event.actor
            position = event.position
            assert actor is not None

            def step() -> None:
                # 先把令牌画在（未改变的）当前坐标，再染成告警色 —— 形成"撞了一下"的视觉反馈。
                if position is not None:
                    if actor is Seat.A:
                        self.board.set_tokens(position, None)
                    else:
                        self.board.set_tokens(None, position)
                self._flash_blocked(actor)

            return step

        def _flash_blocked(self, actor: Seat) -> None:
            """撞墙闪烁：把令牌底色短暂改成告警色（纯视觉，不改格值文本）。"""
            for row in range(self.board.rowCount()):
                for col in range(self.board.columnCount()):
                    item = self.board.item(row, col)
                    if item is None:  # pragma: no cover
                        continue
                    if item.text() in ("A", "AB") and actor is Seat.A:
                        item.setBackground(_COLOR_BLOCKED)
                    elif item.text() in ("B", "AB") and actor is Seat.B:
                        item.setBackground(_COLOR_BLOCKED)

        @staticmethod
        def _roll_face(dice_N: int) -> int:
            """骰子动画中间帧（1..6 之间的随机面；最终一定定格在权威值 ``dice_N``）。"""
            return random.randint(1, 6) if dice_N is None else random.choice(
                [value for value in range(1, 7) if value != dice_N] or [dice_N]
            )

        def _play_animation(self, steps: typing.Sequence[typing.Callable[[], None]]) -> None:
            """播放动画步骤：立即模式同步执行；否则用 ``QTimer`` 驱动嵌套事件循环。

            嵌套循环带"安全超时"（步数 × 间隔 + 1.5s），任何情况都会返回并对齐终态。
            """
            if not steps:
                return
            if not self.animation_enabled or len(steps) == 1:
                for step in steps:
                    step()
                return
            loop = QEventLoop(self)
            timer = QTimer(self)
            index = 0

            def tick() -> None:
                nonlocal index
                if index >= len(steps):  # pragma: no cover - 防御
                    timer.stop()
                    loop.quit()
                    return
                steps[index]()
                index += 1
                if index >= len(steps):
                    timer.stop()
                    loop.quit()

            timer.setInterval(self.animation_interval_ms)
            timer.timeout.connect(tick)
            timer.start()
            safety = QTimer(self)
            safety.setSingleShot(True)
            safety.timeout.connect(loop.quit)
            safety.start(self.animation_interval_ms * (len(steps) + 2) + 1500)
            tick()  # 第一步立即执行
            if index < len(steps):
                loop.exec()
            timer.stop()
            safety.stop()
            for step in steps[index:]:  # 动画被中断也要落到终态
                step()

        # -------------------------------------------------------------- 人工输入
        def request_human_action(
            self, seat: Seat, dice_N: int, view: ViewMap, state_view: PublicStateView
        ) -> tuple[Direction, ...] | None:
            """阻塞式取人工指令（嵌套 ``QEventLoop``）；返回 ``None`` 表示已中止。"""
            if isinstance(view, TrueMap):  # pragma: no cover - 类型上不可能，防御性
                raise ValueError("人工输入不得接收真值地图")
            del view, state_view
            self._pending_action = None
            self._awaiting_dice = dice_N
            self.prompt_label.setText(
                f"{self._seat_name(seat)} 请输入移动指令：w/a/s/d 空格分隔，end 结束回合"
                f"（本回合最多 {dice_N} 步）"
            )
            self.hint_label.setText("")
            self.input_edit.clear()
            self._set_input_enabled(True)
            self.input_edit.setFocus()
            self._action_loop = QEventLoop(self)
            self._action_loop.exec()
            self._action_loop = None
            self._set_input_enabled(False)
            if self._pending_action is None:
                return None
            return self._pending_action

        def on_submit_input(self) -> tuple[Direction, ...] | None:
            """提交输入框里的指令；非法输入就地提示，不退出等待。"""
            dice_N = self._awaiting_dice
            if dice_N is None:
                self.hint_label.setText("当前不在等待人工输入。")
                return None
            steps, error = parse_action_text(self.input_edit.text(), dice_N)
            if error is not None:
                self.hint_label.setText(error)
                return None
            too_many = len(steps) > dice_N
            self._pending_action = steps
            self.input_edit.clear()  # 会触发 textChanged → _on_input_changed（提示随后覆盖）
            self.hint_label.setText(
                f"提示：本回合最多 {dice_N} 次有效移动；"
                f"超出部分在完成 {dice_N} 步后自动忽略。"
                if too_many
                else ""
            )
            if self._action_loop is not None:
                self._action_loop.quit()
            return steps

        def on_end_turn(self) -> tuple[Direction, ...]:
            """放弃本回合剩余步数（等价输入 ``end``）。"""
            self._pending_action = ()
            if self._action_loop is not None:
                self._action_loop.quit()
            return ()

        def _on_input_changed(self, text: str) -> None:
            dice_N = self._awaiting_dice
            if dice_N is None:
                return
            steps, error = parse_action_text(text, dice_N)
            if error is None:
                self.steps_label.setText(
                    f"{self._steps_text(dice_N, len(steps))}（已输入 {len(steps)} 步，未提交）"
                )
            self.hint_label.setText(error or "")

        def _set_input_enabled(self, enabled: bool) -> None:
            for widget in (self.input_edit, self.submit_button, self.end_turn_button):
                widget.setEnabled(enabled)

        # -------------------------------------------------------------- 工具
        def _seat_name(self, seat: Seat) -> str:
            label = self.seat_labels.get(seat)
            return f"{seat.value}（{label}）" if label else seat.value

        def _append_log(self, message: str) -> None:
            self.log_view.appendPlainText(str(message))

        def _set_status(self, message: str) -> None:
            self.statusBar().showMessage(str(message))

        def _set_game_controls_enabled(self, enabled: bool) -> None:
            for widget in (
                self.generate_button,
                self.load_button,
                self.save_button,
                self.start_button,
                self.demo_button,
            ):
                widget.setEnabled(enabled)
            self.export_button.setEnabled(enabled)

        def _ask_open_path(self) -> str | None:
            chosen, _ = QFileDialog.getOpenFileName(
                self, "读取 txt 地图", "", "文本地图 (*.txt);;所有文件 (*)"
            )
            return chosen or None

        def _ask_save_path(self, title: str, suffix: str, default: str = "") -> str | None:
            chosen, _ = QFileDialog.getSaveFileName(
                self, title, default, f"文本文件 (*{suffix});;所有文件 (*)"
            )
            return chosen or None

        # -------------------------------------------------------------- 冒烟
        def run_smoke(
            self,
            *,
            report_path: pathlib.Path | str | None = None,
            seed: int | None = 20260928,
            params: typing.Sequence[int] | None = SMOKE_MAP_PARAMS,
            mode: GameMode | None = None,
        ) -> int:
            """无显示冒烟：随机地图 → 推进一回合 → 导出报告（返回 0/1）。

            ``params=None`` 表示沿用当前已加载的地图（例如 ``--load`` 指定的 txt）。
            """
            self.animation_enabled = False
            self.popups_enabled = False
            self.animation_check.setChecked(False)
            self.popups_check.setChecked(False)
            game_mode = mode if mode is not None else self.current_mode()
            try:
                if params is not None:
                    self.height_spin.setValue(int(params[0]))
                    self.width_spin.setValue(int(params[1]))
                    self.walls_spin.setValue(int(params[2]))
                    self.treasures_spin.setValue(int(params[3]))
                    self.traps_spin.setValue(int(params[4]))
                    self.on_generate(seed=seed)
                self.set_mode(game_mode)
                if not self.controller.loaded:
                    print("GUI 冒烟失败：没有可用地图（--load 路径无效且未生成）", file=sys.stderr)
                    return 1
                io = GuiIO(sink=self, script=[("d",)], fallback=(), seat_labels=self.seat_labels)
                result = self.controller.run_human_vs_ai(
                    game_mode, io=io, seat_labels=self.seat_labels, max_turns=40
                )
                target = self.controller.export_report(report_path)
            except Exception as exc:  # noqa: BLE001 - 冒烟失败必须给出可读原因
                print(f"GUI 冒烟失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            winner = result.winner if result.winner is not None else "平局"
            print(
                f"GUI 冒烟通过：模式={result.mode.value}  回合数={result.turns}  "
                f"比分 A={result.score_a} B={result.score_b}  胜者={winner}  "
                f"帧数={len(self.frames)}  弹窗={len(self.popups)}  报告={target}"
            )
            return 0

        # -------------------------------------------------------------- 生命周期
        def closeEvent(self, event: typing.Any) -> None:  # noqa: N802 - Qt 命名
            self._closing = True
            if self._action_loop is not None:
                self._pending_action = None
                self._action_loop.quit()
            for box in list(self._open_boxes):
                try:
                    box.close()
                except RuntimeError:  # pragma: no cover
                    pass
            self._open_boxes.clear()
            super().closeEvent(event)

    return {
        "BoardWidget": BoardWidget,
        "MainWindow": MainWindow,
        "QApplication": QApplication,
        "QMessageBox": QMessageBox,
        "QEventLoop": QEventLoop,
        "QTimer": QTimer,
    }


def create_app(argv: typing.Sequence[str] | None = None) -> typing.Any:
    """返回（必要时创建）``QApplication``；PyQt6 不可用时抛 :class:`GuiUnavailableError`。

    模块级持有引用（``_APP``）：Qt 的 ``QApplication`` 一旦被 Python 回收，
    后续构造任何 QWidget 都会报 "Must construct a QApplication before a QWidget"。
    """
    global _APP
    classes = qt_classes()
    app = classes["QApplication"].instance()
    if app is None:
        app = classes["QApplication"](list(argv) if argv is not None else [sys.argv[0]])
    _APP = app
    return app


def create_window(**kwargs: typing.Any) -> typing.Any:
    """构造 ``MainWindow``（自动确保 ``QApplication`` 存在）。"""
    create_app()
    return qt_classes()["MainWindow"](**kwargs)


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True, slots=True)
class _Args:
    help: bool = False
    mode: GameMode = GameMode.NORMAL
    seed: int | None = 20260928
    params: tuple[int, int, int, int, int] | None = None
    load: pathlib.Path | None = None
    report: pathlib.Path | None = None
    no_animation: bool = False
    interval: int = 90
    smoke: bool = False
    show: bool = False


class _ArgError(ValueError):
    pass


def _parse_args(argv: typing.Sequence[str]) -> _Args:
    help_flag = False
    mode = GameMode.NORMAL
    seed: int | None = 20260928
    params: tuple[int, int, int, int, int] | None = None
    load: pathlib.Path | None = None
    report: pathlib.Path | None = None
    no_animation = False
    interval = 90
    smoke = False
    show = False
    tokens = list(argv)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-h", "--help"):
            help_flag = True
        elif token == "--no-animation":
            no_animation = True
        elif token == "--smoke":
            smoke = True
        elif token == "--show":
            show = True
        elif token in ("--mode", "--seed", "--load", "--report", "--interval"):
            if index + 1 >= len(tokens):
                raise _ArgError(f"{token} 需要一个取值")
            value = tokens[index + 1]
            index += 1
            if token == "--mode":
                try:
                    mode = GameMode(value.strip().upper())
                except ValueError as exc:
                    raise _ArgError(f"--mode 需要 EASY/NORMAL/HARD，收到 {value!r}") from exc
            elif token == "--seed":
                try:
                    seed = int(value)
                except ValueError as exc:
                    raise _ArgError(f"--seed 需要整数，收到 {value!r}") from exc
            elif token == "--interval":
                if not value.isdigit() or int(value) <= 0:
                    raise _ArgError(f"--interval 需要正整数（毫秒），收到 {value!r}")
                interval = int(value)
            elif token == "--load":
                load = pathlib.Path(value)
            else:
                report = pathlib.Path(value)
        elif token == "--params":
            if index + 5 >= len(tokens):
                raise _ArgError("--params 需要 5 个整数：高 宽 墙 宝藏 陷阱")
            values = tokens[index + 1 : index + 6]
            if any(not item.lstrip("+-").isdigit() for item in values):
                raise _ArgError(f"--params 需要 5 个整数，收到 {values!r}")
            params = typing.cast(
                "tuple[int, int, int, int, int]", tuple(int(item) for item in values)
            )
            index += 5
        else:
            raise _ArgError(f"未知参数 {token!r}")
        index += 1
    return _Args(
        help=help_flag,
        mode=mode,
        seed=seed,
        params=params,
        load=load,
        report=report,
        no_animation=no_animation,
        interval=interval,
        smoke=smoke,
        show=show,
    )


def main(argv: typing.Sequence[str] | None = None, *, app: typing.Any = None) -> int:
    """GUI 入口：返回进程退出码（0 正常 / 1 冒烟失败 / 2 参数或环境错误）。"""
    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parse_args(tokens)
    except _ArgError as exc:
        print(f"参数错误：{exc}")
        print(USAGE)
        return 2
    if args.help:
        print(USAGE)
        return 0
    if not is_available():
        print(_GUI_MISSING_TEXT)
        return 2
    if args.smoke and not args.show:
        # 必须在创建 QApplication 之前设置平台插件
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        qt_app = app if app is not None else create_app()
        window = qt_classes()["MainWindow"](
            mode=args.mode,
            params=args.params if args.params is not None else DEFAULT_MAP_PARAMS,
            animation_enabled=not args.no_animation,
            animation_interval_ms=args.interval,
        )
    except GuiUnavailableError as exc:
        print(f"{_GUI_MISSING_TEXT}\n（{exc}）")
        return 2

    if args.load is not None:
        window.on_load(args.load)
    elif not args.smoke:
        window.on_generate(seed=args.seed)
    window.show()
    if args.smoke:
        # --load 时沿用已读取的地图；否则用 --params 或冒烟默认小地图参数生成
        smoke_params: tuple[int, int, int, int, int] | None = None
        if args.load is None:
            smoke_params = args.params if args.params is not None else SMOKE_MAP_PARAMS
        code = window.run_smoke(
            report_path=args.report,
            seed=args.seed,
            params=smoke_params,
            mode=args.mode,
        )
        window.close()
        return code
    return int(qt_app.exec())


if __name__ == "__main__":  # pragma: no cover - 由 python -m treasure_game.gui 触发
    raise SystemExit(main())
