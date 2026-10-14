"""``AiModule`` 集成测试（模式路由 + 诊断 + 阈值，spec §四 3 / 06 §3）。

覆盖：
* ``get_greedy_move`` / ``get_opt_move`` 与 ``GreedyAI`` / ``OptAI`` 逐字一致（同参数）；
* ``get_diagnostics()`` 时序（未决策 → ``IllegalStateError``）与字段口径；
* 「根预测 = 重放」在随机合法状态上对贪心与 OPT 都成立；
* 视图-真值隔离：两张真图只改"隐藏未触发陷阱"位置 → ``HumanPolicy`` 候选完全一致；
  ``HumanPolicy`` 收到 ``TrueMap``/``GameState`` 必须抛 ``TypeError``；
* HARD 下 AI 永远读真值图（不会踩它"知道"的陷阱）；
* 默认 ``depth=3`` / ``candidate_limit=24`` 在 8×8 上单次决策 < 1s。
"""

from __future__ import annotations

import dataclasses
import random
import time

import pytest

from treasure_game.ai_module import AiModule
from treasure_game.greedy_ai import GreedyAI
from treasure_game.human_policy import (
    HUMAN_MODEL_ID,
    HUMAN_MODEL_ID_WORST_CASE,
    STRATEGIES,
    HumanPolicy,
    human_model_id,
)
from treasure_game.map_module import MapModule
from treasure_game.models import (
    Direction,
    GameMode,
    GameState,
    IllegalStateError,
    Position,
    PublicStateView,
    Seat,
    TrueMap,
)
from treasure_game.opt_ai import OptAI, OptConfig
from treasure_game.path_module import PathModule

RIGHT = Direction.RIGHT
UP = Direction.UP
DOWN = Direction.DOWN


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _empty_map(
    height: int = 8,
    width: int = 8,
    walls: frozenset[Position] = frozenset(),
    treasures: dict[Position, int] | None = None,
    traps: dict[Position, int] | None = None,
) -> TrueMap:
    terrain = [[1 if Position(r, c) in walls else 0 for c in range(width)] for r in range(height)]
    return TrueMap.create(
        height=height, width=width, terrain=terrain, treasures=treasures or {}, traps=traps or {}
    )


def _state(
    true_map: TrueMap,
    pos_a: Position,
    pos_b: Position,
    mode: GameMode = GameMode.NORMAL,
    score_a: int = 0,
    score_b: int = 0,
) -> GameState:
    return GameState(
        true_map=true_map, pos_a=pos_a, pos_b=pos_b, score_a=score_a, score_b=score_b,
        turn=Seat.B, mode=mode, turn_index=0,
    )


def _generated_state(seed: int, size: int = 6) -> GameState:
    rng = random.Random(seed)
    true_map, pos_a, pos_b = MapModule.generate(
        size, size, walls=6, treasures=3, traps=2, rng=rng
    )
    return GameState(
        true_map=true_map, pos_a=pos_a, pos_b=pos_b, score_a=2, score_b=1,
        turn=Seat.B, mode=GameMode.NORMAL, turn_index=0,
    )


def _perf_state() -> GameState:
    """8×8 性能夹具（合法状态：出生点不与资源重叠）。"""
    walls = frozenset(
        {Position(1, 1), Position(1, 2), Position(2, 5), Position(3, 3),
         Position(4, 6), Position(5, 2), Position(6, 4), Position(6, 5)}
    )
    true_map = _empty_map(
        8, 8, walls=walls,
        treasures={Position(0, 7): 2, Position(4, 4): 4, Position(7, 0): 2, Position(3, 6): 6},
        traps={Position(2, 2): -2, Position(5, 5): -3, Position(6, 1): -2},
    )
    return _state(true_map, Position(0, 0), Position(7, 7), score_a=1, score_b=3)


# ---------------------------------------------------------------------------
# 与两套 AI 的一致性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [20, 21, 22])
def test_get_greedy_move_matches_greedy_ai(seed: int) -> None:
    state = _generated_state(seed)
    module = AiModule()
    assert module.get_greedy_move(state, 3, Seat.B) == GreedyAI().decide(state, 3, Seat.B)


@pytest.mark.parametrize("seed", [20, 21, 22])
def test_get_opt_move_matches_opt_ai(seed: int) -> None:
    state = _generated_state(seed)
    module = AiModule(depth=2, candidate_limit=8)
    direct = OptAI(OptConfig(depth=2, candidate_limit=8))
    assert module.get_opt_move(state, 2, Seat.B) == direct.decide(state, 2, Seat.B)
    assert module.get_root_utility() == pytest.approx(direct.root_utility(), abs=1e-9)


def test_get_opt_move_is_never_greedy() -> None:
    state = _perf_state()
    module = AiModule(depth=2, candidate_limit=8)
    module.get_opt_move(state, 2, Seat.B)
    assert module.get_diagnostics().algorithm == "opt"
    assert module.get_diagnostics().depth == 2


# ---------------------------------------------------------------------------
# 诊断时序与字段
# ---------------------------------------------------------------------------
def test_diagnostics_before_any_decision_raises() -> None:
    module = AiModule()
    with pytest.raises(IllegalStateError):
        module.get_diagnostics()
    with pytest.raises(IllegalStateError):
        module.get_root_utility()
    with pytest.raises(IllegalStateError):
        module.get_root_eval()
    with pytest.raises(IllegalStateError):
        module.get_calculation_details()


@pytest.mark.parametrize("algorithm", ["greedy", "opt", "opt+partition"])
def test_calculation_details_match_current_decision(algorithm: str) -> None:
    state = _perf_state()
    module = AiModule(depth=1, candidate_limit=8, partition_threshold=1 if algorithm == "opt+partition" else 144)
    sequence = (module.get_greedy_move if algorithm == "greedy" else module.get_opt_move)(state, 2, Seat.B)
    details = "\n".join(module.get_calculation_details())
    assert f"算法：{algorithm}" in details
    assert "最终动作：" + (" ".join(step.value for step in sequence) or "end（停留）") in details
    assert "搜索统计：节点" in details
    if algorithm == "greedy":
        assert "BFS 距离" in details and "选定目标" in details
    else:
        assert "【选中】" in details and "U =" in details
    if algorithm == "opt+partition":
        assert "分治近似" in details and "合并比较" in details
    # 同一实例下一回合切换算法时不得带入上一轮候选。
    module.get_greedy_move(state, 0, Seat.A)
    next_details = "\n".join(module.get_calculation_details())
    assert "没有可用步数" in next_details and "【选中】" not in next_details


@pytest.mark.parametrize("partition", [False, True])
def test_hard_calculation_details_hide_estimates(partition: bool) -> None:
    state = _perf_state()
    state = dataclasses.replace(state, mode=GameMode.HARD)
    module = AiModule(depth=1, partition_threshold=1 if partition else 144)
    module.get_opt_move(state, 2, Seat.B)
    details = "\n".join(module.get_calculation_details())
    assert "困难模式隐藏" in details and "【选中】" in details
    assert "U =" not in details and "U ≤" not in details
    assert "预计移动终点" not in details and "本轮移动预计得分" not in details


def test_greedy_diagnostics_and_root_projection() -> None:
    state = _perf_state()
    module = AiModule()
    sequence = module.get_greedy_move(state, 3, Seat.B)
    diagnostics = module.get_diagnostics()
    replay = PathModule.sim_player_multi_step(
        state.true_map, state.position_of(Seat.B), sequence, 3, actor=Seat.B
    )
    assert diagnostics.algorithm == "greedy"
    assert diagnostics.depth == 0
    assert diagnostics.approximate is False
    assert diagnostics.root_move_seq == sequence
    assert diagnostics.root_final_pos == replay.final_pos
    assert diagnostics.root_score_delta == replay.score_delta
    with pytest.raises(IllegalStateError):  # 贪心没有期望搜索效用
        module.get_root_utility()


@pytest.mark.parametrize("seed", [30, 31, 32, 33, 34, 35])
@pytest.mark.parametrize("seat", [Seat.B, Seat.A])
def test_root_projection_matches_path_replay(seed: int, seat: Seat) -> None:
    state = _generated_state(seed)
    module = AiModule(depth=2, candidate_limit=8)
    for getter in (module.get_greedy_move, module.get_opt_move):
        sequence = getter(state, 2, seat)
        diagnostics = module.get_diagnostics()
        replay = PathModule.sim_player_multi_step(
            state.true_map, state.position_of(seat), sequence, 2, actor=seat
        )
        assert diagnostics.root_move_seq == sequence
        assert diagnostics.root_final_pos == replay.final_pos
        assert diagnostics.root_score_delta == replay.score_delta
        assert replay.valid_steps <= 2


def test_root_eval_is_negative_utility() -> None:
    state = _perf_state()
    module = AiModule(depth=1, candidate_limit=8)
    module.get_opt_move(state, 2, Seat.B)
    assert module.get_root_eval() == pytest.approx(-module.get_root_utility())


# ---------------------------------------------------------------------------
# 视图-真值隔离（人类模型）
# ---------------------------------------------------------------------------
def _hidden_trap_pair() -> tuple[TrueMap, TrueMap]:
    treasures = {Position(0, 3): 4, Position(7, 7): 2}
    first = _empty_map(8, 8, treasures=dict(treasures), traps={Position(1, 1): -2, Position(6, 6): -3})
    second = _empty_map(8, 8, treasures=dict(treasures), traps={Position(2, 2): -2, Position(5, 5): -3})
    return first, second


def test_hard_hidden_traps_do_not_change_human_candidates() -> None:
    """两张真图只改"隐藏未触发陷阱"的位置 → HARD 视图相同 → 人类候选完全一致。"""
    first, second = _hidden_trap_pair()
    view_first = MapModule(first).get_view_map(GameMode.HARD)
    view_second = MapModule(second).get_view_map(GameMode.HARD)
    assert view_first.cells == view_second.cells  # 视图逐格相同
    assert view_first.cell_value(1, 1) == 0 and view_first.cell_value(2, 2) == 0
    assert first.trap_values != second.trap_values  # AI 的真值输入确实不同

    public = PublicStateView(
        mode=GameMode.HARD, pos_a=Position(0, 0), pos_b=Position(3, 3),
        score_a=0, score_b=0, turn=Seat.A, turn_index=0, dice_N=3,
        remaining_treasure_count=2,
    )
    policy = HumanPolicy("GATHER")
    candidates_first = policy.candidate_actions(view_first, Seat.A, 3, public)
    candidates_second = policy.candidate_actions(view_second, Seat.A, 3, public)
    assert candidates_first == candidates_second
    assert candidates_first[0] == ()  # 空动作恒在首位
    assert len(candidates_first) >= 2


@pytest.mark.parametrize("mode", list(GameMode))
@pytest.mark.parametrize("value", [1, 15, 20])
def test_ai_and_view_policies_collect_one_point_and_rare_treasures(mode, value) -> None:
    true_map = _empty_map(
        2, 4, walls=frozenset({Position(0, 2)}), treasures={Position(0, 1): value}
    )
    state = _state(true_map, Position(1, 3), Position(0, 0), mode=mode)
    module = AiModule(depth=1)
    for sequence in (
        module.get_greedy_move(state, 1, Seat.B), module.get_opt_move(state, 1, Seat.B)
    ):
        assert sequence == (RIGHT,)
        replay = PathModule.sim_player_multi_step(true_map, state.pos_b, sequence, 1, Seat.B)
        assert replay.score_delta == value and replay.true_map.is_finished()

    view = MapModule(true_map).get_view_map(mode)
    public = PublicStateView(
        mode=mode, pos_a=state.pos_a, pos_b=state.pos_b, score_a=0, score_b=0,
        turn=Seat.B, turn_index=0, dice_N=1, remaining_treasure_count=1,
    )
    for strategy in ("GATHER", "RATIO", "CAUTIOUS"):
        assert HumanPolicy(strategy).choose(view, Seat.B, 1, public) == (RIGHT,)


def test_normal_versus_hard_view_only_differs_in_trap_display() -> None:
    """NORMAL 暴露陷阱 / HARD 伪装空地；陷阱**不阻挡通行**，故确定性候选集完全一致。

    视图真正改变的只有"人类知道什么"：候选选定后仍在 TrueMap 上执行并承担真实后果，
    这正是 MIN 的"不知道 ≠ 不会踩"语义（02 §2 防信息泄露）。
    """
    first, _ = _hidden_trap_pair()
    module = MapModule(first)
    hard_view = module.get_view_map(GameMode.HARD)
    normal_view = module.get_view_map(GameMode.NORMAL)
    assert hard_view.cell_value(1, 1) == 0
    assert normal_view.cell_value(1, 1) == -2
    assert hard_view.cells != normal_view.cells

    public = PublicStateView(
        mode=GameMode.NORMAL, pos_a=Position(1, 1), pos_b=Position(3, 3),
        score_a=0, score_b=0, turn=Seat.A, turn_index=0, dice_N=3,
        remaining_treasure_count=2,
    )
    policy = HumanPolicy("GATHER")
    hard = policy.candidate_actions(hard_view, Seat.A, 3, public)
    normal = policy.candidate_actions(normal_view, Seat.A, 3, public)
    assert hard == normal  # 陷阱不阻挡通行 → 路径候选集合不变
    assert hard[0] == ()
    assert all(len(sequence) <= 3 for sequence in hard)


def test_human_policy_type_guard_rejects_truth() -> None:
    first, _ = _hidden_trap_pair()
    view = MapModule(first).get_view_map(GameMode.HARD)
    public = PublicStateView(
        mode=GameMode.HARD, pos_a=Position(0, 0), pos_b=Position(3, 3),
        score_a=0, score_b=0, turn=Seat.A, turn_index=0, dice_N=3,
        remaining_treasure_count=2,
    )
    policy = HumanPolicy()
    with pytest.raises(TypeError):
        policy.candidate_actions(first, Seat.A, 3, public)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.choose(first, Seat.A, 3, public)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.candidate_actions(
            view, Seat.A, 3, _state(first, Position(0, 0), Position(3, 3))  # type: ignore[arg-type]
        )
    assert policy.model_id == "VIEW_HEURISTIC:GATHER:v1"
    # 搜索内部默认把人类预测为**单一启发式策略**（Lead 2026-09-28 变更）：
    # 默认模型标识与"最坏情况策略集"标识是两个不同的字符串，缓存不跨模型复用。
    assert HUMAN_MODEL_ID == "view-heuristic-gather-v1"
    assert HUMAN_MODEL_ID_WORST_CASE == "view-policy-set-v1"
    assert HUMAN_MODEL_ID != HUMAN_MODEL_ID_WORST_CASE


def test_human_policy_candidates_are_deterministic_and_view_only() -> None:
    first, _ = _hidden_trap_pair()
    view = MapModule(first).get_view_map(GameMode.HARD)
    public = PublicStateView(
        mode=GameMode.HARD, pos_a=Position(0, 0), pos_b=Position(0, 5),
        score_a=0, score_b=0, turn=Seat.A, turn_index=0, dice_N=2,
        remaining_treasure_count=2,
    )
    policy = HumanPolicy()
    assert policy.candidate_actions(view, Seat.A, 2, public) == policy.candidate_actions(
        view, Seat.A, 2, public
    )
    for sequence in policy.candidate_actions(view, Seat.A, 2, public):
        assert len(sequence) <= 2
        assert all(isinstance(direction, Direction) for direction in sequence)


def test_hard_ai_reads_true_map_and_avoids_known_trap() -> None:
    """HARD 下 AI 仍读真值图：绕开它会踩到的陷阱，而人类视图把它伪装成空地。"""
    true_map = _empty_map(5, 5, treasures={Position(0, 3): 2}, traps={Position(0, 1): -2})
    state = _state(true_map, Position(4, 4), Position(0, 0), mode=GameMode.HARD)
    module = AiModule(depth=1, candidate_limit=8)
    sequence = module.get_opt_move(state, 2, Seat.B)
    replay = PathModule.sim_player_multi_step(true_map, Position(0, 0), sequence, 2, actor=Seat.B)
    assert replay.score_delta == 0  # 没有踩到 (0,1) 的 -2
    assert replay.final_pos != Position(0, 1)
    assert sequence != (RIGHT, RIGHT)  # 直奔宝藏会踩坑，AI 知道所以不这么走
    view = MapModule(true_map).get_view_map(GameMode.HARD)
    assert view.cell_value(0, 1) == 0  # 人类视图看不到（视图-真值隔离）
    assert true_map.cell_value(Position(0, 1)) == -2  # AI 真值图看得到


# ---------------------------------------------------------------------------
# CAUTIOUS：唯一会读"视图格值正负"的人类策略（NORMAL vs HARD 对照的机制）
# ---------------------------------------------------------------------------
def _corridor(trap_at: Position = Position(2, 2)) -> TrueMap:
    """5×5 走廊：人工 A=(2,0)，宝藏 (2,4)=+6，陷阱 (2,2)=-2，骰 6。

    直路 ``dddd``（4 步，途经陷阱）；绕行 ``wdddsd``（6 步，走第 1 行避开陷阱）都在骰值内。
    """
    return _empty_map(5, 5, treasures={Position(2, 4): 6}, traps={trap_at: -2})


def _corridor_public(mode: GameMode) -> PublicStateView:
    return PublicStateView(
        mode=mode, pos_a=Position(2, 0), pos_b=Position(0, 4),
        score_a=0, score_b=0, turn=Seat.A, turn_index=0, dice_N=6,
        remaining_treasure_count=1,
    )


def test_cautious_model_id_is_exactly_frozen() -> None:
    """experiments-dev 的 CSV ``human_model_id`` 列逐字依赖这个字符串。"""
    assert HumanPolicy("CAUTIOUS").model_id == "VIEW_HEURISTIC:CAUTIOUS:v1"
    assert HumanPolicy("cautious").model_id == "VIEW_HEURISTIC:CAUTIOUS:v1"
    assert HumanPolicy().model_id == "VIEW_HEURISTIC:GATHER:v1"
    assert human_model_id("CAUTIOUS") == "VIEW_HEURISTIC:CAUTIOUS:v1"
    assert "CAUTIOUS" in STRATEGIES


def test_five_existing_strategies_are_all_trap_blind() -> None:
    """既有五种策略只看几何（可通行性/宝藏/坐标）：NORMAL 与 HARD 首个动作完全相同。"""
    true_map = _corridor()
    normal_view = MapModule(true_map).get_view_map(GameMode.NORMAL)
    hard_view = MapModule(true_map).get_view_map(GameMode.HARD)
    assert normal_view.cell_value(2, 2) == -2
    assert hard_view.cell_value(2, 2) == 0
    for name in ("GATHER", "RATIO", "EVADE", "STOP", "MIXED"):
        policy = HumanPolicy(name)
        assert policy.trap_blind is True
        normal_action = policy.choose(normal_view, Seat.A, 6, _corridor_public(GameMode.NORMAL))
        hard_action = policy.choose(hard_view, Seat.A, 6, _corridor_public(GameMode.HARD))
        assert normal_action == hard_action, name
        assert policy.candidate_actions(
            normal_view, Seat.A, 6, _corridor_public(GameMode.NORMAL)
        ) == policy.candidate_actions(hard_view, Seat.A, 6, _corridor_public(GameMode.HARD)), name
    assert HumanPolicy("CAUTIOUS").trap_blind is False


def test_cautious_reacts_to_visible_traps_normal_vs_hard() -> None:
    """NORMAL（陷阱可见）绕行 vs HARD（伪装成 0）踩进去 —— 真实后果由真值决定。"""
    true_map = _corridor()
    normal_view = MapModule(true_map).get_view_map(GameMode.NORMAL)
    hard_view = MapModule(true_map).get_view_map(GameMode.HARD)
    policy = HumanPolicy("CAUTIOUS")

    normal_candidates = policy.candidate_actions(
        normal_view, Seat.A, 6, _corridor_public(GameMode.NORMAL)
    )
    hard_candidates = policy.candidate_actions(
        hard_view, Seat.A, 6, _corridor_public(GameMode.HARD)
    )
    assert normal_candidates != hard_candidates  # 候选集本身就被视图格值影响
    normal_action = policy.choose(normal_view, Seat.A, 6, _corridor_public(GameMode.NORMAL))
    hard_action = policy.choose(hard_view, Seat.A, 6, _corridor_public(GameMode.HARD))
    assert normal_action != hard_action
    assert normal_action == (UP, RIGHT, RIGHT, RIGHT, DOWN, RIGHT)  # 6 步绕行
    assert hard_action == (RIGHT, RIGHT, RIGHT, RIGHT)  # 看不见陷阱 → 直路 4 步

    # NORMAL：绕开可见陷阱 → 真值重放只吃宝藏，不触发 TRAP
    normal_replay = PathModule.sim_player_multi_step(
        true_map, Position(2, 0), normal_action, 6, actor=Seat.A
    )
    assert normal_replay.final_pos == Position(2, 4)
    assert normal_replay.score_delta == 6
    assert [event.kind for event in normal_replay.events].count("TRAP") == 0

    # HARD：视图上是 0（看不见）→ 直路踩中隐藏陷阱，真值扣 2 分
    hard_replay = PathModule.sim_player_multi_step(
        true_map, Position(2, 0), hard_action, 6, actor=Seat.A
    )
    assert hard_view.cell_value(2, 2) == 0  # 人类视图看不到
    assert true_map.cell_value(Position(2, 2)) == -2  # 真值图有陷阱
    assert hard_replay.score_delta == 4  # +6 -2
    assert [event.kind for event in hard_replay.events].count("TRAP") == 1


def test_cautious_hidden_trap_position_does_not_leak_into_hard_view() -> None:
    """两张真图只改"隐藏未触发陷阱"位置：HARD 下 CAUTIOUS 候选/动作一致，NORMAL 下必须不同。"""
    map_a = _corridor(Position(2, 2))
    map_b = _corridor(Position(2, 3))
    assert map_a.trap_values != map_b.trap_values

    for mode, expect_same in ((GameMode.HARD, True), (GameMode.NORMAL, False)):
        view_a = MapModule(map_a).get_view_map(mode)
        view_b = MapModule(map_b).get_view_map(mode)
        public = _corridor_public(mode)
        cautious = HumanPolicy("CAUTIOUS")
        gather = HumanPolicy("GATHER")
        candidates_a = cautious.candidate_actions(view_a, Seat.A, 6, public)
        candidates_b = cautious.candidate_actions(view_b, Seat.A, 6, public)
        if expect_same:
            assert view_a.cells == view_b.cells  # 未触发陷阱都被伪装 → 视图逐格相同
            assert candidates_a == candidates_b
            assert cautious.choose(view_a, Seat.A, 6, public) == cautious.choose(
                view_b, Seat.A, 6, public
            )
        else:
            assert view_a.cells != view_b.cells
            assert candidates_a != candidates_b
            assert cautious.choose(view_a, Seat.A, 6, public) != cautious.choose(
                view_b, Seat.A, 6, public
            )
        # 陷阱盲策略在两种模式下都不受影响
        assert gather.candidate_actions(view_a, Seat.A, 6, public) == gather.candidate_actions(
            view_b, Seat.A, 6, public
        )


def test_cautious_is_deterministic_and_view_only() -> None:
    true_map = _corridor()
    view = MapModule(true_map).get_view_map(GameMode.NORMAL)
    public = _corridor_public(GameMode.NORMAL)
    policy = HumanPolicy("CAUTIOUS")
    first = policy.candidate_actions(view, Seat.A, 6, public)
    second = policy.candidate_actions(view, Seat.A, 6, public)
    assert first == second
    assert policy.choose(view, Seat.A, 6, public) == policy.choose(view, Seat.A, 6, public)
    assert first[0] == ()  # 空动作恒在首位（"停步"永远是安全候选）
    with pytest.raises(TypeError):
        policy.candidate_actions(true_map, Seat.A, 6, public)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.choose(true_map, Seat.A, 6, public)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 构造参数与默认值
# ---------------------------------------------------------------------------
def test_default_config_and_validation() -> None:
    module = AiModule()
    assert module.config.depth == 3
    assert module.config.candidate_limit == 24
    assert module.config.use_cache is True
    assert module.config.use_pruning is True
    assert module.config.chance_pruning is False
    assert module.partition_threshold == 144
    assert module.use_partition is True
    with pytest.raises(ValueError):
        AiModule(depth=0)
    with pytest.raises(ValueError):
        AiModule(partition_threshold=0)
    with pytest.raises(ValueError):
        AiModule(rng="rng")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AiModule().get_opt_move(_perf_state(), -1, Seat.B)


def test_eight_by_eight_depth3_decision_under_one_second() -> None:
    """默认 depth=3 / K=24 在 8×8 上单次决策 < 1s（报告实测耗时与节点数）。"""
    state = _perf_state()
    module = AiModule()
    start = time.perf_counter()
    sequence = module.get_opt_move(state, 3, Seat.B)
    elapsed = time.perf_counter() - start
    diagnostics = module.get_diagnostics()
    print(
        f"\n[perf] 8x8 depth=3 K=24: {elapsed * 1000:.1f} ms, nodes={diagnostics.nodes}, "
        f"cache_hits={diagnostics.cache_hits}, cache_misses={diagnostics.cache_misses}"
    )
    assert elapsed < 1.0
    assert diagnostics.depth == 3
    assert diagnostics.nodes > 0
    assert diagnostics.cache_hits > 0
    replay = PathModule.sim_player_multi_step(
        state.true_map, state.position_of(Seat.B), sequence, 3, actor=Seat.B
    )
    assert diagnostics.root_final_pos == replay.final_pos
    assert diagnostics.root_score_delta == replay.score_delta
