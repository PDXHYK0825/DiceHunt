"""``OptAI`` / ``OptConfig`` 单元测试（OPT Expecti-Max，spec 用例 2 / 02 §2）。

覆盖：
* spec 第七节 用例 2 与 04-test-plan 用例 2 的三个子场景（深度 1、骰 1、8×8、NORMAL）；
* 根 U 与**无剪枝全枚举**一致；``use_pruning`` / ``use_cache`` / ``chance_pruning`` 等价性；
* ``cache_hits > 0`` 的重复子树场景；记忆化只存精确值；
* 「根预测 = 重放」（随机合法状态上 ``root_final_pos``/``root_score_delta`` 与 Path 一致）；
* 座位规范化（AI 在 A 座位时不交换分数）、诊断时序、确定性。
"""

from __future__ import annotations

import random

import pytest

from treasure_game.battle_module import BattleModule
from treasure_game.map_module import MapModule
from treasure_game.models import (
    Direction,
    GameMode,
    GameState,
    IllegalStateError,
    Position,
    Seat,
    TrueMap,
    ViewMap,
)
from treasure_game.opt_ai import OptAI, OptConfig
from treasure_game.path_module import PathModule

UP = Direction.UP
LEFT = Direction.LEFT
DOWN = Direction.DOWN
RIGHT = Direction.RIGHT


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


def _spec_case_2(
    ai_score: int = 10,
    human_score: int = 0,
    treasure_value: int = 2,
    mode: GameMode = GameMode.NORMAL,
) -> tuple[TrueMap, GameState]:
    """spec 用例 2 夹具：AI B=(3,3)、人工 A=(3,4)，左邻 (3,2)=+v、远处 (7,7)=+2。

    额外陷阱 (0,7)=-2 既不在一步路径上，也用来验证 NORMAL 视图的可见性。
    """
    true_map = _empty_map(
        treasures={Position(3, 2): treasure_value, Position(7, 7): 2},
        traps={Position(0, 7): -2},
    )
    state = GameState(
        true_map=true_map,
        pos_a=Position(3, 4),  # 人工
        pos_b=Position(3, 3),  # AI
        score_a=human_score,
        score_b=ai_score,
        turn=Seat.B,
        mode=mode,
        turn_index=0,
    )
    return true_map, state


def _replay(state: GameState, seat: Seat, dice_N: int, sequence: tuple[Direction, ...]):
    return PathModule.sim_player_multi_step(
        state.true_map, state.position_of(seat), sequence, dice_N, actor=seat
    )


# ---------------------------------------------------------------------------
# spec 用例 2
# ---------------------------------------------------------------------------
def test_spec_case_2_i_ai_ahead_takes_the_fight() -> None:
    """AI 10 / 人工 0：走到 (3,4) 与人工重合 → 回合末对战 AI 胜 +5、人工 -5，ΔU=+10 > 捡宝 +2。"""
    true_map, state = _spec_case_2(ai_score=10, human_score=0)
    ai = OptAI(OptConfig(depth=1, candidate_limit=24))
    sequence = ai.decide(state, 1, Seat.B)

    assert sequence == (RIGHT,)
    assert ai.root_utility() == pytest.approx(20.0)
    assert ai.root_eval() == pytest.approx(-20.0)

    # 收益来源可复算：捡宝 (10+2)-0 = 12；求战 (10+5)-(0-5) = 20；原地不动 = 10。
    battle = BattleModule.trigger_battle(true_map, Position(3, 4), Position(3, 4), 0, 10)
    assert (battle.score_a, battle.score_b) == (-5, 15)
    assert (battle.score_b - battle.score_a) == 20
    assert (10 + 2) - 0 == 12
    assert 20 > 12 > 10  # 主动求战 > 捡宝 > 停步

    diagnostics = ai.diagnostics()
    replay = _replay(state, Seat.B, 1, sequence)
    assert replay.final_pos == Position(3, 4)  # 与人工同一格 → 回合末对战
    assert diagnostics.root_move_seq == sequence
    assert diagnostics.root_final_pos == replay.final_pos
    assert diagnostics.root_score_delta == replay.score_delta == 0
    assert diagnostics.algorithm == "opt"
    assert diagnostics.depth == 1
    assert diagnostics.approximate is False


def test_spec_case_2_i_matches_unpruned_full_enumeration() -> None:
    """根 U 与「无剪枝 + 穷举候选」基线一致（安全剪枝不改变根值）。"""
    _, state = _spec_case_2(ai_score=10, human_score=0)
    for use_pruning in (True, False):
        baseline = OptAI(
            OptConfig(
                depth=1,
                candidate_limit=24,
                exhaustive_candidates=True,
                use_pruning=use_pruning,
                use_cache=False,
            )
        )
        assert baseline.decide(state, 1, Seat.B) == (RIGHT,)
        assert baseline.root_utility() == pytest.approx(20.0)


def test_spec_case_2_ii_ai_behind_avoids_the_fight() -> None:
    """AI 0 / 人工 10：重合会让 AI 败（U 从 -10 掉到 -20）→ 选择 +2 且不重合。"""
    true_map, state = _spec_case_2(ai_score=0, human_score=10)
    ai = OptAI(OptConfig(depth=1, candidate_limit=24))
    sequence = ai.decide(state, 1, Seat.B)

    assert sequence == (LEFT,)
    assert ai.root_utility() == pytest.approx(-8.0)  # (0+2) - 10

    battle = BattleModule.trigger_battle(true_map, Position(3, 4), Position(3, 4), 10, 0)
    assert (battle.score_a, battle.score_b) == (15, -5)  # 人工 +5 / AI -5
    assert (battle.score_b - battle.score_a) == -20  # 重合的 U 明显更差
    assert -8.0 > -10.0 > -20.0  # 捡宝 > 停步 > 求战

    replay = _replay(state, Seat.B, 1, sequence)
    assert replay.final_pos == Position(3, 2) != state.pos_a
    assert replay.score_delta == 2
    diagnostics = ai.diagnostics()
    assert diagnostics.root_final_pos == replay.final_pos
    assert diagnostics.root_score_delta == replay.score_delta


def test_spec_case_2_iii_high_value_treasure_wins() -> None:
    """第三局把 (3,2) 改成 +20 → AI 10/人工 0 时应选 +20 宝藏（U=30 > 求战 20）。"""
    _, state = _spec_case_2(ai_score=10, human_score=0, treasure_value=20)
    ai = OptAI(OptConfig(depth=1, candidate_limit=24))
    sequence = ai.decide(state, 1, Seat.B)
    assert sequence == (LEFT,)
    assert ai.root_utility() == pytest.approx(30.0)
    replay = _replay(state, Seat.B, 1, sequence)
    assert replay.final_pos == Position(3, 2)
    assert replay.score_delta == 20
    assert ai.diagnostics().root_final_pos == replay.final_pos


def test_spec_case_2_normal_view_reveals_untriggered_traps() -> None:
    """NORMAL（与 EASY）视图暴露尚未触发的陷阱；HARD 才隐藏。"""
    true_map, _ = _spec_case_2()
    module = MapModule(true_map)
    assert isinstance(module.get_view_map(GameMode.NORMAL), ViewMap)
    assert module.get_view_map(GameMode.NORMAL).cell_value(0, 7) == -2
    assert module.get_view_map(GameMode.EASY).cell_value(0, 7) == -2
    assert module.get_view_map(GameMode.HARD).cell_value(0, 7) == 0


# ---------------------------------------------------------------------------
# 剪枝 / 缓存 / 机会节点等价性
# ---------------------------------------------------------------------------
def _generated_state(seed: int) -> GameState:
    rng = random.Random(seed)
    true_map, pos_a, pos_b = MapModule.generate(6, 6, walls=6, treasures=3, traps=2, rng=rng)
    return GameState(
        true_map=true_map, pos_a=pos_a, pos_b=pos_b, score_a=2, score_b=1,
        turn=Seat.B, mode=GameMode.NORMAL, turn_index=0,
    )


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_pruning_and_cache_are_exact(seed: int) -> None:
    """同候选集/深度下，开/关剪枝与开/关缓存 → 根 U 与 tie-break 动作完全一致。"""
    state = _generated_state(seed)
    results = {}
    for use_cache in (True, False):
        for use_pruning in (True, False):
            config = OptConfig(
                depth=2,
                candidate_limit=0,  # 不限候选，保证"同候选集"
                use_cache=use_cache,
                use_pruning=use_pruning,
                exhaustive_candidates=True,
            )
            ai = OptAI(config)
            sequence = ai.decide(state, 2, Seat.B)
            diagnostics = ai.diagnostics()
            results[(use_cache, use_pruning)] = (
                ai.root_utility(),
                sequence,
                diagnostics.cache_hits,
                diagnostics.cache_misses,
                diagnostics.nodes,
            )

    reference = results[(True, True)]
    for key, value in results.items():
        assert value[0] == pytest.approx(reference[0], abs=1e-9), key
        assert value[1] == reference[1], key

    # 缓存开关的可观察差异：开缓存有命中、关缓存不统计。
    assert results[(True, True)][2] > 0 and results[(True, True)][3] > 0
    assert results[(False, True)][2] == 0 and results[(False, True)][3] == 0
    # 剪枝应当减少节点数（同一候选集下）。
    assert results[(True, True)][4] <= results[(True, False)][4]


@pytest.mark.parametrize("seed", [3, 4])
def test_chance_interval_pruning_matches_full_enumeration(seed: int) -> None:
    """机会节点区间剪枝（默认关闭）与六面全枚举逐状态同值——可启用性证据。"""
    state = _generated_state(seed)
    outputs = []
    for chance_pruning in (False, True):
        ai = OptAI(
            OptConfig(
                depth=2,
                candidate_limit=0,
                chance_pruning=chance_pruning,
                exhaustive_candidates=True,
            )
        )
        sequence = ai.decide(state, 2, Seat.B)
        outputs.append((ai.root_utility(), sequence, ai.diagnostics().nodes))
    assert outputs[0][0] == pytest.approx(outputs[1][0], abs=1e-9)
    assert outputs[0][1] == outputs[1][1]


def test_cache_hits_come_from_transpositions() -> None:
    """穷举候选里 UP,LEFT 与 LEFT,UP 等互为转置 → 精确 DP 出现命中。"""
    true_map = _empty_map(
        height=6, width=6, treasures={Position(0, 5): 2, Position(5, 0): 4},
        traps={Position(2, 2): -2},
    )
    state = GameState(
        true_map=true_map, pos_a=Position(5, 5), pos_b=Position(2, 2),
        score_a=0, score_b=0, turn=Seat.B, mode=GameMode.NORMAL,
    )
    ai = OptAI(OptConfig(depth=2, candidate_limit=0, exhaustive_candidates=True))
    ai.decide(state, 2, Seat.B)
    diagnostics = ai.diagnostics()
    assert diagnostics.cache_hits > 0
    assert diagnostics.cache_misses > 0
    assert diagnostics.nodes > 0


def test_cache_is_scoped_per_decision() -> None:
    """缓存按每次决策清零：同一状态连续两次 decide 的诊断逐字相同（不会因残留命中而变快）。"""
    state = _generated_state(0)
    ai = OptAI(OptConfig(depth=2, candidate_limit=8))
    first = ai.decide(state, 2, Seat.B)
    first_diagnostics = ai.diagnostics()
    second = ai.decide(state, 2, Seat.B)
    second_diagnostics = ai.diagnostics()
    assert first == second
    assert (
        first_diagnostics.nodes,
        first_diagnostics.cache_hits,
        first_diagnostics.cache_misses,
    ) == (
        second_diagnostics.nodes,
        second_diagnostics.cache_hits,
        second_diagnostics.cache_misses,
    )


def test_dp_key_contains_every_required_component() -> None:
    """白盒核对 02 §2 的精确 DP key：每个成分**单独**变化都必须改变 key。

    注意静态/动态分层（ADR-04）：``remaining_treasures`` / ``active_traps`` / ``revealed_traps``
    是动态层，**不参与** ``map_id`` 指纹，但必须参与 DP key；反之"改静态层"（墙/分值/坐标）
    会改变 ``map_id``。
    """
    from treasure_game.opt_ai import ROLE_CHANCE, ROLE_MAX, ROLE_MIN, _Node, _Search

    def variant_map(**overrides):
        kwargs = {
            "height": 6,
            "width": 6,
            "terrain": [[0] * 6 for _ in range(6)],
            "treasures": {Position(0, 5): 2, Position(5, 0): 4},
            "traps": {Position(2, 2): -2, Position(3, 3): -3},
        }
        kwargs.update(overrides)
        return TrueMap.create(**kwargs)

    base_map = variant_map()
    state = GameState(
        true_map=base_map, pos_a=Position(1, 1), pos_b=Position(2, 2),
        score_a=0, score_b=0, turn=Seat.B, mode=GameMode.NORMAL,
    )
    config = OptConfig(depth=2, candidate_limit=8)
    search = _Search(config, state, Seat.B)
    node = _Node(base_map, Position(1, 1), Position(2, 2), 0, 0)
    base_key = search._key(ROLE_MAX, node, 2, 2)

    # --- 静态层变体：真正不同的 map_id -------------------------------------
    wall_terrain = [[0] * 6 for _ in range(6)]
    wall_terrain[4][4] = 1
    wall_map = variant_map(terrain=wall_terrain)
    assert wall_map.map_id != base_map.map_id  # 静态层确实不同
    other_state = GameState(
        true_map=wall_map, pos_a=Position(1, 1), pos_b=Position(2, 2),
        score_a=0, score_b=0, turn=Seat.B, mode=GameMode.NORMAL,
    )
    other_search = _Search(config, other_state, Seat.B)
    assert other_search._key(ROLE_MAX, _Node(wall_map, Position(1, 1), Position(2, 2), 0, 0), 2, 2) != base_key

    # --- 动态层变体：map_id 不变，但 DP key 必须区分 ------------------------
    dynamic_variants = {
        "remaining_treasures": variant_map(remaining_treasures={Position(0, 5)}),
        "active_traps": variant_map(active_traps={Position(3, 3)}),
        "revealed_traps": variant_map(revealed_traps={Position(3, 3)}),
    }
    for name, other_map in dynamic_variants.items():
        assert other_map.map_id == base_map.map_id, f"{name} 属于动态层，不应改变 map_id"
        other_key = search._key(
            ROLE_MAX, _Node(other_map, Position(1, 1), Position(2, 2), 0, 0), 2, 2
        )
        assert other_key != base_key, f"DP key 未区分 {name}"

    # --- 单个搜索分量变体 ---------------------------------------------------
    variants = {
        "canonical_seat": _Search(config, state, Seat.A)._key(ROLE_MAX, node, 2, 2),
        "node_kind_min": search._key(ROLE_MIN, node, 2, 2),
        "node_kind_chance": search._key(ROLE_CHANCE, node, None, 2),
        "pos_opt": search._key(ROLE_MAX, _Node(base_map, Position(1, 2), Position(2, 2), 0, 0), 2, 2),
        "pos_human": search._key(ROLE_MAX, _Node(base_map, Position(1, 1), Position(2, 3), 0, 0), 2, 2),
        "score_opt": search._key(ROLE_MAX, _Node(base_map, Position(1, 1), Position(2, 2), 1, 0), 2, 2),
        "score_human": search._key(ROLE_MAX, _Node(base_map, Position(1, 1), Position(2, 2), 0, 1), 2, 2),
        "known_dice": search._key(ROLE_MAX, node, 3, 2),
        "remaining_depth": search._key(ROLE_MAX, node, 2, 1),
    }
    for name, key in variants.items():
        assert key != base_key, f"DP key 未区分 {name}"
        assert len(key) == len(base_key)

    # turn（行动座位）由 role + 规范化座位共同决定：A 座位下同一 role 必须换 turn。
    seat_a_search = _Search(config, state, Seat.A)
    assert seat_a_search._key(ROLE_MIN, node, 2, 2) != seat_a_search._key(ROLE_MAX, node, 2, 2)

    # --- 配置分量：candidate_limit / human_model_id / partition_context ------
    # 全部在**构造期**传入（OptConfig 是 frozen，构造后才改属性会绕过 __post_init__）。
    for name, other_config in (
        ("candidate_limit", OptConfig(depth=2, candidate_limit=9)),
        # 显式给 human_model_id → __post_init__ 不覆盖 → key 必须区分
        ("human_model_id", OptConfig(depth=2, candidate_limit=8, human_model_id="另一模型")),
        ("partition_context", OptConfig(depth=2, candidate_limit=8,
                                        partition_context="local:1.1")),
    ):
        other = _Search(other_config, state, Seat.B)
        assert other._key(ROLE_MAX, node, 2, 2) != base_key, f"DP key 未区分 {name}"

    # --- 人类建模方式（Lead 2026-09-28 新增）：两种模型必须产出不同 key --------
    from treasure_game.opt_ai import HUMAN_MODEL_HEURISTIC, HUMAN_MODEL_WORST_CASE

    heuristic = _Search(
        OptConfig(depth=2, candidate_limit=8, human_model=HUMAN_MODEL_HEURISTIC),
        state, Seat.B,
    )
    worst = _Search(
        OptConfig(depth=2, candidate_limit=8, human_model=HUMAN_MODEL_WORST_CASE),
        state, Seat.B,
    )
    assert heuristic.human_model_id != worst.human_model_id, "两种人类模型的标识必须不同"
    assert heuristic._key(ROLE_MAX, node, 2, 2) != worst._key(ROLE_MAX, node, 2, 2), (
        "DP key 必须区分人类建模方式（否则单动作预测与最坏情况的缓存会互相污染）"
    )
    # 同一模型重复构造 → 标识稳定 same key
    again = _Search(
        OptConfig(depth=2, candidate_limit=8, human_model=HUMAN_MODEL_HEURISTIC),
        state, Seat.B,
    )
    assert again._key(ROLE_MAX, node, 2, 2) == heuristic._key(ROLE_MAX, node, 2, 2)
    # 不同启发式策略 → 不同标识与不同 key
    ratio = _Search(
        OptConfig(depth=2, candidate_limit=8, human_model=HUMAN_MODEL_HEURISTIC,
                  human_model_strategy="RATIO"),
        state, Seat.B,
    )
    assert ratio.human_model_id != heuristic.human_model_id
    assert ratio._key(ROLE_MAX, node, 2, 2) != heuristic._key(ROLE_MAX, node, 2, 2)

    # --- mode --------------------------------------------------------------
    hard_state = GameState(
        true_map=base_map, pos_a=Position(1, 1), pos_b=Position(2, 2),
        score_a=0, score_b=0, turn=Seat.B, mode=GameMode.HARD,
    )
    assert _Search(config, hard_state, Seat.B)._key(ROLE_MAX, node, 2, 2) != base_key


# ---------------------------------------------------------------------------
# 根预测 = 重放
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [10, 11, 12, 13, 14, 15])
@pytest.mark.parametrize("seat", [Seat.B, Seat.A])
def test_root_projection_matches_path_replay(seed: int, seat: Seat) -> None:
    state = _generated_state(seed)
    ai = OptAI(OptConfig(depth=2, candidate_limit=8))
    sequence = ai.decide(state, 2, seat)
    diagnostics = ai.diagnostics()
    replay = _replay(state, seat, 2, sequence)
    assert diagnostics.root_move_seq == sequence
    assert diagnostics.root_final_pos == replay.final_pos
    assert diagnostics.root_score_delta == replay.score_delta
    assert replay.valid_steps <= 2


def test_seat_a_is_canonicalised_without_score_swap() -> None:
    """AI 在 A 座位时：规范化只改角色，不交换真实分数（ADR-05/06 §4.10）。"""
    true_map = _empty_map(treasures={Position(3, 2): 2, Position(7, 7): 2})
    ahead = GameState(
        true_map=true_map, pos_a=Position(3, 3), pos_b=Position(3, 4),
        score_a=10, score_b=0, turn=Seat.A, mode=GameMode.NORMAL,
    )
    ai = OptAI(OptConfig(depth=1, candidate_limit=24))
    assert ai.decide(ahead, 1, Seat.A) == (RIGHT,)  # A 领先 → 主动求战
    assert ai.root_utility() == pytest.approx(20.0)

    behind = GameState(
        true_map=true_map, pos_a=Position(3, 3), pos_b=Position(3, 4),
        score_a=0, score_b=10, turn=Seat.A, mode=GameMode.NORMAL,
    )
    ai2 = OptAI(OptConfig(depth=1, candidate_limit=24))
    assert ai2.decide(behind, 1, Seat.A) == (LEFT,)  # A 落后 → 躲避重合去捡 +2
    assert ai2.root_utility() == pytest.approx(-8.0)


# ---------------------------------------------------------------------------
# 搜索深度与机会节点语义
# ---------------------------------------------------------------------------
def test_depth_really_models_the_opponent_reply() -> None:
    """深度 1 只看本回合直接后果；深度 2 起把人类回合算进来（MIN + 骰子机会节点）。

    夹具：AI B=(0,0) 骰 2，人工 A=(4,3) 与 (4,4)=+8 相邻，另有一枚 (0,2)=+2 就在 AI 面前。
    深度 1 → 捡 +2（U=2）；深度 2/3 → 预见到人工下一手必吃 +8（U=2-8=-6）。
    """
    true_map = _empty_map(height=5, width=5, treasures={Position(0, 2): 2, Position(4, 4): 8})
    state = GameState(
        true_map=true_map, pos_a=Position(4, 3), pos_b=Position(0, 0),
        score_a=0, score_b=0, turn=Seat.B, mode=GameMode.NORMAL,
    )
    shallow = OptAI(OptConfig(depth=1, candidate_limit=8))
    assert shallow.decide(state, 2, Seat.B) == (RIGHT, RIGHT)
    assert shallow.root_utility() == pytest.approx(2.0)

    for depth in (2, 3):
        deeper = OptAI(OptConfig(depth=depth, candidate_limit=8))
        assert deeper.decide(state, 2, Seat.B) == (RIGHT, RIGHT)
        assert deeper.root_utility() == pytest.approx(-6.0)
        assert deeper.diagnostics().nodes > shallow.diagnostics().nodes


def test_chance_node_weights_six_dice_equally() -> None:
    """人工在 (4,0)，(4,4)=+12 距离 4：骰 4/5/6 能吃到（3/6），期望 U = -12×3/6 = -6。"""
    true_map = _empty_map(height=5, width=5, treasures={Position(4, 4): 12})
    state = GameState(
        true_map=true_map, pos_a=Position(4, 0), pos_b=Position(0, 0),
        score_a=0, score_b=0, turn=Seat.B, mode=GameMode.NORMAL,
    )
    ai = OptAI(OptConfig(depth=2, candidate_limit=8))
    ai.decide(state, 2, Seat.B)
    assert ai.root_utility() == pytest.approx(-6.0)

    # 深度 1 时还看不到人类回合 → 0；差值正好说明 chance 节点在做 1/6 加权期望。
    shallow = OptAI(OptConfig(depth=1, candidate_limit=8))
    shallow.decide(state, 2, Seat.B)
    assert shallow.root_utility() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 诊断与边界
# ---------------------------------------------------------------------------
def test_diagnostics_before_decide_raises() -> None:
    ai = OptAI()
    with pytest.raises(IllegalStateError):
        ai.diagnostics()
    with pytest.raises(IllegalStateError):
        ai.root_utility()
    with pytest.raises(IllegalStateError):
        ai.root_eval()


def test_dice_zero_returns_empty_with_valid_diagnostics() -> None:
    _, state = _spec_case_2()
    ai = OptAI(OptConfig(depth=2, candidate_limit=8))
    assert ai.decide(state, 0, Seat.B) == ()
    diagnostics = ai.diagnostics()
    assert diagnostics.root_final_pos == Position(3, 3)
    assert diagnostics.root_score_delta == 0


def test_decide_is_deterministic() -> None:
    """同状态同配置重复决策 → 同动作同根 U（ADR-08：搜索不消耗 RNG）。"""
    state = _generated_state(7)
    outputs = []
    for _ in range(3):
        ai = OptAI(OptConfig(depth=2, candidate_limit=8))
        sequence = ai.decide(state, 2, Seat.B)
        outputs.append((sequence, ai.root_utility(), ai.diagnostics().nodes))
    assert outputs[0] == outputs[1] == outputs[2]


def test_config_validation_and_input_guards() -> None:
    _, state = _spec_case_2()
    assert OptConfig().depth == 3
    assert OptConfig().candidate_limit == 24
    assert OptConfig().use_cache is True and OptConfig().use_pruning is True
    assert OptConfig().chance_pruning is False  # 安全初版默认关闭
    with pytest.raises(ValueError):
        OptAI(OptConfig(depth=0)).decide(state, 1, Seat.B)
    with pytest.raises(ValueError):
        OptAI(OptConfig(depth=1)).decide(state, -1, Seat.B)
    with pytest.raises(ValueError):
        OptAI(OptConfig(depth=1)).decide(state, 1, "B")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OptAI("config")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OptAI(OptConfig(depth=1)).decide("state", 1, Seat.B)  # type: ignore[arg-type]
