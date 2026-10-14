"""仿真模拟器（Simulator）：三种对局入口共享同一个私有回合循环（06 §3/§4，W3-01）。

设计要点
--------
* **单一回合循环**：``run_human_vs_ai`` / ``run_machine_vs_machine`` / ``run_human_vs_human``
  只做"座位策略路由"，规则全部落在 :meth:`Simulator._run` 里，杜绝复制第二份规则。
* **回合顺序**（06 §4.7 / §4.9）：掷骰（注入 RNG 均匀 1..6，或 ``dice_seed_table`` 按
  ``turn_index*2 + (0 for A else 1)`` 取模，**绝不消耗搜索 RNG**）→ ``GameIO.render`` →
  ``GameIO.next_action`` / AI 决策 → :meth:`PathModule.sim_player_multi_step`（唯一移动真相源，
  ``actor=seat``）→ 更新 ``GameState`` → **仅此时**（回合终点）调用一次
  :meth:`BattleModule.trigger_battle` → 判定终局。
* **中途路过对手不触发对战**：对战只在每个座位的整轮移动结束后按其终点坐标结算一次；
  同回合不递归、不重复结算（04 风险表 R14）。
* **视野隔离**：``GameIO`` 只拿到 ``ViewMap`` + ``PublicStateView``；``Simulator`` 自己从不把
  真值图交给表现层。
* **HARD 隐藏陷阱提示**：``PathModule`` 是模式无关纯内核，只发通用 ``TRAP`` 事件
  （``踩到陷阱 -2 分``）。本模块依据"``mode == HARD`` 且该陷阱在**本次移动之前**不在
  ``TrueMap.revealed_traps`` 中"把这条事件**改写**成
  ``GameEvent.trap(..., hidden=True)``（文案 ``【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】``，
  数字取扣分绝对值）——保持"恰好一条 TRAP 事件"，同时不泄露其他未触发陷阱坐标/负值。
* **``GameResult.metrics``** 填齐 06 §5.5 的全部冻结键（见 :data:`METRIC_KEYS`）。
* **初态即空**：不掷骰、不对战，直接以终局事件收尾；**最后宝藏**被拾取的回合先做终点对战
  再判定终局（D11）；``turn_index >= max_turns`` 仍有宝藏 → ``draw_timeout=True``、``winner=None``。
* **人工中止**：人工座位抛出 ``EOFError``/``KeyboardInterrupt`` 或返回 ``None`` 时转换为
  :class:`HumanAbortError`（``IllegalStateError`` 子类）——受控中止，不产生假胜负，也不冒裸 traceback。

本模块只依赖标准库与项目内真值模块；不导入 ``pytest`` / GUI。
"""

from __future__ import annotations

import collections.abc
import dataclasses
import enum
import random
import time

from treasure_game.battle_module import BattleModule
from treasure_game.map_module import MapModule
from treasure_game.models import (
    Direction,
    GameEvent,
    GameMode,
    GameResult,
    GameState,
    GameIO,
    IllegalStateError,
    PublicStateView,
    Seat,
    ViewMap,
)
from treasure_game.path_module import PathModule

__all__ = ["SeatPolicy", "Simulator", "HumanAbortError", "METRIC_KEYS"]

#: 06 §5.5 冻结的 ``GameResult.metrics`` 键（顺序即文档顺序；缺键 = W4 实验直接失败）。
METRIC_KEYS: tuple[str, ...] = (
    "battles",
    "trap_steps_a",
    "trap_steps_b",
    "treasure_steps_a",
    "treasure_steps_b",
    "ai_calls",
    "ai_elapsed_ns",
    "cache_hits",
    "cache_misses",
    "nodes",
    "partition_used",
    "dice_sequence",
    "timed_out",
)


class SeatPolicy(enum.Enum):
    """座位策略（06 §3 冻结四值；``OPT_PARTITION`` 即"允许走分治近似的 OPT"）。"""

    HUMAN = "HUMAN"
    GREEDY = "GREEDY"
    OPT = "OPT"
    OPT_PARTITION = "OPT_PARTITION"

    def __str__(self) -> str:  # pragma: no cover - 仅为日志可读性
        return self.value


class HumanAbortError(IllegalStateError):
    """人工输入被中止（EOF / Ctrl-C / 返回 ``None``）：本局不计胜负（追加 API，06 未冻结）。"""


# ---------------------------------------------------------------------------
# 表现层适配器（GameIO）调用约定：严格按 06 §2 的四参形式调用，
# ``render(view, state_view, events)`` → ``next_action(seat, dice_N, view, state_view)``。
# ---------------------------------------------------------------------------
def _import_ai_module() -> type:
    """按需导入 Wave 2 的 ``AiModule``（模块级不导入，保证 AI 未落地时本模块仍可导入）。"""
    try:
        from treasure_game.ai_module import AiModule
    except ImportError as exc:  # pragma: no cover - 取决于队友落地进度
        raise IllegalStateError(
            "AI 模块不可用：无法导入 treasure_game.ai_module.AiModule；"
            "机器座位需要它（06 §3 冻结签名）。"
        ) from exc
    return AiModule


def _machine_policy_for_mode(game_mode: GameMode, configured: SeatPolicy) -> SeatPolicy:
    """人机对抗的机器座位路由（06 §3/01 §5）：EASY → 贪心；NORMAL/HARD → OPT 引擎。

    ``OPT_PARTITION`` 是"允许分治近似的 OPT"，仅在 NORMAL/HARD 且调用方显式配置它时保留，
    其余情况一律按模式取默认值（EASY→GREEDY、NORMAL/HARD→OPT）。
    """
    if game_mode is GameMode.EASY:
        return SeatPolicy.GREEDY
    if configured is SeatPolicy.OPT_PARTITION:
        return SeatPolicy.OPT_PARTITION
    return SeatPolicy.OPT


def _winner_of(score_a: int, score_b: int) -> str | None:
    """终局比分 → ``"A"`` / ``"B"`` / ``None``（平局）。"""
    if score_a > score_b:
        return "A"
    if score_b > score_a:
        return "B"
    return None


class Simulator:
    """一局对战的状态机外壳：构造一次，可重复运行（每次运行从 ``initial_state`` 重新开始）。

    :param initial_state: 对局初态（真值图 + 双方坐标分数 + 模式）；契约要求 ``turn=Seat.A``，
        本实现会在每次运行开始时把回合规范化到 A 先手（骰序索引 ``turn_index*2+seat`` 依赖该约定）。
    :param io: 表现层适配器（``GameIO``）；人工座位必须有它，机机对局可以为 ``None``。
    :param rng: 骰子 RNG（均匀 1..6）；注入 ``dice_seed_table`` 时完全不消耗它。
    :param policy_a / policy_b: 座位策略；``run_human_vs_ai`` 强制 A=HUMAN 并按模式路由 B。
    :param max_turns: 回合（一轮 = A 一次 + B 一次）上限；``<=0`` 抛 ``ValueError``。
    :param ai: 注入的 ``AiModule``；``None`` 时按 06 §3 默认参数构造（每次运行新建，指标隔离）。
    :param dice_seed_table: 预生成骰值表（每项 1..6），按 ``turn_index*2 + (0 for A else 1)`` 取模。
    :param dice_min / dice_max: 骰子取值范围（默认 1..6，规格第二节"1-6 均匀随机"）。
        两者必须为 ``int`` 且 ``1 <= dice_min <= dice_max``；注入 ``dice_seed_table`` 时表格值
        必须落在该范围内。属于 spec 选做项"可配置的骰子范围"。
    :param battle_win_score: 对战胜者加分（败者同额扣分，默认 5，规格第二节）。必须为非负 ``int``。
        属于 spec 选做项"可配置的对战奖惩分数"。
    """

    def __init__(
        self,
        initial_state: GameState,
        io: GameIO | None = None,
        rng: random.Random | None = None,
        policy_a: SeatPolicy = SeatPolicy.HUMAN,
        policy_b: SeatPolicy = SeatPolicy.OPT,
        max_turns: int = 500,
        ai: AiModule | None = None,  # noqa: F821 - 延迟导入的类型，见 _import_ai_module
        dice_seed_table: collections.abc.Sequence[int] | None = None,
        dice_min: int = 1,
        dice_max: int = 6,
        battle_win_score: int = 5,
    ) -> None:
        if not isinstance(initial_state, GameState):
            raise ValueError(f"initial_state 必须是 GameState，收到 {type(initial_state).__name__}")
        if io is not None:
            self._validate_io(io)
        if rng is not None and not isinstance(rng, random.Random):
            raise ValueError(f"rng 必须是 random.Random，收到 {type(rng).__name__}")
        for name, policy in (("policy_a", policy_a), ("policy_b", policy_b)):
            if not isinstance(policy, SeatPolicy):
                raise ValueError(f"{name} 必须是 SeatPolicy，收到 {type(policy).__name__}")
        if isinstance(max_turns, bool) or not isinstance(max_turns, int):
            raise ValueError(f"max_turns 必须是 int，收到 {type(max_turns).__name__}")
        if max_turns <= 0:
            raise ValueError(f"max_turns 必须为正，收到 {max_turns}")
        for name, value in (("dice_min", dice_min), ("dice_max", dice_max)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是 int，收到 {type(value).__name__}")
        if dice_min < 1:
            raise ValueError(f"dice_min 必须 >= 1，收到 {dice_min}")
        if dice_max < dice_min:
            raise ValueError(f"dice_max 必须 >= dice_min，收到 {dice_min}..{dice_max}")
        if isinstance(battle_win_score, bool) or not isinstance(battle_win_score, int):
            raise ValueError(f"battle_win_score 必须是 int，收到 {type(battle_win_score).__name__}")
        if battle_win_score < 0:
            raise ValueError(f"battle_win_score 必须非负，收到 {battle_win_score}")
        if ai is not None:
            for method in ("get_greedy_move", "get_opt_move"):
                if not callable(getattr(ai, method, None)):
                    raise ValueError(f"ai 缺少冻结方法 {method}（06 §3）")
        table = self._validate_dice_table(dice_seed_table, dice_min, dice_max)

        self._initial_state = initial_state
        self._io = io
        self._rng = rng if rng is not None else random.Random()
        self._policy_a = policy_a
        self._policy_b = policy_b
        self._max_turns = max_turns
        self._injected_ai = ai
        self._dice_seed_table = table
        self._dice_min = dice_min
        self._dice_max = dice_max
        self._battle_win_score = battle_win_score

    # ------------------------------------------------------------------ 构造校验
    @staticmethod
    def _validate_io(io: object) -> None:
        for method in ("render", "next_action", "log"):
            if not callable(getattr(io, method, None)):
                raise ValueError(f"io 必须是 GameIO（缺少 {method} 方法），收到 {type(io).__name__}")

    @staticmethod
    def _validate_dice_table(
        table: collections.abc.Sequence[int] | None,
        dice_min: int = 1,
        dice_max: int = 6,
    ) -> tuple[int, ...] | None:
        if table is None:
            return None
        if isinstance(table, (str, bytes)) or not isinstance(table, collections.abc.Sequence):
            raise ValueError(f"dice_seed_table 必须是 int 序列，收到 {type(table).__name__}")
        values = tuple(table)
        if not values:
            raise ValueError("dice_seed_table 不能为空")
        for index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"dice_seed_table[{index}] 必须是 int，收到 {type(value).__name__}"
                )
            if not dice_min <= value <= dice_max:
                raise ValueError(
                    f"dice_seed_table[{index}] 必须在 {dice_min}..{dice_max}，收到 {value}"
                )
        return values

    # ------------------------------------------------------------------ 三入口
    def run_human_vs_ai(self, game_mode: GameMode) -> GameResult:
        """人机对抗：A=HUMAN（需 IO），B 按模式取 GREEDY（EASY）/ OPT（NORMAL、HARD）。

        视图由 ``MapModule(state.true_map).get_view_map(game_mode)`` 生成——
        以**入口参数** ``game_mode`` 为准（同时把初态模式规范化到该值）。
        """
        if not isinstance(game_mode, GameMode):
            raise ValueError(f"game_mode 必须是 GameMode，收到 {type(game_mode).__name__}")
        if self._io is None:
            raise IllegalStateError("人机对抗需要注入 GameIO：人工座位没有输入输出通道")
        policies = {
            Seat.A: SeatPolicy.HUMAN,
            Seat.B: _machine_policy_for_mode(game_mode, self._policy_b),
        }
        return self._run(mode=game_mode, policies=policies)

    def run_machine_vs_machine(self) -> GameResult:
        """机器对机器：默认 A=GREEDY、B=OPT；构造注入的机器策略优先生效。

        ``HUMAN`` 在机机对局中没有输入通道，因此会按默认值解析（A→GREEDY、B→OPT），
        保证 ``Simulator(state).run_machine_vs_machine()`` 这种默认构造也能直接跑。
        """
        policy_a = self._policy_a if self._policy_a is not SeatPolicy.HUMAN else SeatPolicy.GREEDY
        policy_b = self._policy_b if self._policy_b is not SeatPolicy.HUMAN else SeatPolicy.OPT
        return self._run(mode=self._initial_state.mode, policies={Seat.A: policy_a, Seat.B: policy_b})

    def run_human_vs_human(self) -> GameResult:
        """人人对战：两个座位都是人工（需 IO），模式沿用构造初态的模式。"""
        if self._io is None:
            raise IllegalStateError("人人对战需要注入 GameIO：两个人工座位都需要输入输出通道")
        policies = {Seat.A: SeatPolicy.HUMAN, Seat.B: SeatPolicy.HUMAN}
        return self._run(mode=self._initial_state.mode, policies=policies)

    # ------------------------------------------------------------------ 私有回合循环
    def _run(self, mode: GameMode, policies: dict[Seat, SeatPolicy]) -> GameResult:
        """唯一回合循环：三入口都走这里（禁止复制规则）。"""
        io = self._io
        human_seats = frozenset(seat for seat, policy in policies.items() if policy is SeatPolicy.HUMAN)
        if human_seats and io is None:
            raise IllegalStateError("人工座位缺少 GameIO：无法获取人工指令")

        state = dataclasses.replace(self._initial_state, mode=mode, turn=Seat.A, turn_index=0)
        machine_seats = frozenset(seat for seat in Seat if seat not in human_seats)
        ai = self._resolve_ai(machine_seats, policies)

        events: list[GameEvent] = []
        dice_sequence: list[int] = []
        stats = self._new_stats()
        pending: tuple[GameEvent, ...] = ()
        moves_played = 0
        round_index = 0
        timed_out = False
        last_dice = 0

        # 初态即空（宝藏全被拾取）：不掷骰、不移动、不对战，直接终局。
        if state.true_map.is_finished():
            over = GameEvent.game_over()
            events.append(over)
            return self._build_result(
                mode=mode, state=state, events=events, dice_sequence=dice_sequence, stats=stats,
                moves_played=0, timed_out=False,
            )

        while True:
            if round_index >= self._max_turns:
                timed_out = True
                state = dataclasses.replace(state, turn_index=round_index)
                break
            for seat in (Seat.A, Seat.B):
                dice_N = self._roll(round_index, seat)
                dice_sequence.append(dice_N)
                last_dice = dice_N

                view = MapModule(state.true_map).get_view_map(mode)
                state_view = self._public_view(state, seat, round_index, dice_N)
                if io is not None:
                    start_turn = getattr(io, "start_turn", None)
                    if callable(start_turn):
                        start_turn(seat, dice_N, seat in human_seats)
                    # 固定顺序：先 render（携带上一回合的事件），再 next_action。
                    self._call_render(io, view, state_view, pending)

                action = self._decide(
                    seat=seat, policy=policies[seat], state=state, dice_N=dice_N, view=view,
                    state_view=state_view, ai=ai, stats=stats, human_seats=human_seats,
                )
                state, turn_events = self._execute_turn(
                    state=state, seat=seat, dice_N=dice_N, action=action, mode=mode, stats=stats,
                )
                events.extend(turn_events)
                pending = turn_events
                moves_played += 1

                # 终局判定在**终点对战之后**（D11）：拾取最后一枚宝藏的回合照样先结算对战。
                if state.true_map.is_finished():
                    break
            else:
                round_index += 1
                continue
            break

        if state.true_map.is_finished():
            over = GameEvent.game_over()
            events.append(over)
            pending = pending + (over,)

        if io is not None and moves_played > 0:
            # 结算画面：让玩家看到最后一回合的事件与终局后的视图（不含任何未揭示陷阱信息）。
            final_view = MapModule(state.true_map).get_view_map(mode)
            final_view_state = self._public_view(state, state.turn, state.turn_index, last_dice or 1)
            self._call_render(io, final_view, final_view_state, pending)

        return self._build_result(
            mode=mode, state=state, events=events, dice_sequence=dice_sequence, stats=stats,
            moves_played=moves_played, timed_out=timed_out,
        )

    def _execute_turn(
        self,
        *,
        state: GameState,
        seat: Seat,
        dice_N: int,
        action: tuple[Direction, ...],
        mode: GameMode,
        stats: dict[str, object],
    ) -> tuple[GameState, tuple[GameEvent, ...]]:
        """结算一个座位的完整回合：Path 移动 → 更新状态 → **仅终点**一次对战。"""
        before_map = state.true_map
        before_revealed = before_map.revealed_traps

        move = PathModule.sim_player_multi_step(
            before_map, state.position_of(seat), action, dice_N, actor=seat
        )
        turn_events = self._apply_hard_trap_hints(move.events, mode, before_revealed)

        for event in turn_events:
            if event.kind == GameEvent.TREASURE:
                stats[f"treasure_steps_{seat.value.lower()}"] += 1
            elif event.kind == GameEvent.TRAP and event.score_delta < 0:
                # "踏入且扣分"才算一次（重复踩已暴露陷阱不再扣分，也不计数）。
                stats[f"trap_steps_{seat.value.lower()}"] += 1

        pos_a, pos_b = state.pos_a, state.pos_b
        score_a, score_b = state.score_a, state.score_b
        if seat is Seat.A:
            pos_a = move.final_pos
            score_a += move.score_delta
        else:
            pos_b = move.final_pos
            score_b += move.score_delta

        # 对战只在回合移动全部结束后的终点结算一次；中途路过对手不触发（§4.7）。
        battle = BattleModule.trigger_battle(
            move.true_map, pos_a, pos_b, score_a, score_b,
            win_score=self._battle_win_score,
        )
        events = list(turn_events)
        if battle.triggered:
            stats["battles"] += 1
            events.extend(battle.events)
            pos_a, pos_b = battle.pos_a, battle.pos_b
            score_a, score_b = battle.score_a, battle.score_b

        new_state = GameState(
            true_map=move.true_map,
            pos_a=pos_a,
            pos_b=pos_b,
            score_a=score_a,
            score_b=score_b,
            turn=seat.opponent,
            mode=mode,
            turn_index=state.turn_index,
        )
        return new_state, tuple(events)

    @staticmethod
    def _apply_hard_trap_hints(
        path_events: tuple[GameEvent, ...],
        mode: GameMode,
        before_revealed: frozenset,
    ) -> tuple[GameEvent, ...]:
        """HARD：把"本次移动前尚未揭示"的 TRAP 事件改写为隐藏陷阱提示文案。

        ``PathModule`` 与模式无关，只发通用 ``踩到陷阱 -2 分``；模式判定必须由本层完成。
        改写（而非追加第二条 TRAP 事件）保证"恰好一条 TRAP 事件"，且数字取扣分**绝对值**。
        """
        if mode is not GameMode.HARD:
            return tuple(path_events)
        rewritten: list[GameEvent] = []
        for event in path_events:
            if (
                event.kind == GameEvent.TRAP
                and event.position is not None
                and event.position not in before_revealed
            ):
                rewritten.append(
                    GameEvent.trap(event.actor, event.position, event.score_delta, hidden=True)
                )
            else:
                rewritten.append(event)
        return tuple(rewritten)

    # ------------------------------------------------------------------ 决策
    def _decide(
        self,
        *,
        seat: Seat,
        policy: SeatPolicy,
        state: GameState,
        dice_N: int,
        view: ViewMap,
        state_view: PublicStateView,
        ai: object | None,
        stats: dict[str, object],
        human_seats: frozenset[Seat],
    ) -> tuple[Direction, ...]:
        """取本回合动作序列：人工走 ``GameIO``，机器走 ``AiModule``（两者都只在此处路由）。"""
        if policy is SeatPolicy.HUMAN:
            if seat not in human_seats:  # pragma: no cover - 路由自洽性守卫
                raise IllegalStateError(f"座位 {seat} 被标记为人工但不在人工集合内")
            raw = self._human_action(seat, dice_N, view, state_view)
            return self._normalize_action(raw, source=f"座位 {seat} 的人工输入")

        if ai is None:  # pragma: no cover - 由 _resolve_ai 保证
            raise IllegalStateError(f"座位 {seat} 需要机器决策，但 AI 模块不可用")
        calculation = getattr(self._io, "ai_calculation", None)
        if callable(calculation):
            calculation(state_view, None)
        started = time.perf_counter_ns()
        if policy is SeatPolicy.GREEDY:
            raw = ai.get_greedy_move(state, dice_N, seat)  # type: ignore[attr-defined]
        else:  # OPT / OPT_PARTITION 走同一个 OPT 引擎（分治与否由 AiModule 阈值决定）
            raw = ai.get_opt_move(state, dice_N, seat)  # type: ignore[attr-defined]
        elapsed_ns = time.perf_counter_ns() - started
        stats["ai_elapsed_ns"] += elapsed_ns
        stats["ai_calls"] += 1
        self._collect_diagnostics(ai, stats)
        action = self._normalize_action(raw, source=f"座位 {seat} 的机器决策")
        if callable(calculation):
            getter = getattr(ai, "get_calculation_details", None)
            details = tuple(getter()) if callable(getter) else (
                "当前 AI 未提供搜索明细。",
                "最终动作：" + (" ".join(step.value for step in action) or "end（停留）"),
            )
            calculation(state_view, details + (f"本轮决策总耗时：{elapsed_ns / 1e6:.3f} ms。",))
        return action

    def _human_action(
        self, seat: Seat, dice_N: int, view: ViewMap, state_view: PublicStateView
    ) -> object:
        """调用 ``GameIO.next_action``；EOF/Ctrl-C/返回 None 一律转换为受控中止。"""
        assert self._io is not None
        try:
            raw = self._call_next_action(self._io, seat, dice_N, view, state_view)
        except HumanAbortError:
            raise
        except (EOFError, KeyboardInterrupt) as exc:
            raise HumanAbortError(
                f"人工输入已中止（{type(exc).__name__}）：本局不计胜负"
            ) from exc
        if raw is None:
            raise HumanAbortError("人工输入已取消（next_action 返回 None）：本局不计胜负")
        return raw

    def _call_next_action(
        self,
        io: GameIO,
        seat: Seat,
        dice_N: int,
        view: ViewMap,
        state_view: PublicStateView,
    ) -> object:
        """按 06 §2 冻结签名调用：``next_action(seat, dice_N, view, state_view)``。"""
        return io.next_action(seat, dice_N, view, state_view)

    def _call_render(
        self,
        io: GameIO,
        view: ViewMap,
        state_view: PublicStateView,
        events: tuple[GameEvent, ...],
    ) -> None:
        io.render(view, state_view, tuple(events))

    @staticmethod
    def _collect_diagnostics(ai: object, stats: dict[str, object]) -> None:
        """累计一次决策的 DP 命中/未命中、节点数与近似标志（事后读，绝不重置 AI 状态）。"""
        getter = getattr(ai, "get_diagnostics", None)
        if not callable(getter):
            return
        try:
            diagnostics = getter()
        except Exception:  # noqa: BLE001 - 诊断读取失败不得破坏对局（实验口径另有守卫）
            return
        if diagnostics is None:  # pragma: no cover - 契约要求返回 SearchDiagnostics
            return
        stats["cache_hits"] += int(getattr(diagnostics, "cache_hits", 0) or 0)
        stats["cache_misses"] += int(getattr(diagnostics, "cache_misses", 0) or 0)
        stats["nodes"] += int(getattr(diagnostics, "nodes", 0) or 0)
        algorithm = str(getattr(diagnostics, "algorithm", "") or "")
        if bool(getattr(diagnostics, "approximate", False)) or algorithm == "opt+partition":
            stats["partition_used"] = True

    @staticmethod
    def _normalize_action(raw: object, *, source: str) -> tuple[Direction, ...]:
        """把动作序列规范化为 ``tuple[Direction, ...]``；接受 ``Direction`` 或 ``w/a/s/d`` 字符。"""
        if isinstance(raw, (str, bytes)):
            raise ValueError(f"{source} 必须是 Direction 序列，不能是字符串（{raw!r}）")
        if not isinstance(raw, collections.abc.Iterable):
            raise ValueError(f"{source} 必须是 Direction 序列，收到 {type(raw).__name__}")
        normalized: list[Direction] = []
        for index, item in enumerate(raw):
            if isinstance(item, Direction):
                normalized.append(item)
                continue
            if isinstance(item, str):
                try:
                    normalized.append(Direction.from_char(item))
                except ValueError as exc:
                    raise ValueError(f"{source} 第 {index} 项非法方向字符：{item!r}") from exc
                continue
            raise ValueError(
                f"{source} 第 {index} 项必须是 Direction，收到 {type(item).__name__}：{item!r}"
            )
        return tuple(normalized)

    # ------------------------------------------------------------------ 骰子 / 视图
    def _roll(self, round_index: int, seat: Seat) -> int:
        """掷骰：注入表按 ``turn_index*2 + (0 for A else 1)`` 取模；否则用注入 RNG 均匀 1..6。

        注入表存在时**完全不触碰** ``rng``，因此搜索/骰子的 RNG 互不干扰（W4 口径）。
        """
        if self._dice_seed_table is None:
            return self._rng.randint(self._dice_min, self._dice_max)
        index = (round_index * 2 + (0 if seat is Seat.A else 1)) % len(self._dice_seed_table)
        return self._dice_seed_table[index]

    @staticmethod
    def _public_view(
        state: GameState, seat: Seat, round_index: int, dice_N: int
    ) -> PublicStateView:
        """人工界面唯一可见状态：绝不含真值图，也不含未揭示陷阱坐标。"""
        return PublicStateView(
            mode=state.mode,
            pos_a=state.pos_a,
            pos_b=state.pos_b,
            score_a=state.score_a,
            score_b=state.score_b,
            turn=seat,
            turn_index=round_index,
            dice_N=dice_N,
            remaining_treasure_count=len(state.true_map.remaining_treasures),
        )

    def _resolve_ai(
        self, machine_seats: frozenset[Seat], policies: dict[Seat, SeatPolicy]
    ) -> object | None:
        """取本次运行使用的 AI 模块：注入优先；否则按 06 §3 默认参数新建（每次运行新建）。"""
        if not machine_seats:
            return None
        if self._injected_ai is not None:
            return self._injected_ai
        return _import_ai_module()()

    # ------------------------------------------------------------------ 结果
    @staticmethod
    def _new_stats() -> dict[str, object]:
        """一个 *可变* 的累计器；最终展开成 06 §5.5 冻结 metrics。"""
        return {
            "battles": 0,
            "trap_steps_a": 0,
            "trap_steps_b": 0,
            "treasure_steps_a": 0,
            "treasure_steps_b": 0,
            "ai_calls": 0,
            "ai_elapsed_ns": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "nodes": 0,
            "partition_used": False,
        }

    @staticmethod
    def _build_result(
        *,
        mode: GameMode,
        state: GameState,
        events: list[GameEvent],
        dice_sequence: list[int],
        stats: dict[str, object],
        moves_played: int,
        timed_out: bool,
    ) -> GameResult:
        """组装 ``GameResult``：回合数按"完整轮"计（只走完 A 也算 1 轮），metrics 补齐冻结键。"""
        metrics: dict[str, object] = dict(stats)
        metrics["dice_sequence"] = list(dice_sequence)
        metrics["timed_out"] = bool(timed_out)
        missing = [key for key in METRIC_KEYS if key not in metrics]
        if missing:  # pragma: no cover - 结构性守卫，防止以后改动漏键
            raise IllegalStateError(f"metrics 缺少 06 §5.5 冻结键：{missing}")
        return GameResult(
            mode=mode,
            final_state=state,
            winner=None if timed_out else _winner_of(state.score_a, state.score_b),
            score_a=state.score_a,
            score_b=state.score_b,
            turns=(moves_played + 1) // 2,
            events=tuple(events),
            draw_timeout=bool(timed_out),
            metrics=metrics,
        )
