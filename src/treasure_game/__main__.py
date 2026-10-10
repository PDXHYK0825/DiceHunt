"""控制台入口：``python -m treasure_game``（spec 第三节 A 的七项主菜单，W3-02）。

运行方式（06 §0）：``cwd = Project/`` 且 ``PYTHONPATH=src``::

    python -m treasure_game
    echo 1 | python -m treasure_game          # 管道喂菜单选择

``main(argv=None) -> int`` 是唯一入口，额外接受可选的 ``stdin`` / ``stdout`` 关键字参数，
便于测试注入假终端而**不真正阻塞等待输入**（不改变 ``main(argv)`` 的位置签名）。

编排层职责（本文件可以接触真值对象，``console_io.py`` 不可以）：
加载/生成地图 → 构造 ``GameState`` → 交给 ``Simulator`` 跑三种对局 → 打印统计与日志路径。
"""

from __future__ import annotations

import dataclasses
import pathlib
import random
import sys
import typing

from treasure_game.console_io import MENU_ITEMS, ConsoleIO
from treasure_game.map_module import MapModule
from treasure_game.models import (
    GameMode,
    GameResult,
    GameState,
    IllegalStateError,
    MapFormatError,
    Position,
    PublicStateView,
    Seat,
    TrueMap,
)
from treasure_game.simulator import HumanAbortError, SeatPolicy, Simulator

__all__ = ["main", "USAGE"]

_QUIT_TOKENS = frozenset({"0", "q", "quit", "exit", "退出"})

#: 交付目录（``Project/``）：地图与日志的默认落点。
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_MAPS_DIR = PROJECT_ROOT / "maps"

#: 未加载地图时，人机/机机入口自动生成的默认小地图参数（低墙密度，保证连通可生成）。
DEFAULT_MAP_PARAMS = (8, 8, 8, 3, 2)

MODE_LABELS: dict[GameMode, str] = {
    GameMode.EASY: "简单模式",
    GameMode.NORMAL: "普通模式",
    GameMode.HARD: "困难模式",
}

USAGE = """用法：python -m treasure_game [选项]
  对战地图寻宝策略分析平台 —— 控制台版本（无参数时进入七项主菜单）

选项：
  -h, --help                 显示本帮助并退出
  --choice N                 启动后直接执行第 N 项菜单（1-7），随后仍进入菜单循环
  --mode {EASY,NORMAL,HARD}  人人对战与地图工具使用的模式（默认 EASY）
  --seed N                   随机地图与骰子种子（默认系统随机；给定则两局可复现）
  --log-dir PATH             对局文本日志目录（默认 Project/logs/）
  --no-log                   不保存对局文本日志
  --depth N                  OPT 搜索深度（默认 3；越大越强但越慢）
  --win-score N              对战胜者加分/败者扣分（默认 5）
  --dice-range MIN-MAX       骰子取值范围（默认 1-6）
  --gui                      启动 PyQt6 图形界面（需已安装 PyQt6；缺失时回退提示）

菜单：1-3 人机对抗（简单/普通/困难）；4 机器对机器；5 人人对战；
      6 随机生成地图；7 读取本地 txt 地图；输入 0 退出。"""


# ---------------------------------------------------------------------------
# 启动参数（手写解析：未知参数返回退出码 2，不抛 SystemExit）
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True, slots=True)
class _Args:
    help: bool = False
    choice: int | None = None
    mode: GameMode = GameMode.EASY
    seed: int | None = None
    no_log: bool = False
    log_dir: pathlib.Path | None = None
    gui: bool = False
    gui_argv: tuple[str, ...] = ()
    depth: int | None = None
    win_score: int | None = None
    dice_range: tuple[int, int] | None = None


class _ArgError(ValueError):
    pass


def _parse_args(argv: typing.Sequence[str]) -> _Args:
    help_flag = False
    choice: int | None = None
    mode = GameMode.EASY
    seed: int | None = None
    no_log = False
    log_dir: pathlib.Path | None = None
    gui = False
    gui_argv: tuple[str, ...] = ()
    depth: int | None = None
    win_score: int | None = None
    dice_range: tuple[int, int] | None = None
    index = 0
    tokens = list(argv)
    while index < len(tokens):
        token = tokens[index]
        if token in ("-h", "--help"):
            help_flag = True
        elif token == "--no-log":
            no_log = True
        elif token == "--gui":
            gui = True
            # --gui 之后的所有参数原样转交 GUI（例如 --smoke / --no-animation / --load）
            gui_argv = tuple(tokens[index + 1:])
            break
        elif token in ("--choice", "--mode", "--seed", "--log-dir",
                       "--depth", "--win-score", "--dice-range"):
            if index + 1 >= len(tokens):
                raise _ArgError(f"{token} 需要一个取值")
            value = tokens[index + 1]
            index += 1
            if token == "--choice":
                if not value.isdigit() or int(value) not in MENU_ITEMS:
                    raise _ArgError(f"--choice 需要 1-7，收到 {value!r}")
                choice = int(value)
            elif token == "--mode":
                try:
                    mode = GameMode(value.strip().upper())
                except ValueError as exc:
                    raise _ArgError(f"--mode 需要 EASY/NORMAL/HARD，收到 {value!r}") from exc
            elif token == "--log-dir":
                log_dir = pathlib.Path(value)
            elif token == "--depth":
                if not value.isdigit() or int(value) < 1:
                    raise _ArgError(f"--depth 需要 >= 1 的整数，收到 {value!r}")
                depth = int(value)
            elif token == "--win-score":
                if not value.isdigit():
                    raise _ArgError(f"--win-score 需要非负整数，收到 {value!r}")
                win_score = int(value)
            elif token == "--dice-range":
                parts = value.replace("~", "-").split("-")
                if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
                    raise _ArgError(f"--dice-range 需要形如 1-6 的两个正整数，收到 {value!r}")
                lo, hi = int(parts[0]), int(parts[1])
                if lo < 1 or hi < lo:
                    raise _ArgError(f"--dice-range 需要满足 1 <= min <= max，收到 {value!r}")
                dice_range = (lo, hi)
            else:
                try:
                    seed = int(value)
                except ValueError as exc:
                    raise _ArgError(f"--seed 需要整数，收到 {value!r}") from exc
        else:
            raise _ArgError(f"未知参数 {token!r}")
        index += 1
    return _Args(help=help_flag, choice=choice, mode=mode, seed=seed, no_log=no_log,
                 log_dir=log_dir, gui=gui, gui_argv=gui_argv,
                 depth=depth, win_score=win_score, dice_range=dice_range)


# ---------------------------------------------------------------------------
# 会话状态：当前地图（仅编排层持有真值对象）
# ---------------------------------------------------------------------------
class _Session:
    """当前加载的地图真值 + 出生点 + 显示模式。"""

    def __init__(self, mode: GameMode) -> None:
        self.mode = mode
        self.true_map: TrueMap | None = None
        self.pos_a: Position | None = None
        self.pos_b: Position | None = None
        self.source: str = ""

    @property
    def loaded(self) -> bool:
        return self.true_map is not None

    def load(
        self, true_map: TrueMap, pos_a: Position, pos_b: Position, *, source: str
    ) -> None:
        self.true_map = true_map
        self.pos_a = pos_a
        self.pos_b = pos_b
        self.source = source

    def initial_state(self, mode: GameMode | None = None) -> GameState:
        if not self.loaded:
            raise IllegalStateError("尚未加载地图：请先随机生成（6）或读取 txt（7）")
        assert self.true_map is not None and self.pos_a is not None and self.pos_b is not None
        return GameState(
            true_map=self.true_map,
            pos_a=self.pos_a,
            pos_b=self.pos_b,
            score_a=0,
            score_b=0,
            turn=Seat.A,
            mode=self.mode if mode is None else mode,
            turn_index=0,
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(
    argv: typing.Sequence[str] | None = None,
    *,
    stdin: typing.TextIO | None = None,
    stdout: typing.TextIO | None = None,
) -> int:
    """控制台主入口：返回进程退出码（0 正常 / 2 参数错误）。"""
    tokens = list(sys.argv[1:] if argv is None else argv)
    out = stdout if stdout is not None else sys.stdout
    try:
        args = _parse_args(tokens)
    except _ArgError as exc:
        print(f"参数错误：{exc}", file=out)
        print(USAGE, file=out)
        return 2
    if args.help:
        print(USAGE, file=out)
        return 0
    if args.gui:
        # 选做 GUI：延迟导入，PyQt6 缺失时不抛裸异常，只回退提示（控制台必做项不受影响）
        try:
            from treasure_game import gui  # noqa: PLC0415  (延迟导入是刻意设计)
        except ImportError as exc:
            print(f"无法启动图形界面（PyQt6 未安装）：{exc}", file=out)
            print("请改用控制台版本：python -m treasure_game", file=out)
            return 2
        if not gui.is_available():
            print("无法启动图形界面（PyQt6 未安装）。请改用：python -m treasure_game", file=out)
            return 2
        forwarded: list[str] = list(args.gui_argv)
        if args.seed is not None and "--seed" not in forwarded:
            forwarded = ["--seed", str(args.seed), *forwarded]
        if "--mode" not in forwarded:
            forwarded += ["--mode", args.mode.value]
        return int(gui.main(forwarded))

    console = ConsoleIO(
        stdin if stdin is not None else sys.stdin,
        out,
        save_log=not args.no_log,
        log_dir=args.log_dir,
    )
    session = _Session(mode=args.mode)
    try:
        return _run_menu(console, session, args, initial_choice=args.choice)
    except KeyboardInterrupt:
        console.write("已取消（KeyboardInterrupt），退出对战平台。")
        return 0


def _run_menu(
    console: ConsoleIO,
    session: _Session,
    args: _Args,
    *,
    initial_choice: int | None = None,
) -> int:
    """七项主菜单循环：非法输入重新提示，EOF/0/q 正常退出（永不冒裸 traceback）。"""
    if initial_choice is not None:
        _dispatch_guarded(console, session, args, initial_choice)
    while True:
        raw = console.prompt_menu()
        if raw is None:
            console.write("输入已结束（EOF），退出对战平台。")
            return 0
        text = raw.strip()
        if text.lower() in _QUIT_TOKENS:
            console.write("已退出对战平台。")
            return 0
        if not text.isdigit() or int(text) not in MENU_ITEMS:
            console.write("非法输入：请输入 1-7 之间的数字（输入 0 退出）。")
            continue
        _dispatch_guarded(console, session, args, int(text))


def _dispatch_guarded(console: ConsoleIO, session: _Session, args: _Args, choice: int) -> None:
    """执行一个菜单项，把可预期失败（地图/AI/IO）变成提示，绝不冒裸 traceback。"""
    try:
        _dispatch(console, session, args, choice)
    except HumanAbortError as exc:
        console.write(f"已中止当前对局：{exc}")
    except KeyboardInterrupt:
        console.write("已取消当前操作，返回主菜单。")
    except (MapFormatError, IllegalStateError, OSError, ValueError) as exc:
        console.write(f"操作失败：{type(exc).__name__}: {exc}")


def _dispatch(console: ConsoleIO, session: _Session, args: _Args, choice: int) -> None:
    if choice == 1:
        session.mode = GameMode.EASY
        _play_human_vs_ai(console, session, args, GameMode.EASY)
    elif choice == 2:
        session.mode = GameMode.NORMAL
        _play_human_vs_ai(console, session, args, GameMode.NORMAL)
    elif choice == 3:
        session.mode = GameMode.HARD
        _play_human_vs_ai(console, session, args, GameMode.HARD)
    elif choice == 4:
        _play_machine_vs_machine(console, session, args)
    elif choice == 5:
        _play_human_vs_human(console, session, args)
    elif choice == 6:
        _generate_map(console, session, args)
    else:
        _load_map(console, session, args)


# ---------------------------------------------------------------------------
# 三个对局入口
# ---------------------------------------------------------------------------
def _play_human_vs_ai(
    console: ConsoleIO, session: _Session, args: _Args, mode: GameMode
) -> None:
    if not _ensure_map(console, session, args):
        return
    ai = _make_ai(args)
    machine = "贪心 AI" if mode is GameMode.EASY else "OPT 最优 AI"
    console.set_mode(mode)
    console.set_seat_labels({Seat.A: "人类", Seat.B: machine})
    policy_b = SeatPolicy.GREEDY if mode is GameMode.EASY else SeatPolicy.OPT
    console.write("")
    console.write(
        f"===== 【人机对抗】{MODE_LABELS[mode]}｜人（A） VS {machine}（B）"
        f"｜地图：{session.source} ====="
    )
    simulator = Simulator(
        initial_state=session.initial_state(mode),
        io=console,
        rng=_new_rng(args),
        policy_a=SeatPolicy.HUMAN,
        policy_b=policy_b,
        max_turns=500,
        ai=ai,
        **_sim_extras(args),
    )
    result = simulator.run_human_vs_ai(mode)
    _report_result(console, result)


def _play_machine_vs_machine(console: ConsoleIO, session: _Session, args: _Args) -> None:
    if not _ensure_map(console, session, args):
        return
    ai = _make_ai(args)
    console.set_mode(session.mode)
    console.set_seat_labels({Seat.A: "贪心 AI", Seat.B: "OPT 最优 AI"})
    console.write("")
    console.write(f"===== 【算法测试模式】机器对机器｜贪心 AI（A） VS OPT 最优 AI（B）"
                  f"｜地图：{session.source} =====")
    simulator = Simulator(
        initial_state=session.initial_state(),
        io=console,
        rng=_new_rng(args),
        policy_a=SeatPolicy.GREEDY,
        policy_b=SeatPolicy.OPT,
        max_turns=500,
        ai=ai,
        **_sim_extras(args),
    )
    result = simulator.run_machine_vs_machine()
    _report_result(console, result)


def _play_human_vs_human(console: ConsoleIO, session: _Session, args: _Args) -> None:
    if not _ensure_map(console, session, args):
        return
    console.set_mode(session.mode)
    console.set_seat_labels({Seat.A: "玩家 1", Seat.B: "玩家 2"})
    console.write("")
    console.write(
        f"===== 【算法测试模式】人人对战 MANUAL VS MANUAL｜{MODE_LABELS[session.mode]}"
        f"｜地图：{session.source} ====="
    )
    simulator = Simulator(
        initial_state=session.initial_state(),
        io=console,
        rng=_new_rng(args),
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.HUMAN,
        max_turns=500,
        **_sim_extras(args),
    )
    result = simulator.run_human_vs_human()
    _report_result(console, result)


# ---------------------------------------------------------------------------
# 地图工具
# ---------------------------------------------------------------------------
def _generate_map(console: ConsoleIO, session: _Session, args: _Args) -> None:
    console.write("")
    console.write("===== 【地图工具】随机生成地图 =====")
    raw = console.read_line(
        "请输入地图参数（高 宽 墙数 宝藏数 陷阱数，空格分隔，例如 8 8 8 3 2）："
    )
    if raw is None:
        console.write("输入已结束（EOF），取消地图生成。")
        return
    parts = raw.split()
    if len(parts) != 5 or any(not part.lstrip("+-").isdigit() for part in parts):
        console.write("非法输入：需要 5 个整数（高 宽 墙数 宝藏数 陷阱数）。已取消。")
        return
    height, width, walls, treasures, traps = (int(part) for part in parts)
    true_map, pos_a, pos_b = MapModule.generate(
        height, width, walls, treasures, traps, _new_rng(args)
    )
    session.load(true_map, pos_a, pos_b, source=f"随机生成 {height}x{width}")
    console.write(
        f"地图已生成：{height}x{width}  墙={walls}  宝藏={treasures}  陷阱={traps}  "
        f"map_id={true_map.map_id}  出生点 A={_fmt_pos(pos_a)} B={_fmt_pos(pos_b)}"
    )
    _show_map(console, session)

    name = console.read_line("保存为 txt 地图文件名或路径（回车跳过保存）：")
    if name is None or not name.strip():
        console.write("（未保存地图文件）")
        return
    path = _save_target(name.strip())
    MapModule.save_txt(path, true_map, pos_a, pos_b)
    console.write(f"地图已保存：{path}")


def _save_target(text: str) -> pathlib.Path:
    """把用户输入解析成保存路径：含路径分隔符/绝对路径按原样（补 ``.txt``），否则落到 ``Project/maps/``。"""
    candidate = pathlib.Path(text)
    has_separator = ("/" in text) or ("\\" in text)
    if not candidate.is_absolute() and not has_separator:
        candidate = DEFAULT_MAPS_DIR / candidate.name
    if candidate.suffix.lower() != ".txt":
        candidate = candidate.with_suffix(".txt")
    return candidate


def _load_map(console: ConsoleIO, session: _Session, args: _Args) -> None:
    del args
    console.write("")
    console.write("===== 【地图工具】读取本地 txt 地图 =====")
    raw = console.read_line("请输入 txt 地图路径（例如 maps/demo.txt 或绝对路径）：")
    if raw is None:
        console.write("输入已结束（EOF），取消读取地图。")
        return
    text = raw.strip()
    if not text:
        console.write("非法输入：路径不能为空。已取消。")
        return
    path = _resolve_map_path(text)
    if path is None:
        console.write(f"地图文件不存在：{text}（也尝试过 {PROJECT_ROOT / text}）")
        return
    true_map, pos_a, pos_b = MapModule.load_txt(path)
    session.load(true_map, pos_a, pos_b, source=str(path))
    console.write(
        f"地图已读取：{path}  {true_map.height}x{true_map.width}  "
        f"宝藏 {len(true_map.treasure_values)} 个  陷阱 {len(true_map.trap_values)} 个  "
        f"map_id={true_map.map_id}  出生点 A={_fmt_pos(pos_a)} B={_fmt_pos(pos_b)}"
    )
    _show_map(console, session)


def _fmt_pos(pos: Position) -> str:
    """坐标显示为 ``(row, col)``（与事件文案一致）。"""
    return f"({pos.row}, {pos.col})"


def _resolve_map_path(text: str) -> pathlib.Path | None:
    """相对路径先按 cwd 解析，再按 ``Project/`` 解析。"""
    candidate = pathlib.Path(text)
    if candidate.is_file():
        return candidate
    fallback = PROJECT_ROOT / text
    if fallback.is_file():
        return fallback
    return None


def _show_map(console: ConsoleIO, session: _Session) -> None:
    """按当前模式渲染已加载/已生成的地图（脱敏视图，不泄露 HARD 未触发陷阱）。"""
    assert session.true_map is not None
    view = MapModule(session.true_map).get_view_map(session.mode)
    state_view = PublicStateView(
        mode=session.mode,
        pos_a=session.pos_a if session.pos_a is not None else Position(0, 0),
        pos_b=session.pos_b if session.pos_b is not None else Position(0, 0),
        score_a=0,
        score_b=0,
        turn=Seat.A,
        turn_index=0,
        dice_N=1,
        remaining_treasure_count=len(session.true_map.remaining_treasures),
    )
    console.render_board(view, state_view)


def _ensure_map(console: ConsoleIO, session: _Session, args: _Args) -> bool:
    """没有地图时自动生成一张默认小地图（低墙密度，保证连通）。"""
    if session.loaded:
        return True
    height, width, walls, treasures, traps = DEFAULT_MAP_PARAMS
    try:
        true_map, pos_a, pos_b = MapModule.generate(
            height, width, walls, treasures, traps, _new_rng(args)
        )
    except (ValueError, OSError) as exc:
        console.write(f"默认地图生成失败：{type(exc).__name__}: {exc}")
        return False
    session.load(true_map, pos_a, pos_b, source=f"默认随机 {height}x{width}")
    console.write(
        f"当前未加载地图，已随机生成默认地图：{height}x{width}  墙={walls}  "
        f"宝藏={treasures}  陷阱={traps}  map_id={true_map.map_id}"
    )
    _show_map(console, session)
    return True


# ---------------------------------------------------------------------------
# 统计与日志
# ---------------------------------------------------------------------------
def _report_result(console: ConsoleIO, result: GameResult) -> None:
    """打印对局结果与性能统计（机器思考耗时、DP 缓存命中、节点、分治近似）。"""
    metrics = result.metrics
    console.write("")
    winner = result.winner if result.winner is not None else "平局"
    suffix = "（超时未分胜负）" if result.draw_timeout else ""
    console.write(
        f"【对局结束】模式={MODE_LABELS[result.mode]}  回合数={result.turns}  "
        f"比分 A={result.score_a}  B={result.score_b}  胜者={winner}{suffix}"
    )
    calls = int(metrics["ai_calls"])  # type: ignore[arg-type]
    elapsed = int(metrics["ai_elapsed_ns"])  # type: ignore[arg-type]
    hits = int(metrics["cache_hits"])  # type: ignore[arg-type]
    misses = int(metrics["cache_misses"])  # type: ignore[arg-type]
    average = f"{elapsed / calls / 1e6:.3f} ms" if calls else "n/a（无机器决策）"
    rate = f"{hits / (hits + misses) * 100:.1f}%" if (hits + misses) else "n/a"
    console.write(
        f"【性能统计】机器决策={calls} 次  累计思考={elapsed / 1e6:.3f} ms  平均={average}  "
        f"DP 缓存命中/未命中={hits}/{misses}（命中率 {rate}）  "
        f"搜索节点={int(metrics['nodes'])}  分治近似={'是' if metrics['partition_used'] else '否'}"
    )
    console.write(
        f"【对局统计】触发对战={int(metrics['battles'])} 次  "
        f"踩陷阱 A/B={int(metrics['trap_steps_a'])}/{int(metrics['trap_steps_b'])}  "
        f"拾宝 A/B={int(metrics['treasure_steps_a'])}/{int(metrics['treasure_steps_b'])}  "
        f"骰序={list(metrics['dice_sequence'])}"
    )
    log_path = console.save_log()
    if log_path is not None:
        console.write(f"对局日志已保存：{log_path}")


def _make_ai(args: _Args | None = None) -> object:
    """构造 ``AiModule``；未显式指定时用 06 §3 冻结默认（depth=3 / K=24 / 阈值 144 / 缓存+剪枝）。

    ``--depth`` 属于 spec 选做项"可配置的 OPT 搜索深度"。
    """
    try:
        from treasure_game.ai_module import AiModule
    except ImportError as exc:
        raise IllegalStateError(
            "AI 模块尚未就绪：无法导入 treasure_game.ai_module（人机/机机模式需要它）"
        ) from exc
    if args is None or args.depth is None:
        return AiModule()
    return AiModule(depth=args.depth)


def _sim_extras(args: _Args) -> dict[str, object]:
    """把选做项"可配置的奖惩分数 / 骰子范围"转成 ``Simulator`` 关键字参数。"""
    extras: dict[str, object] = {}
    if args.win_score is not None:
        extras["battle_win_score"] = args.win_score
    if args.dice_range is not None:
        extras["dice_min"], extras["dice_max"] = args.dice_range
    return extras


def _new_rng(args: _Args) -> random.Random:
    return random.Random(args.seed) if args.seed is not None else random.Random()


if __name__ == "__main__":  # pragma: no cover - 模块方式运行时由 __main__ 触发
    raise SystemExit(main())
