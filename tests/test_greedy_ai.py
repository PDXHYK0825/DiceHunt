"""``GreedyAI`` 单元测试（EASY 模式贪心，spec 用例 1 / 02 §1）。

覆盖：
* spec 第七节 用例 1 与 04-test-plan 用例 1（8×8、B=(0,0)、A=(0,2)、(0,3)=+6、(2,0)=+4、``dice_N=3``）；
* 比率交叉相乘 + 四级 tie-break（比率 → 分值 → 距离 → 坐标）；
* 人类位置无关（贪心不看对手）、不可达/无宝藏返回空、``dice_N`` 边界；
* EASY 视图原样显示陷阱；唯一移动真相源（Path 重放可核对）；
* 入参校验与视图-真值隔离。
"""

from __future__ import annotations

import pytest

from treasure_game.greedy_ai import GreedyAI
from treasure_game.map_module import MapModule
from treasure_game.models import (
    Direction,
    GameMode,
    GameState,
    Position,
    Seat,
    TrueMap,
    ViewMap,
)
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
    """全空地小地图 + 指定墙/资源（直接构造 ``TrueMap``，不依赖随机生成器）。"""
    terrain = [[1 if Position(r, c) in walls else 0 for c in range(width)] for r in range(height)]
    return TrueMap.create(
        height=height, width=width, terrain=terrain, treasures=treasures or {}, traps=traps or {}
    )


def _state(
    true_map: TrueMap,
    pos_a: Position,
    pos_b: Position,
    mode: GameMode = GameMode.EASY,
    score_a: int = 0,
    score_b: int = 0,
) -> GameState:
    return GameState(
        true_map=true_map, pos_a=pos_a, pos_b=pos_b, score_a=score_a, score_b=score_b,
        turn=Seat.B, mode=mode, turn_index=0,
    )


def _case_1_map() -> TrueMap:
    """spec 用例 1 的 8×8 地图：宝藏 (0,3)=+6、(2,0)=+4；额外陷阱 (5,5)=-2 不在路径上。"""
    return _empty_map(
        treasures={Position(0, 3): 6, Position(2, 0): 4},
        traps={Position(5, 5): -2},
    )


# ---------------------------------------------------------------------------
# spec 用例 1
# ---------------------------------------------------------------------------
def test_spec_case_1_easy_greedy_ratio() -> None:
    """比率相同（6/3 == 4/2 == 2）→ 按"宝藏分值高"破平局选 (0,3)，返回 RIGHT×3。"""
    true_map = _case_1_map()
    state = _state(true_map, Position(0, 2), Position(0, 0))
    distances = PathModule.bfs_distances(true_map, Position(0, 0))
    assert distances[Position(0, 3)] == 3 and distances[Position(2, 0)] == 2
    assert 6 * 2 == 4 * 3  # 交叉相乘：比率确实相等

    sequence = GreedyAI().decide(state, 3, Seat.B)
    assert sequence == (RIGHT, RIGHT, RIGHT)

    replay = PathModule.sim_player_multi_step(true_map, Position(0, 0), sequence, 3, actor=Seat.B)
    assert replay.final_pos == Position(0, 3)
    assert replay.score_delta == 6
    assert replay.valid_steps == 3
    assert replay.blocked_steps == 0


def test_spec_case_1_value_break_tie_reversed() -> None:
    """把两宝藏分值对调 → 比率相同但 (2,0) 分值更高，改选 DOWN×2。"""
    true_map = _empty_map(treasures={Position(0, 3): 4, Position(2, 0): 6})
    state = _state(true_map, Position(0, 2), Position(0, 0))
    sequence = GreedyAI().decide(state, 3, Seat.B)
    assert sequence == (DOWN, DOWN)
    replay = PathModule.sim_player_multi_step(true_map, Position(0, 0), sequence, 3, actor=Seat.B)
    assert replay.final_pos == Position(2, 0)
    assert replay.score_delta == 6


@pytest.mark.parametrize(
    "pos_a",
    [Position(0, 2), Position(0, 4), Position(3, 3), Position(7, 7), Position(7, 0)],
)
def test_spec_case_1_human_position_irrelevant(pos_a: Position) -> None:
    """贪心不看人类座位：人工换位置不改变输出（02 §1 验证点）。"""
    true_map = _case_1_map()
    assert GreedyAI().decide(_state(true_map, pos_a, Position(0, 0)), 3, Seat.B) == (
        RIGHT,
        RIGHT,
        RIGHT,
    )


def test_spec_case_1_easy_view_shows_traps_and_treasures() -> None:
    """EASY 视图原样显示陷阱（未触发也显示 -v）与剩余宝藏。"""
    view = MapModule(_case_1_map()).get_view_map(GameMode.EASY)
    assert isinstance(view, ViewMap)
    assert view.cell_value(5, 5) == -2
    assert view.cell_value(0, 3) == 6
    assert view.cell_value(0, 0) == 0
    assert view.spent_traps == frozenset()


# ---------------------------------------------------------------------------
# tie-break 细则
# ---------------------------------------------------------------------------
def test_tie_break_by_distance_zero_denominator() -> None:
    """比率与分值都相同（距离 0 按 1 计）→ 距离短者优先（站在宝藏上）。"""
    true_map = _empty_map(treasures={Position(3, 3): 4, Position(3, 4): 4})
    target = GreedyAI.choose_target(true_map, Position(3, 3), 3)
    assert target == Position(3, 3)


def test_tie_break_by_position_lexicographic() -> None:
    """比率与距离相同 → 目标 (row, col) 字典序小者优先。"""
    true_map = _empty_map(treasures={Position(3, 1): 4, Position(1, 3): 4})
    distances = PathModule.bfs_distances(true_map, Position(3, 3))
    assert distances[Position(3, 1)] == 2 and distances[Position(1, 3)] == 2
    target = GreedyAI.choose_target(true_map, Position(3, 3), 3)
    assert target == Position(1, 3)  # row 1 < row 3
    assert GreedyAI().decide(_state(true_map, Position(0, 0), Position(3, 3)), 3, Seat.B) == (UP, UP)


def test_tie_break_by_ratio_cross_multiplication() -> None:
    """整数交叉相乘给出的最大收益/距离：6/2=3 胜过 5/2=2.5（避免浮点误差）。"""
    true_map = _empty_map(treasures={Position(0, 2): 6, Position(2, 0): 5})
    target = GreedyAI.choose_target(true_map, Position(0, 0), 3)
    assert target == Position(0, 2)
    assert GreedyAI().decide(_state(true_map, Position(7, 7), Position(0, 0)), 3, Seat.B) == (
        RIGHT,
        RIGHT,
    )


def test_unreachable_treasure_is_skipped() -> None:
    """被墙完全封住的宝藏不可达 → 不作为候选；只有不可达宝藏时返回空序列。"""
    walls = frozenset({Position(0, 1), Position(1, 0), Position(1, 1)})
    true_map = _empty_map(walls=walls, treasures={Position(0, 0): 99})
    state = _state(true_map, Position(7, 7), Position(2, 2))
    assert GreedyAI.choose_target(true_map, Position(2, 2), 3) is None
    assert GreedyAI().decide(state, 3, Seat.B) == ()


def test_no_treasure_returns_empty() -> None:
    true_map = _empty_map()
    assert GreedyAI().decide(_state(true_map, Position(1, 1), Position(0, 0)), 3, Seat.B) == ()


def test_dice_zero_and_short_path() -> None:
    """``dice_N=0`` 返回空；路径比骰子短时返回整条路径（不补步、不绕圈）。"""
    true_map = _empty_map(treasures={Position(0, 2): 4})
    state = _state(true_map, Position(5, 5), Position(0, 0))
    assert GreedyAI().decide(state, 0, Seat.B) == ()
    assert GreedyAI().decide(state, 6, Seat.B) == (RIGHT, RIGHT)
    replay = PathModule.sim_player_multi_step(true_map, Position(0, 0), (RIGHT, RIGHT), 6, actor=Seat.B)
    assert replay.valid_steps == 2 and replay.score_delta == 4


def test_sequence_never_penetrates_walls() -> None:
    """墙必须绕行：序列是 BFS 最短路前缀 → 重放 blocked_steps == 0。

    (0,1) 是墙 → (0,4) 只能走第 1 行绕过去，最短路长度 6；``dice_N=6`` 正好走完。
    """
    walls = frozenset({Position(0, 1)})
    true_map = _empty_map(walls=walls, treasures={Position(0, 4): 8})
    sequence = GreedyAI().decide(_state(true_map, Position(7, 7), Position(0, 0)), 6, Seat.B)
    replay = PathModule.sim_player_multi_step(true_map, Position(0, 0), sequence, 6, actor=Seat.B)
    assert len(sequence) == 6
    assert replay.blocked_steps == 0
    assert replay.final_pos == Position(0, 4)
    assert replay.score_delta == 8


def test_input_validation_and_isolation() -> None:
    true_map = _case_1_map()
    state = _state(true_map, Position(0, 2), Position(0, 0))
    policy = GreedyAI()
    with pytest.raises(ValueError):
        policy.decide(state, -1, Seat.B)
    with pytest.raises(ValueError):
        policy.decide(state, 1.5, Seat.B)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        policy.decide(state, True, Seat.B)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        policy.decide("state", 3, Seat.B)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.decide(
            GameState(
                true_map=MapModule(true_map).get_view_map(GameMode.EASY),  # type: ignore[arg-type]
                pos_a=Position(0, 2), pos_b=Position(0, 0), score_a=0, score_b=0,
                turn=Seat.B, mode=GameMode.EASY,
            ),
            3,
            Seat.B,
        )
    with pytest.raises(TypeError):
        PathModule.sim_player_multi_step(
            MapModule(true_map).get_view_map(GameMode.EASY),  # type: ignore[arg-type]
            Position(0, 0), (RIGHT,), 1, actor=Seat.B,
        )
