"""控制台表现层（``ConsoleIO``，W3-02 必做交付）。

职责边界（06 §2/§4.6，视图-真值隔离）
--------------------------------------
* 本类**只**接收 ``ViewMap`` + ``PublicStateView`` + ``GameEvent``；
  它不持有、不访问、也不导入真值状态对象（对局状态类与真值地图类），
  因此无法从人工界面反推任何未触发陷阱。
* ``Simulator`` 调用顺序固定：先 :meth:`ConsoleIO.render`，再 :meth:`ConsoleIO.next_action`。
* 主菜单逐字复刻 spec 第三节 A 的七项（:data:`MENU_TEXT` 与 `docs/spec.md` 的代码块逐行一致，
  由 ``tests/test_cli.py`` 直接比对文档原文）。

输入约定
--------
* 移动指令：``w/a/s/d`` 空格分隔（大小写不敏感），``end`` 结束回合并**截断**其后所有输入；
* 非法/空输入不崩溃：打印提示后**重新读取**，直到拿到合法指令；
* EOF / Ctrl-C → :class:`treasure_game.simulator.HumanAbortError`（受控中止，不产生假胜负）。
"""

from __future__ import annotations

import datetime
import pathlib
import sys
import typing

from treasure_game.models import (
    Direction,
    GameEvent,
    GameMode,
    Position,
    PublicStateView,
    Seat,
    ViewMap,
)
from treasure_game.simulator import HumanAbortError

__all__ = ["ConsoleIO", "MENU_TEXT", "MENU_ITEMS"]

#: 主菜单（spec 第三节 A 逐字复刻；含第 3、5 项后的空行）。
MENU_TEXT = """==== 寻宝博弈对战平台 ====
【人机对抗】
1、简单模式｜人 VS 贪心 AI （全部信息可见）
2、普通模式｜人 VS OPT 最优 AI （全部信息可见）
3、困难模式｜人 VS OPT 最优 AI （陷阱初始隐藏）

【算法测试模式】
4、机器对机器
5、人人对战 MANUAL VS MANUAL

【地图工具】
6、随机生成地图
7、读取本地 txt 地图
请选择功能："""

#: 七个菜单项的编号与标题（供 ``__main__`` 路由与测试断言）。
MENU_ITEMS: dict[int, str] = {
    1: "简单模式｜人 VS 贪心 AI （全部信息可见）",
    2: "普通模式｜人 VS OPT 最优 AI （全部信息可见）",
    3: "困难模式｜人 VS OPT 最优 AI （陷阱初始隐藏）",
    4: "机器对机器",
    5: "人人对战 MANUAL VS MANUAL",
    6: "随机生成地图",
    7: "读取本地 txt 地图",
}

#: 退出输入（菜单未列出"退出"项，这里静默接受 0/q/quit/exit）。
_QUIT_TOKENS = frozenset({"0", "q", "quit", "exit", "退出"})

_HARD_LEGEND = "图例：# = 障碍物   正数 = 宝藏   . = 空地   陷阱踩中前完全隐藏，踩中后显示扣分"
_OPEN_LEGEND = "图例：# = 障碍物   正数 = 宝藏   负数 = 陷阱   . = 空地"

_DEFAULT_LOG_DIR = pathlib.Path(__file__).resolve().parents[2] / "logs"


class ConsoleIO:
    """控制台 ``GameIO`` 实现：渲染视图/事件、解析人工指令、累积对局文本日志。

    :param stdin: 输入流（默认 ``sys.stdin``）；测试可注入 ``io.StringIO``。
    :param stdout: 输出流（默认 ``sys.stdout``）；测试可注入 ``io.StringIO``。
    :param save_log: 是否累积文本日志（``save_log`` 落盘用）。
    :param log_dir: 文本日志目录（默认 ``<Project>/logs``）。
    :param seat_labels: 座位显示名（例如 ``{Seat.A: "人类", Seat.B: "OPT 最优 AI"}``）。
    """

    def __init__(
        self,
        stdin: typing.TextIO | None = None,
        stdout: typing.TextIO | None = None,
        *,
        save_log: bool = True,
        log_dir: pathlib.Path | str | None = None,
        seat_labels: typing.Mapping[Seat, str] | None = None,
    ) -> None:
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._save_log = bool(save_log)
        self._log_dir = pathlib.Path(log_dir) if log_dir is not None else _DEFAULT_LOG_DIR
        self._seat_labels: dict[Seat, str] = {
            Seat.A: "玩家 A",
            Seat.B: "玩家 B",
        }
        if seat_labels:
            self._seat_labels.update(seat_labels)
        self._transcript: list[str] = []
        self._last_mode: GameMode | None = None
        #: 已渲染帧的记录（只含脱敏视图与公开状态，供测试/日志回放；不持有任何真值对象）
        self.views: list[ViewMap] = []
        self.state_views: list[PublicStateView] = []
        self.render_events: list[tuple[GameEvent, ...]] = []
        #: 每帧开始输出前文本日志缓冲区的长度（用于断言"某帧之前"没有泄露）
        self.render_marks: list[int] = []
        self.render_calls: int = 0

    # ------------------------------------------------------------------ 基础输出
    def write(self, text: str = "") -> None:
        """输出一行（并记入文本日志缓冲区）。"""
        self._stdout.write(f"{text}\n")
        self._stdout.flush()
        self._transcript.append(text)

    def log(self, message: str) -> None:
        """``GameIO.log``：逐字输出消息（冻结文案不加工、不加前缀）。"""
        self.write(str(message))

    def set_seat_labels(self, labels: typing.Mapping[Seat, str]) -> None:
        """更新座位显示名（人机/机机/人人三种形态由编排层告知）。"""
        self._seat_labels.update(labels)

    def set_mode(self, mode: GameMode) -> None:
        """记录当前模式（仅用于日志文件名与提示，不参与规则）。"""
        if not isinstance(mode, GameMode):
            raise ValueError(f"mode 必须是 GameMode，收到 {type(mode).__name__}")
        self._last_mode = mode

    @property
    def transcript(self) -> tuple[str, ...]:
        """已输出的所有行（快照，供测试与日志落盘）。"""
        return tuple(self._transcript)

    def save_log(self, path: pathlib.Path | str | None = None) -> pathlib.Path | None:
        """把文本日志写入磁盘；``path`` 为空时按时间戳生成文件名。失败返回 ``None``（不抛）。"""
        if not self._save_log:
            return None
        if path is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            mode = f"_{self._last_mode.value}" if self._last_mode is not None else ""
            path = self._log_dir / f"game_{stamp}{mode}.txt"
        target = pathlib.Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(self._transcript) + "\n", encoding="utf-8")
        except OSError as exc:  # 磁盘/权限问题不得让控制台崩溃
            self.write(f"（日志保存失败：{type(exc).__name__}: {exc}）")
            return None
        return target

    # ------------------------------------------------------------------ 输入
    def read_line(self, prompt: str | None = None) -> str | None:
        """读取一行；``prompt`` 先输出（同一行）。EOF 返回 ``None``。"""
        if prompt:
            self._stdout.write(prompt)
            self._stdout.flush()
            self._transcript.append(prompt)
        raw = self._stdin.readline()
        if raw == "":
            return None
        text = raw.rstrip("\r\n")
        self._transcript.append(f"> {text}")
        return text

    def read_line_or_abort(self, prompt: str | None = None) -> str:
        """读取一行；EOF / Ctrl-C 转为受控中止（人工座位专用）。"""
        try:
            line = self.read_line(prompt)
        except KeyboardInterrupt as exc:
            raise HumanAbortError("人工输入已中止（KeyboardInterrupt）：本局不计胜负") from exc
        if line is None:
            raise HumanAbortError("人工输入已结束（EOF）：本局不计胜负")
        return line

    def prompt_menu(self) -> str | None:
        """打印逐字复刻的主菜单并要求输入；EOF 返回 ``None``（调用方据此退出）。"""
        self.write(MENU_TEXT)
        try:
            return self.read_line()
        except KeyboardInterrupt:
            return None

    # ------------------------------------------------------------------ GameIO：渲染
    def render(
        self,
        view: ViewMap,
        state_view: PublicStateView,
        events: tuple[GameEvent, ...] = (),
    ) -> None:
        """渲染当前回合：回合信息 → 视图地图 → 图例 → 上一回合事件日志。"""
        self.render_calls += 1
        self.render_marks.append(len(self._transcript))
        self.views.append(view)
        self.state_views.append(state_view)
        self.render_events.append(tuple(events))
        self.write("")
        self.write(
            f"---- 第 {state_view.turn_index + 1} 回合 "
            f"| 当前：{self._seat_name(state_view.turn)} "
            f"| 骰子最大有效步数 N = {state_view.dice_N} ----"
        )
        self.write(
            f"比分：A = {state_view.score_a}   B = {state_view.score_b}   "
            f"剩余宝藏：{state_view.remaining_treasure_count}   模式：{state_view.mode.value}"
        )
        self.render_board(view, state_view)
        self.write("【上回合日志】" if events else "【上回合日志】（本局第一步）")
        for event in events:
            self.log(event.message)

    def render_board(self, view: ViewMap, state_view: PublicStateView) -> None:
        """打印视图地图（脱敏后的格子值）；``A``/``B`` 为玩家位置，``#`` 为墙。"""
        lines: list[str] = []
        lines.append("     " + "".join(f"{col:>3}" for col in range(view.width)))
        for row in range(view.height):
            cells: list[str] = []
            for col in range(view.width):
                pos = Position(row, col)
                if pos == state_view.pos_a and pos == state_view.pos_b:
                    cells.append("AB")
                elif pos == state_view.pos_a:
                    cells.append("A")
                elif pos == state_view.pos_b:
                    cells.append("B")
                else:
                    value = view.cell_value(row, col)
                    cells.append("#" if value == 1 else ("." if value == 0 else str(value)))
            lines.append(f"{row:>3}  " + "".join(f"{cell:>3}" for cell in cells))
        for line in lines:
            self.write(line)
        self.write(_HARD_LEGEND if state_view.mode is GameMode.HARD else _OPEN_LEGEND)

    # ------------------------------------------------------------------ GameIO：输入
    def next_action(
        self,
        seat: Seat,
        dice_N: int,
        view: ViewMap,
        state_view: PublicStateView,
    ) -> tuple[Direction, ...]:
        """读取人工指令：``w/a/s/d`` 空格分隔，``end`` 结束回合（截断其后输入）。

        非法或空输入 → 提示后**重新输入**；EOF/Ctrl-C → ``HumanAbortError``（受控中止）。
        四个参数按 06 §2 冻结签名，全部必填（表现层不得从其他渠道补真值）。
        """
        del view  # 视图已在 render 阶段展示；本方法只负责把文本变成方向序列
        if not isinstance(state_view, PublicStateView):
            raise ValueError(
                f"state_view 必须是 PublicStateView（06 §2），收到 {type(state_view).__name__}"
            )
        if isinstance(dice_N, bool) or not isinstance(dice_N, int) or not 1 <= dice_N <= 6:
            raise ValueError(f"dice_N 必须在 1..6，收到 {dice_N!r}")
        self.set_mode(state_view.mode)
        while True:
            prompt = (
                f"{self._seat_name(seat)} 请输入移动指令"
                f"（w/a/s/d 空格分隔，end 结束回合，本回合最多 {dice_N} 步）："
            )
            raw = self.read_line_or_abort(prompt)
            text = raw.strip()
            if not text:
                self.write("非法输入：内容为空。请输入 w/a/s/d（空格分隔）或 end 结束回合。")
                continue
            steps: list[Direction] = []
            invalid: str | None = None
            for token in text.split():
                lowered = token.lower()
                if lowered == "end":
                    break  # end 之后的所有输入被截断
                try:
                    steps.append(Direction.from_char(lowered))
                except ValueError:
                    invalid = token
                    break
            if invalid is not None:
                self.write(
                    f"非法输入：{invalid!r} 不是合法指令（只接受 w/a/s/d 与 end）。请重新输入。"
                )
                continue
            if len(steps) > dice_N:
                self.write(
                    f"提示：本回合最多 {dice_N} 次有效移动；超出部分在完成 {dice_N} 步后自动忽略。"
                )
            return tuple(steps)

    # ------------------------------------------------------------------ 工具
    def _seat_name(self, seat: Seat) -> str:
        label = self._seat_labels.get(seat, seat.value)
        return f"{seat.value}（{label}）"
