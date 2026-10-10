"""W3-01 仿真模拟器测试（Owner: simulator-dev）。

覆盖范围
--------
* **spec 用例 3（HARD 隐藏陷阱）**：前视图伪装成空地、踩中扣分且恰好一条 ``TRAP`` 事件、
  文案为冻结的隐藏提示、后视图暴露 + ``spent_traps`` 标记、重复踏入不再扣分、
  踩中前的人工 UI/日志不泄露该陷阱坐标或负值、机器侧始终读真值、同图 NORMAL 一开始可见。
* **spec 用例 2（Simulator 侧）**：固定骰序下 OPT AI 在"捡 +2 宝藏"与"求战 +5/−5"之间选择求战，
  AI 净 +7、对战恰一次。
* **spec 用例 5（Simulator 侧）**：终点重合只结算一次；败方四周全资源格无法推开时仍重合，
  同回合不递归二次结算。
* 回合顺序（先 render 再 next_action）、中途路过对手不对战、最后一枚宝藏先对战后终局（D11）、
  初态即空不掷骰、``max_turns`` 超时、骰序注入与 RNG 隔离、策略路由、
  ``GameResult.metrics`` 冻结键、人工 EOF 受控中止、视图-真值隔离。

所有夹具直接构造 ``TrueMap``/``GameState``，不依赖随机生成器（04 测试计划要求）。
"""

from __future__ import annotations

import io as _io_module
import pathlib
import random
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve()
_PROJECT = _HERE.parents[1]
for _path in (str(_PROJECT), str(_PROJECT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from treasure_game.console_io import ConsoleIO  # noqa: E402
from treasure_game.map_module import MapModule  # noqa: E402
from treasure_game.models import (  # noqa: E402
    Direction,
    GameEvent,
    GameMode,
    GameState,
    IllegalStateError,
    Position,
    PublicStateView,
    SearchDiagnostics,
    Seat,
    TrueMap,
    ViewMap,
)
from treasure_game.simulator import (  # noqa: E402
    METRIC_KEYS,
    HumanAbortError,
    SeatPolicy,
    Simulator,
)

try:  # ai-dev 并行交付：未落地时相关用例自动跳过
    import treasure_game.ai_module as _ai_module  # noqa: F401

    _AI_READY = True
except Exception as _exc:  # noqa: BLE001
    _AI_READY = False
    _AI_DETAIL = f"{type(_exc).__name__}: {_exc}"


# --------------------------------------------------------------------------------------
# 夹具工具
# --------------------------------------------------------------------------------------
def StringIO(initial: str = "") -> _io_module.StringIO:
    """本地 ``StringIO`` 别名（避免与测试内的 ``io`` 变量重名）。"""
    return _io_module.StringIO(initial)


def make_state(
    grid,
    pos_a: Position,
    pos_b: Position,
    *,
    mode: GameMode = GameMode.NORMAL,
    score_a: int = 0,
    score_b: int = 0,
) -> GameState:
    """直接构造初态（不经过随机生成器）。"""
    return GameState(
        true_map=TrueMap.from_grid(grid),
        pos_a=pos_a,
        pos_b=pos_b,
        score_a=score_a,
        score_b=score_b,
        turn=Seat.A,
        mode=mode,
        turn_index=0,
    )


class RecordingIO:
    """只接 ``ViewMap`` + ``PublicStateView`` 的假 GameIO。

    记录调用顺序（验证"先 render 再 next_action"）、每次渲染的视图/公开状态/事件，
    并按座位排队人工指令（用尽后返回空序列 = 直接结束回合）。
    """

    def __init__(self, actions=None) -> None:
        self.actions: dict[Seat, list] = {Seat.A: [], Seat.B: []}
        for seat, queue in (actions or {}).items():
            self.actions[seat] = list(queue)
        self.calls: list[str] = []
        self.views: list[ViewMap] = []
        self.state_views: list[PublicStateView] = []
        self.render_events: list[tuple[GameEvent, ...]] = []
        self.logs: list[str] = []

    # --- GameIO ---
    def render(self, view, state_view, events=()):  # noqa: ANN001
        assert isinstance(view, ViewMap), "render 只能收到 ViewMap"
        assert isinstance(state_view, PublicStateView), "render 只能收到 PublicStateView"
        assert not hasattr(state_view, "true_map"), "PublicStateView 不得携带真值图"
        self.calls.append("render")
        self.views.append(view)
        self.state_views.append(state_view)
        self.render_events.append(tuple(events))

    def next_action(self, seat, dice_N, view, state_view):  # noqa: ANN001
        assert isinstance(view, ViewMap) and isinstance(state_view, PublicStateView)
        self.calls.append(f"next_action:{seat.value}")
        queue = self.actions[seat]
        return queue.pop(0) if queue else ()

    def log(self, message):
        self.logs.append(message)


class ExplodingIO(RecordingIO):
    """next_action 抛出指定异常（验证 EOF/Ctrl-C/None 都转成受控中止）。"""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.exc = exc

    def next_action(self, seat, dice_N, view, state_view):  # noqa: ANN001
        raise self.exc


class NoneIO(RecordingIO):
    def next_action(self, seat, dice_N, view, state_view):  # noqa: ANN001
        return None


class FakeAI:
    """按 06 §3 冻结签名实现的最小 AiModule（断言路由/指标时不依赖 ai-dev 的实现）。"""

    def __init__(
        self,
        *,
        greedy_actions=(),
        opt_actions=(),
        algorithm: str = "opt",
        approximate: bool = False,
        nodes: int = 11,
        cache_hits: int = 3,
        cache_misses: int = 1,
    ) -> None:
        self._greedy = list(greedy_actions)
        self._opt = list(opt_actions)
        self.calls: list[str] = []
        self.seats: list[Seat] = []
        self.states: list[GameState] = []
        self.dice: list[int] = []
        self.algorithm = algorithm
        self.approximate = approximate
        self.nodes = nodes
        self.cache_hits = cache_hits
        self.cache_misses = cache_misses
        self.diagnostics_calls = 0

    def get_greedy_move(self, state, dice_N, seat=Seat.B):
        return self._record("greedy", state, dice_N, seat, self._greedy)

    def get_opt_move(self, state, dice_N, seat=Seat.B):
        return self._record("opt", state, dice_N, seat, self._opt)

    def _record(self, kind, state, dice_N, seat, queue):
        self.calls.append(kind)
        self.seats.append(seat)
        self.states.append(state)
        self.dice.append(dice_N)
        return queue.pop(0) if queue else ()

    def get_diagnostics(self):
        self.diagnostics_calls += 1
        return SearchDiagnostics(
            algorithm=self.algorithm,
            nodes=self.nodes,
            cache_hits=self.cache_hits,
            cache_misses=self.cache_misses,
            elapsed_ns=123,
        )


def trap_events(result) -> list[GameEvent]:
    return [event for event in result.events if event.kind == GameEvent.TRAP]


def battle_events(result) -> list[GameEvent]:
    return [event for event in result.events if event.kind == GameEvent.BATTLE]


def hidden_trap_texts(result) -> list[str]:
    return [event.message for event in trap_events(result) if "隐藏陷阱" in event.message]


# ======================================================================================
# spec 用例 3：HARD 隐藏陷阱（人工踩中 → 扣分 → 暴露；不泄露其他未触发陷阱）
# ======================================================================================
def _hard_case_map() -> TrueMap:
    """8×8：人工 A=(1,1)、陷阱 (1,2)=-2、AI B=(6,6)、远处宝藏 (7,7)=+2。"""
    grid = [[0] * 8 for _ in range(8)]
    grid[1][2] = -2
    grid[7][7] = 2
    return TrueMap.from_grid(grid)


def test_spec_case_3_hard_hidden_trap_reveal() -> None:
    true_map = _hard_case_map()
    trap_pos = Position(1, 2)
    # 前视图（HARD）：未触发陷阱伪装为空地
    assert MapModule(true_map).get_view_map(GameMode.HARD).cell_value(1, 2) == 0
    # 同图 NORMAL：一开始就可见原始负值
    assert MapModule(true_map).get_view_map(GameMode.NORMAL).cell_value(1, 2) == -2

    state = GameState(
        true_map=true_map,
        pos_a=Position(1, 1),
        pos_b=Position(6, 6),
        score_a=0,
        score_b=0,
        turn=Seat.A,
        mode=GameMode.HARD,
        turn_index=0,
    )
    ai = FakeAI()  # 机器座位每回合 end（让对局按 max_turns 收尾）
    # 人工输入：RIGHT 踩陷阱 → LEFT 退回 → RIGHT 二次踏入（不再扣分）→ end 若干
    io = ConsoleIO(
        stdin=StringIO("d\na\nd\nend\nend\nend\nend\nend\n"),
        stdout=StringIO(),
        save_log=False,
    )
    simulator = Simulator(
        initial_state=state,
        io=io,
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=6,
        ai=ai,
        dice_seed_table=[1] * 12,
    )
    result = simulator.run_human_vs_ai(GameMode.HARD)

    # 1) 移动成功、分数 -2、恰好一条 TRAP 事件、文案为冻结的隐藏提示
    traps = trap_events(result)
    assert len(traps) == 1
    assert traps[0].position == trap_pos
    assert traps[0].score_delta == -2, "改写后 score_delta 仍必须是陷阱原始负值"
    assert traps[0].message == "【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】"
    assert hidden_trap_texts(result) == ["【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】"]
    assert result.score_a == -2
    assert result.metrics["trap_steps_a"] == 1

    # 2) 后视图：该格暴露为 -2，并在 spent_traps 中标记
    revealed_view = io.views[1]  # 踩中之后立刻渲染的下一帧（B 回合）
    assert revealed_view.cell_value(1, 2) == -2
    assert trap_pos in revealed_view.spent_traps

    # 3) 前视图不泄露：第一帧该格仍是 0
    assert io.views[0].cell_value(1, 2) == 0

    # 4) 第二次踏入不再扣分（trap_steps_a 仍为 1、分数仍为 -2）
    assert result.metrics["trap_steps_a"] == 1
    assert result.score_a == -2

    # 5) 机器侧始终拥有真值 -2（每次决策收到的状态都必须带该陷阱）
    assert ai.states, "机器座位必须被调用"
    for seen in ai.states:
        assert seen.true_map.trap_values[trap_pos] == -2
        assert seen.true_map.cell_value(trap_pos) == -2

    # 6) 踩中前的人工 UI/日志不得含该陷阱坐标或任何负值：
    #    以"携带隐藏陷阱提示的那一帧开始输出之前"为界（该帧本身已包含踩中后的移动日志）
    hint_frame = next(
        index
        for index, frame in enumerate(io.render_events)
        if any("隐藏陷阱" in event.message for event in frame)
    )
    before = "\n".join(io.transcript[: io.render_marks[hint_frame]])
    assert "(1, 2)" not in before, "踩中前的人工输出泄露了陷阱坐标"
    assert "-2" not in before, "踩中前的人工输出泄露了陷阱负值"
    # 踩中之后才允许出现该坐标/负值（此时陷阱已暴露）
    after = "\n".join(io.transcript[io.render_marks[hint_frame]:])
    assert "-2" in after and "(1, 2)" in after
    # 提示必须真实出现在控制台输出里
    assert "【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】" in io.transcript


def test_hard_hint_only_for_first_reveal_and_only_in_hard() -> None:
    """非 HARD 用通用文案；HARD 首次踩中才用隐藏文案。"""
    grid = [[0, -2, 0, 2]]  # (0,3) 的宝藏保证对局不会"初态即空"

    easy = Simulator(
        initial_state=make_state(grid, Position(0, 0), Position(0, 2), mode=GameMode.EASY),
        io=RecordingIO({Seat.A: [(Direction.RIGHT,)]}),
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=1,
        ai=FakeAI(),
        dice_seed_table=[1] * 2,
    ).run_human_vs_ai(GameMode.EASY)
    assert [event.message for event in trap_events(easy)] == ["踩到陷阱 -2 分"]
    assert hidden_trap_texts(easy) == []

    normal = Simulator(
        initial_state=make_state(grid, Position(0, 0), Position(0, 2), mode=GameMode.NORMAL),
        io=RecordingIO({Seat.A: [(Direction.RIGHT,)]}),
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=1,
        ai=FakeAI(),
        dice_seed_table=[1] * 2,
    ).run_human_vs_ai(GameMode.NORMAL)
    assert [event.message for event in trap_events(normal)] == ["踩到陷阱 -2 分"]

    hard = Simulator(
        initial_state=make_state(grid, Position(0, 0), Position(0, 2), mode=GameMode.HARD),
        io=RecordingIO({Seat.A: [(Direction.RIGHT,)]}),
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=1,
        ai=FakeAI(),
        dice_seed_table=[1] * 2,
    ).run_human_vs_ai(GameMode.HARD)
    assert hidden_trap_texts(hard) == ["【踩到隐藏陷阱！扣除 2 分，陷阱已暴露】"]


def test_hard_hint_skipped_when_trap_already_revealed_before_move() -> None:
    """陷阱在本次移动**之前**已在 revealed_traps 中 → 不是"隐藏"陷阱，用通用文案。"""
    trap_pos = Position(0, 1)
    true_map = TrueMap.create(
        height=1,
        width=4,
        terrain=[[0, 0, 0, 0]],
        treasures={Position(0, 3): 2},
        traps={trap_pos: -2},
        active_traps={trap_pos},
        revealed_traps={trap_pos},
    )
    state = GameState(
        true_map=true_map, pos_a=Position(0, 0), pos_b=Position(0, 2),
        score_a=0, score_b=0, turn=Seat.A, mode=GameMode.HARD, turn_index=0,
    )
    result = Simulator(
        initial_state=state,
        io=RecordingIO({Seat.A: [(Direction.RIGHT,)]}),
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=1,
        ai=FakeAI(),
        dice_seed_table=[1] * 2,
    ).run_human_vs_ai(GameMode.HARD)
    assert [event.message for event in trap_events(result)] == ["踩到陷阱 -2 分"]
    assert result.score_a == -2


# ======================================================================================
# spec 用例 2：OPT AI 权衡"捡宝藏"与"求战"（Simulator 侧，固定骰序）
# ======================================================================================
@pytest.mark.skipif(not _AI_READY, reason="ai_module 尚未落地")
def test_spec_case_2_opt_ai_prefers_winning_battle_with_treasure_pickup() -> None:
    """AI（B，10 分）在 (3,3)：右邻 +2 宝藏 (3,4)，两步外是人类 A(3,5)。

    固定骰序让 B 得到 N=2：``RIGHT, RIGHT`` 先捡 +2 再与人类终点重合 → 求战 +5，
    净收益 +7，严格优于只捡 +2；人工 A 本回合 end（原地不动）。
    """
    from treasure_game.ai_module import AiModule

    grid = [[0] * 8 for _ in range(8)]
    grid[3][4] = 2
    grid[7][7] = 2
    state = make_state(
        grid, Position(3, 5), Position(3, 3), mode=GameMode.NORMAL, score_a=0, score_b=10
    )
    io = RecordingIO()  # A 无指令 → 每回合直接 end（原地不动）
    simulator = Simulator(
        initial_state=state,
        io=io,
        rng=random.Random(12345),  # 有骰表时不得被消耗
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=12,
        ai=AiModule(depth=1, candidate_limit=24, partition_threshold=144),
        dice_seed_table=[1, 2] * 12,  # A 每回合 N=1，B 每回合 N=2
    )
    result = simulator.run_human_vs_ai(GameMode.NORMAL)

    # 第 0 回合结束后的公开状态：A=-5（战败）、B=17（+2 宝藏 +5 对战）→ AI 净 +7
    after_round0 = next(view for view in io.state_views if view.turn_index == 1)
    assert (after_round0.score_a, after_round0.score_b) == (-5, 17), (
        f"B 应选择先捡 (3,4) 的 +2 再走到 A 的终点求战；实际 "
        f"{after_round0.score_a}:{after_round0.score_b}"
    )
    assert result.metrics["treasure_steps_b"] == 1, "AI 必须顺手捡到 +2 宝藏"
    # 同回合绝不重复结算：每一帧携带的事件里最多一条 BATTLE（后续回合可以再次因终点重合而对战）
    for frame in io.render_events:
        assert len([event for event in frame if event.kind == GameEvent.BATTLE]) <= 1
    # 第 0 回合（A 停在原地 + B 一步求战）恰好结算一次，文案为冻结格式
    round0_events = io.render_events[2]  # 第 1 回合 A 的渲染携带"上一回合（B）"的事件
    round0_battles = [event for event in round0_events if event.kind == GameEvent.BATTLE]
    assert len(round0_battles) == 1
    assert round0_battles[0].message == "触发对战：B +5 分，A -5 分"
    # 每触发一次对战恰好产生一条 BATTLE 事件（全程口径一致）
    assert result.metrics["battles"] == len(battle_events(result))


# ======================================================================================
# spec 用例 5：终点重合只结算一次；败方无法推开时仍重合也不递归
# ======================================================================================
def test_spec_case_5_battle_settled_once_and_boxed_loser_stays() -> None:
    """3×3：中心 (1,1) 空且四邻全为资源格（无法推开）；双方终点重合。

    回合 0：A 拿 +4（(0,1)），B 拿 +2（(1,2)）→ 分数 4 : 2。
    回合 1：A 下到中心，B 左到中心 → 终点重合 → A 胜 +5 / B −5，B 四周全资源格无法推开。
    断言：只结算一次（4+5=9、2−5=−3）、对战事件恰一条、双方仍重合（未递归二次结算）。
    """
    grid = [
        [0, 4, 0],
        [2, 0, 2],
        [0, 4, 0],
    ]
    state = make_state(grid, Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    io = RecordingIO({Seat.A: [(Direction.RIGHT,), (Direction.DOWN,)]})
    simulator = Simulator(
        initial_state=state,
        io=io,
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=2,
        ai=FakeAI(opt_actions=[(Direction.DOWN,), (Direction.LEFT,)]),
        dice_seed_table=[1] * 4,
    )
    result = simulator.run_human_vs_ai(GameMode.NORMAL)

    assert result.metrics["battles"] == 1, "同回合不得重复结算对战"
    assert len(battle_events(result)) == 1
    assert (result.score_a, result.score_b) == (9, -3)
    assert result.metrics["treasure_steps_a"] == 1
    assert result.metrics["treasure_steps_b"] == 1
    # 败方四周全资源格 → 推开失败：双方仍重合（最后一帧公开状态）
    final_view = io.state_views[-1]
    assert final_view.pos_a == final_view.pos_b == Position(1, 1)
    # 超过 max_turns 仍有宝藏 → 超时，不判胜负
    assert result.draw_timeout is True and result.winner is None
    assert result.turns == 2


def test_battle_not_triggered_when_passing_opponent_midway() -> None:
    """中途路过对手所在格不触发对战（只有终点重合才结算）。"""
    grid = [[0, 0, 0, 2]]
    state = make_state(grid, Position(0, 0), Position(0, 1), mode=GameMode.NORMAL)
    io = RecordingIO({Seat.A: [(Direction.RIGHT, Direction.RIGHT)]})
    result = Simulator(
        initial_state=state,
        io=io,
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=1,
        ai=FakeAI(),
        dice_seed_table=[2, 1],
    ).run_human_vs_ai(GameMode.NORMAL)

    assert result.metrics["battles"] == 0
    assert battle_events(result) == []
    passed = [
        event
        for event in result.events
        if event.kind == GameEvent.MOVE and event.position == Position(0, 1)
    ]
    assert len(passed) == 1 and passed[0].actor is Seat.A, "A 确实路过了对手所在格"
    assert result.final_state.pos_a == Position(0, 2)


def test_last_treasure_pickup_settles_battle_before_game_over() -> None:
    """D11：拾取最后一枚宝藏的回合先做终点对战，再判定终局。"""
    grid = [[0, 2, 0]]
    state = make_state(grid, Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    io = RecordingIO({Seat.A: [(Direction.RIGHT, Direction.RIGHT)]})
    result = Simulator(
        initial_state=state,
        io=io,
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=5,
        ai=FakeAI(),
        dice_seed_table=[2, 1],
    ).run_human_vs_ai(GameMode.NORMAL)

    kinds = [event.kind for event in result.events]
    assert kinds.index(GameEvent.TREASURE) < kinds.index(GameEvent.BATTLE) < kinds.index(GameEvent.GAME_OVER)
    assert result.metrics["battles"] == 1
    assert (result.score_a, result.score_b) == (7, -5)  # 先 +2 宝藏，再 +5 对战
    assert result.winner == "A"
    assert result.turns == 1
    assert result.final_state.true_map.is_finished()


# ======================================================================================
# 终局 / 超时 / 回合数
# ======================================================================================
def test_initial_state_without_treasures_ends_without_dice_or_battle() -> None:
    """初态即空：不掷骰、不移动、不对战，直接按分数判胜负。"""
    state = make_state(
        [[0, 0], [0, 0]], Position(0, 0), Position(1, 1),
        mode=GameMode.NORMAL, score_a=3, score_b=1,
    )
    io = RecordingIO()
    result = Simulator(state, io=io, policy_a=SeatPolicy.HUMAN, ai=FakeAI()).run_human_vs_ai(
        GameMode.NORMAL
    )
    assert result.turns == 0
    assert result.draw_timeout is False
    assert result.winner == "A"
    assert [event.kind for event in result.events] == [GameEvent.GAME_OVER]
    assert result.metrics["dice_sequence"] == []
    assert result.metrics["battles"] == 0
    assert result.metrics["ai_calls"] == 0
    assert io.calls == [], "初态即空不得要求人工输入或渲染"

    # 平局：分数相同 → winner None
    drawn = Simulator(
        make_state([[0, 0], [0, 0]], Position(0, 0), Position(1, 1), score_a=2, score_b=2),
        ai=FakeAI(),
    ).run_machine_vs_machine()
    assert drawn.winner is None and drawn.turns == 0


def test_max_turns_timeout_sets_draw_timeout_and_no_winner() -> None:
    """max_turns 到顶仍有宝藏 → draw_timeout=True、winner=None、回合数=上限。"""
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    result = Simulator(
        state,
        policy_a=SeatPolicy.GREEDY,
        policy_b=SeatPolicy.OPT,
        max_turns=3,
        ai=FakeAI(),  # 两台机器都不动 → 宝藏永不耗尽
        dice_seed_table=[1] * 6,
    ).run_machine_vs_machine()
    assert result.draw_timeout is True
    assert result.winner is None
    assert result.turns == 3
    assert result.metrics["timed_out"] is True
    assert result.metrics["dice_sequence"] == [1] * 6
    assert result.final_state.turn_index == 3
    assert result.events == ()


@pytest.mark.parametrize("kwargs", [{"max_turns": 0}, {"max_turns": -3}])
def test_invalid_constructor_arguments_raise_value_error(kwargs) -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2))
    with pytest.raises(ValueError):
        Simulator(state, **kwargs)


def test_invalid_policies_states_and_dice_tables_raise() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2))
    with pytest.raises(ValueError):
        Simulator(state, policy_a="HUMAN")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Simulator(state, policy_b=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Simulator("not-a-state")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Simulator(state, dice_seed_table=[])
    with pytest.raises(ValueError):
        Simulator(state, dice_seed_table=[0, 7])
    with pytest.raises(ValueError):
        Simulator(state, dice_seed_table=[1, "2"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        Simulator(state, io=object())
    with pytest.raises(ValueError):
        Simulator(state, rng="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Simulator(state, ai=object())


def test_human_entries_require_io() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2))
    with pytest.raises(IllegalStateError):
        Simulator(state, ai=FakeAI()).run_human_vs_ai(GameMode.EASY)
    with pytest.raises(IllegalStateError):
        Simulator(state, ai=FakeAI()).run_human_vs_human()
    with pytest.raises(ValueError):
        Simulator(state, io=RecordingIO(), ai=FakeAI()).run_human_vs_ai("EASY")  # type: ignore[arg-type]


# ======================================================================================
# 骰序 / RNG 隔离 / 指标
# ======================================================================================
def test_dice_table_is_indexed_by_turn_and_seat_and_is_a_prefix() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    result = Simulator(
        state, policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT, max_turns=4,
        ai=FakeAI(), dice_seed_table=[3, 4],
    ).run_machine_vs_machine()
    # 长度 8 = 4 回合 × 2 座位；按 (turn_index, seat) 取模索引 → [3,4] 交替
    assert result.metrics["dice_sequence"] == [3, 4] * 4

    short = Simulator(
        state, policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT, max_turns=3,
        ai=FakeAI(), dice_seed_table=[5],
    ).run_machine_vs_machine()
    assert short.metrics["dice_sequence"] == [5] * 6


def test_injected_rng_is_used_for_dice_and_not_consumed_with_table() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)

    # 无骰表：骰值等于同一 seed 的 randint(1,6) 序列（按 (turn, seat) 顺序）
    rng = random.Random(20240928)
    expected = [rng.randint(1, 6) for _ in range(6)]
    result = Simulator(
        state, rng=random.Random(20240928), policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT,
        max_turns=3, ai=FakeAI(),
    ).run_machine_vs_machine()
    assert result.metrics["dice_sequence"] == expected
    assert set(expected) <= {1, 2, 3, 4, 5, 6}

    # 有骰表：注入的 rng 完全不消耗（搜索/骰子 RNG 隔离）
    probe = random.Random(4242)
    untouched = random.Random(4242)
    with_table = Simulator(
        state, rng=probe, policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT, max_turns=3,
        ai=FakeAI(), dice_seed_table=[2] * 6,
    ).run_machine_vs_machine()
    assert with_table.metrics["dice_sequence"] == [2] * 6
    assert probe.random() == untouched.random(), "注入骰表时不得消耗 rng"


def test_metrics_fill_all_frozen_keys_with_correct_types() -> None:
    """06 §5.5：13 个冻结键必须齐备，且类型/语义正确（timed_out == draw_timeout）。"""
    grid = [[0, 2, 0, -2], [2, 0, 0, 0]]
    state = make_state(grid, Position(0, 0), Position(1, 3), mode=GameMode.EASY)
    result = Simulator(
        state,
        io=RecordingIO({Seat.A: [(Direction.RIGHT,)]}),
        policy_a=SeatPolicy.HUMAN,
        policy_b=SeatPolicy.OPT,
        max_turns=1,
        ai=FakeAI(greedy_actions=[(Direction.UP,)]),  # EASY → 贪心座位
        dice_seed_table=[1, 1],
    ).run_human_vs_ai(GameMode.EASY)

    metrics = result.metrics
    assert set(metrics) == set(METRIC_KEYS), "metrics 必须恰好填齐 06 §5.5 冻结键"
    assert metrics["treasure_steps_a"] == 1
    assert metrics["trap_steps_b"] == 1
    assert metrics["treasure_steps_b"] == 0
    assert metrics["trap_steps_a"] == 0
    assert metrics["battles"] == 0
    assert metrics["ai_calls"] == 1
    assert metrics["ai_elapsed_ns"] >= 0
    assert metrics["cache_hits"] == 3 and metrics["cache_misses"] == 1 and metrics["nodes"] == 11
    assert metrics["partition_used"] is False
    assert metrics["dice_sequence"] == [1, 1]
    assert metrics["timed_out"] is result.draw_timeout
    for key in (
        "battles", "trap_steps_a", "trap_steps_b", "treasure_steps_a", "treasure_steps_b",
        "ai_calls", "ai_elapsed_ns", "cache_hits", "cache_misses", "nodes",
    ):
        assert type(metrics[key]) is int, key
    assert type(metrics["partition_used"]) is bool
    assert type(metrics["timed_out"]) is bool
    assert isinstance(metrics["dice_sequence"], list)
    # 非 HARD：通用陷阱文案
    assert [event.message for event in trap_events(result)] == ["踩到陷阱 -2 分"]


# ======================================================================================
# 策略路由 / 分治标志
# ======================================================================================
def test_mode_routes_machine_seat_to_greedy_or_opt() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    for mode, expected in (
        (GameMode.EASY, "greedy"),
        (GameMode.NORMAL, "opt"),
        (GameMode.HARD, "opt"),
    ):
        ai = FakeAI()
        Simulator(
            state, io=RecordingIO(), policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT,
            max_turns=1, ai=ai, dice_seed_table=[1, 1],
        ).run_human_vs_ai(mode)
        assert ai.calls == [expected], f"{mode} 的机器座位应由 {expected} 决策"


def test_machine_vs_machine_default_and_injected_policies() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)

    default_ai = FakeAI()
    Simulator(state, max_turns=1, ai=default_ai, dice_seed_table=[1, 1]).run_machine_vs_machine()
    assert default_ai.calls == ["greedy", "opt"], "默认 A=GREEDY、B=OPT"

    injected_ai = FakeAI()
    Simulator(
        state, policy_a=SeatPolicy.OPT, policy_b=SeatPolicy.GREEDY, max_turns=1, ai=injected_ai,
        dice_seed_table=[1, 1],
    ).run_machine_vs_machine()
    assert injected_ai.calls == ["opt", "greedy"], "显式注入的策略优先"

    human_ai = FakeAI()
    Simulator(
        state, io=RecordingIO(), policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.HUMAN,
        max_turns=1, ai=human_ai, dice_seed_table=[1, 1],
    ).run_human_vs_human()
    assert human_ai.calls == [], "人人对战不得调用 AI"


def test_opt_partition_uses_opt_engine_and_reports_diagnostics_partition_flag() -> None:
    """OPT_PARTITION 与 OPT 同引擎；partition_used 只镜像 SearchDiagnostics，绝不伪造。"""
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.HARD)

    approximate = FakeAI(algorithm="opt+partition", approximate=True)
    used = Simulator(
        state, io=RecordingIO(), policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT_PARTITION,
        max_turns=1, ai=approximate, dice_seed_table=[1, 1],
    ).run_human_vs_ai(GameMode.HARD)
    assert approximate.calls == ["opt"]
    assert used.metrics["partition_used"] is True

    exact = FakeAI(algorithm="opt", approximate=False)
    not_used = Simulator(
        state, io=RecordingIO(), policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT_PARTITION,
        max_turns=1, ai=exact, dice_seed_table=[1, 1],
    ).run_human_vs_ai(GameMode.HARD)
    assert exact.calls == ["opt"]
    assert not_used.metrics["partition_used"] is False

    # EASY 仍强制贪心（模式优先于配置）
    easy = FakeAI()
    Simulator(
        state, io=RecordingIO(), policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT_PARTITION,
        max_turns=1, ai=easy, dice_seed_table=[1, 1],
    ).run_human_vs_ai(GameMode.EASY)
    assert easy.calls == ["greedy"]


def test_repeated_runs_from_same_simulator_are_isolated_and_identical() -> None:
    """同一 Simulator 重复运行：事件/指标从头开始（不累加），且结果逐字一致。"""
    grid = [[0, 2, 0, -2], [2, 0, 0, 0]]

    def one_run():
        state = make_state(grid, Position(0, 0), Position(1, 3), mode=GameMode.NORMAL)
        return Simulator(
            state,
            io=RecordingIO({Seat.A: [(Direction.RIGHT,)]}),  # 每次运行用新的 IO（人工队列不跨局复用）
            policy_a=SeatPolicy.HUMAN,
            policy_b=SeatPolicy.OPT,
            max_turns=2,
            ai=FakeAI(opt_actions=[(Direction.LEFT,)]),
            dice_seed_table=[1] * 4,
        ).run_human_vs_ai(GameMode.NORMAL)

    def snapshot(result):
        return (
            result.winner,
            result.score_a,
            result.score_b,
            result.turns,
            [(event.kind, event.actor, event.position, event.score_delta) for event in result.events],
            dict(result.metrics, ai_elapsed_ns=0),
        )

    first = one_run()
    second = one_run()
    assert snapshot(first) == snapshot(second), "同参数重复运行必须完全一致（指标不累加）"
    assert first.metrics["dice_sequence"] == second.metrics["dice_sequence"] == [1] * 4
    assert first.metrics["treasure_steps_a"] == second.metrics["treasure_steps_a"] == 1
    assert first.metrics["trap_steps_b"] == second.metrics["trap_steps_b"] == 0


# ======================================================================================
# 调用顺序 / 视图隔离 / 人工中止
# ======================================================================================
def test_render_is_called_before_next_action_each_turn() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.HARD)
    io = RecordingIO({Seat.A: [(Direction.RIGHT,), (Direction.RIGHT,)]})
    Simulator(
        state, io=io, policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT, max_turns=2,
        ai=FakeAI(), dice_seed_table=[1] * 4,
    ).run_human_vs_ai(GameMode.HARD)
    # 人工座位：render → next_action（严格相邻）
    assert io.calls[:2] == ["render", "next_action:A"]
    # 机器座位：只渲染、不取人工输入
    assert "next_action:B" not in io.calls


def test_io_only_receives_view_and_public_state_and_events_are_public() -> None:
    """视图-真值隔离：公开状态无真值字段；HARD 首帧不含未触发陷阱负值。"""
    grid = [[0, 0, 0], [0, -2, 0], [0, 0, 2]]
    state = make_state(grid, Position(0, 0), Position(1, 0), mode=GameMode.HARD)
    io = RecordingIO({Seat.A: [()]})
    Simulator(
        state, io=io, policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT, max_turns=1,
        ai=FakeAI(), dice_seed_table=[1, 1],
    ).run_human_vs_ai(GameMode.HARD)

    assert io.views and io.state_views
    assert io.views[0].cell_value(1, 1) == 0, "HARD 未触发陷阱必须伪装成空地"
    for view in io.views:
        assert isinstance(view, ViewMap)
        assert not hasattr(view, "trap_values")
    for state_view in io.state_views:
        assert isinstance(state_view, PublicStateView)
        assert not hasattr(state_view, "true_map")
        assert not hasattr(state_view, "active_traps")
        assert not hasattr(state_view, "revealed_traps")
    # 公开事件里不得出现未触发陷阱的坐标
    for events in io.render_events:
        for event in events:
            assert event.position != Position(1, 1)


@pytest.mark.parametrize("exc", [EOFError("eof"), KeyboardInterrupt(), RuntimeError("io 崩了")])
def test_human_abort_and_io_errors(exc: BaseException) -> None:
    """EOF / Ctrl-C → 受控中止（HumanAbortError）；其他异常原样透传（便于定位）。"""
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    simulator = Simulator(
        state, io=ExplodingIO(exc), policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT,
        max_turns=2, ai=FakeAI(), dice_seed_table=[1, 1],
    )
    if isinstance(exc, (EOFError, KeyboardInterrupt)):
        with pytest.raises(HumanAbortError) as info:
            simulator.run_human_vs_ai(GameMode.NORMAL)
        assert isinstance(info.value, IllegalStateError), "受控中止必须是 IllegalStateError 子类"
    else:
        with pytest.raises(RuntimeError):
            simulator.run_human_vs_ai(GameMode.NORMAL)


def test_next_action_returning_none_is_controlled_abort() -> None:
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    simulator = Simulator(
        state, io=NoneIO(), policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT, max_turns=2,
        ai=FakeAI(), dice_seed_table=[1, 1],
    )
    with pytest.raises(HumanAbortError):
        simulator.run_human_vs_ai(GameMode.NORMAL)


def test_invalid_action_element_is_rejected_but_characters_are_accepted() -> None:
    """非法 action 元素必须报错（IO 层负责把非法文本变成重新输入）。"""
    state = make_state([[0, 2, 0]], Position(0, 0), Position(0, 2), mode=GameMode.NORMAL)
    io = RecordingIO({Seat.A: [("x",)]})
    with pytest.raises(ValueError):
        Simulator(
            state, io=io, policy_a=SeatPolicy.HUMAN, policy_b=SeatPolicy.OPT, max_turns=2,
            ai=FakeAI(), dice_seed_table=[1, 1],
        ).run_human_vs_ai(GameMode.NORMAL)

    # 机器座位返回 w/a/s/d 字符时按冻结字符映射接受（等价于 Direction）
    result = Simulator(
        make_state([[0, 2, 0]], Position(0, 0), Position(0, 2)),
        policy_a=SeatPolicy.GREEDY, policy_b=SeatPolicy.OPT, max_turns=1,
        ai=FakeAI(greedy_actions=[("d",)]), dice_seed_table=[1, 1],
    ).run_machine_vs_machine()
    assert result.final_state.pos_a == Position(0, 1)
    assert result.metrics["treasure_steps_a"] == 1
