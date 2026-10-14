"""``PathModule`` / ``models`` 移动内核测试。

覆盖：spec §七 用例 4、docs/04-test-plan.md 用例 4、06 §4 的全部移动不变量
（撞墙不耗步、资源单次触发、真值图不可变、步数语义）、BFS 邻序与最短路、
败方推开候选规则，以及入参校验与视图-真值隔离。
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from treasure_game.models import (
    BLOCKED_MOVE_MESSAGE,
    Direction,
    GameEvent,
    GameMode,
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

BLOCKED_TEXT = "该方向为障碍物，无法通过，不消耗步数"


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------
def _empty_map(
    height: int = 8,
    width: int = 8,
    walls: frozenset[Position] | set[Position] = frozenset(),
    treasures: dict[Position, int] | None = None,
    traps: dict[Position, int] | None = None,
) -> TrueMap:
    """全空地小地图 + 指定墙/资源。"""
    terrain = [[1 if Position(r, c) in walls else 0 for c in range(width)] for r in range(height)]
    return TrueMap.create(
        height=height,
        width=width,
        terrain=terrain,
        treasures=treasures or {},
        traps=traps or {},
    )


def _kinds(result) -> list[str]:
    return [event.kind for event in result.events]


# ---------------------------------------------------------------------------
# spec 用例 4：障碍物不消耗步数
# ---------------------------------------------------------------------------
def test_spec_case_4_blocked_attempt_does_not_spend_die() -> None:
    """起点 (2,1)、(2,3) 是墙、N=3、序列 右右上左 → 有效步 3、终点 (1,1)、分数不变。"""
    true_map = _empty_map(walls={Position(2, 3)})
    result = PathModule.sim_player_multi_step(
        true_map, Position(2, 1), (RIGHT, RIGHT, UP, LEFT), 3
    )

    assert result.valid_steps == 3
    assert result.attempted_steps == 4
    assert result.blocked_steps == 1
    assert result.final_pos == Position(1, 1)
    assert result.score_delta == 0

    # 第 2 条指令（第 2 个事件）是撞墙，文案逐字一致
    blocked = result.events[1]
    assert blocked.kind == "BLOCKED"
    assert blocked.message == "该方向为障碍物，无法通过，不消耗步数"
    assert blocked.message == BLOCKED_MOVE_MESSAGE == BLOCKED_TEXT
    assert blocked.score_delta == 0
    assert blocked.actor is Seat.A

    # 撞墙后仍继续执行剩余指令（UP、LEFT 未被吞掉）
    assert _kinds(result) == ["MOVE", "BLOCKED", "MOVE", "MOVE"]
    assert result.events[0].position == Position(2, 2)
    assert result.events[2].position == Position(1, 2)
    assert result.events[3].position == Position(1, 1)
    assert blocked.position == Position(2, 2)  # BLOCKED 事件坐标 = 未移动的当前坐标（恒在界内）


def test_blocked_event_position_stays_inside_map_on_out_of_bounds_attempt() -> None:
    """越界撞墙时 BLOCKED 事件也不携带越界坐标，人工 UI 可直接渲染。"""
    true_map = _empty_map(height=2, width=2)
    result = PathModule.sim_player_multi_step(true_map, Position(0, 0), (UP, LEFT), 6)
    assert result.blocked_steps == 2
    assert result.valid_steps == 0
    assert result.final_pos == Position(0, 0)
    for event in result.events:
        assert event.kind == "BLOCKED"
        assert event.position is not None
        assert true_map.in_bounds(event.position)


# ---------------------------------------------------------------------------
# 越界 / 连续撞墙 / 步数语义
# ---------------------------------------------------------------------------
def test_out_of_bounds_and_wall_share_rule_and_never_refill_steps() -> None:
    """越界与墙同规则：连续 5 次失败不消耗有效步数，也不自动补步。"""
    true_map = _empty_map(height=3, width=3, walls={Position(1, 0)})
    sequence = (UP, LEFT, UP, LEFT, DOWN, RIGHT, RIGHT, RIGHT)
    result = PathModule.sim_player_multi_step(true_map, Position(0, 0), sequence, 2)

    assert result.blocked_steps == 5  # UP/LEFT/UP/LEFT 越界 + DOWN 撞墙
    assert result.valid_steps == 2
    assert result.attempted_steps == 7  # 第 8 条指令在走满 N 后被忽略
    assert result.final_pos == Position(0, 2)
    assert result.score_delta == 0
    assert [e.kind for e in result.events if e.kind == "BLOCKED"] == ["BLOCKED"] * 5
    assert all(
        e.message == BLOCKED_TEXT for e in result.events if e.kind == "BLOCKED"
    )


def test_stop_at_max_valid_step_ignores_remaining_commands() -> None:
    """成功走满 N 后忽略剩余指令：长序列与截断序列结果完全一致。"""
    true_map = _empty_map(height=4, width=4)
    long_seq = (RIGHT, RIGHT, RIGHT, DOWN, DOWN, LEFT, UP)
    short_seq = (RIGHT, RIGHT, RIGHT)
    long_result = PathModule.sim_player_multi_step(true_map, Position(0, 0), long_seq, 3)
    short_result = PathModule.sim_player_multi_step(true_map, Position(0, 0), short_seq, 3)

    assert long_result == short_result  # frozen dataclass 值相等
    assert long_result.valid_steps == long_result.attempted_steps == 3
    assert long_result.final_pos == Position(0, 3)
    assert _kinds(long_result) == ["MOVE", "MOVE", "MOVE"]


def test_zero_max_valid_step_processes_nothing() -> None:
    """``max_valid_step=0`` 立即结束：不处理任何指令，但仍返回新的 TrueMap。"""
    true_map = _empty_map(height=3, width=3, treasures={Position(0, 1): 2})
    result = PathModule.sim_player_multi_step(true_map, Position(0, 0), (RIGHT, RIGHT), 0)

    assert result.valid_steps == 0
    assert result.attempted_steps == 0
    assert result.blocked_steps == 0
    assert result.final_pos == Position(0, 0)
    assert result.score_delta == 0
    assert result.events == ()
    assert result.true_map == true_map  # 内容等价
    assert result.true_map is not true_map  # 但始终是新的 TrueMap（§4.3）


def test_empty_sequence_is_allowed() -> None:
    """空序列合法：移动阶段直接结束。"""
    true_map = _empty_map(height=3, width=3)
    result = PathModule.sim_player_multi_step(true_map, Position(1, 1), (), 6)
    assert (result.valid_steps, result.attempted_steps, result.final_pos) == (
        0,
        0,
        Position(1, 1),
    )


# ---------------------------------------------------------------------------
# 资源结算：单次触发 + 陷阱永久暴露 + 入参不可变
# ---------------------------------------------------------------------------
def test_resources_trigger_once_and_trap_stays_revealed() -> None:
    """宝藏/陷阱首次踏入立即结算；重复踏入不再变动；陷阱进入 revealed_traps 永久保留。"""
    true_map = TrueMap.from_grid(
        [
            [0, 0, 0, 0, 0],
            [0, 3, 0, -2, 0],
            [0, 0, 0, 0, 0],
        ]
    )
    start = Position(1, 0)
    sequence = (RIGHT, RIGHT, RIGHT, LEFT, LEFT, RIGHT)
    result = PathModule.sim_player_multi_step(true_map, start, sequence, 6)

    assert result.valid_steps == 6
    assert result.score_delta == 1  # +3 宝藏，-2 陷阱
    kinds = _kinds(result)
    assert kinds.count("TREASURE") == 1
    assert kinds.count("TRAP") == 1
    treasure_event = result.events[kinds.index("TREASURE")]
    trap_event = result.events[kinds.index("TRAP")]
    assert treasure_event.message == "拾取宝藏 +3 分"
    assert treasure_event.score_delta == 3
    assert trap_event.message == "踩到陷阱 -2 分"
    assert trap_event.score_delta == -2

    assert result.true_map.remaining_treasures == frozenset()
    assert result.true_map.active_traps == frozenset()
    assert result.true_map.revealed_traps == frozenset({Position(1, 3)})
    assert result.true_map.is_finished() is True

    # 入参真值图内容完全不变（静态层 + 动态层）
    assert true_map.remaining_treasures == frozenset({Position(1, 1)})
    assert true_map.active_traps == frozenset({Position(1, 3)})
    assert true_map.revealed_traps == frozenset()
    assert true_map.cell_value(Position(1, 1)) == 3
    assert true_map.cell_value(Position(1, 3)) == -2
    assert true_map.is_finished() is False
    assert true_map.treasure_total() == 3

    # 已拾取的宝藏格与已暴露的陷阱格再踩：只产生 MOVE，不再加分/扣分
    again = PathModule.sim_player_multi_step(
        result.true_map, Position(1, 2), (RIGHT, LEFT, RIGHT, LEFT), 4
    )
    assert again.score_delta == 0
    assert _kinds(again) == ["MOVE"] * 4
    assert again.true_map.revealed_traps == frozenset({Position(1, 3)})
    assert again.true_map.remaining_treasures == frozenset()


def test_revealed_trap_keeps_negative_cell_value_but_never_deducts_twice() -> None:
    """已触发陷阱在真值视图仍显示负值（暴露用），但不会二次扣分。"""
    true_map = _empty_map(height=2, width=3, traps={Position(0, 1): -4})
    first = PathModule.sim_player_multi_step(true_map, Position(0, 0), (RIGHT,), 1)
    assert first.score_delta == -4
    assert first.true_map.revealed_traps == frozenset({Position(0, 1)})
    assert first.true_map.cell_value(Position(0, 1)) == -4

    second = PathModule.sim_player_multi_step(
        first.true_map, Position(0, 0), (RIGHT,), 1
    )
    assert second.score_delta == 0
    assert second.true_map.revealed_traps == frozenset({Position(0, 1)})


def test_true_map_is_deeply_immutable_and_result_returns_new_object() -> None:
    """§4.3：TrueMap 内部为 tuple/frozenset/MappingProxyType，外部无法就地篡改。"""
    true_map = _empty_map(height=3, width=3, treasures={Position(0, 1): 2}, traps={Position(2, 2): -2})

    assert isinstance(true_map.terrain, tuple)
    assert all(isinstance(row, tuple) for row in true_map.terrain)
    assert isinstance(true_map.remaining_treasures, frozenset)
    assert isinstance(true_map.active_traps, frozenset)
    assert isinstance(true_map.revealed_traps, frozenset)
    assert not isinstance(true_map.treasure_values, dict)
    assert not isinstance(true_map.trap_values, dict)

    with pytest.raises(TypeError):
        true_map.treasure_values[Position(0, 0)] = 9  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        true_map.height = 5  # type: ignore[misc]

    result = PathModule.sim_player_multi_step(true_map, Position(0, 0), (RIGHT,), 1)
    assert result.true_map is not true_map
    assert result.true_map.remaining_treasures == frozenset()
    assert true_map.remaining_treasures == frozenset({Position(0, 1)})

    # 直接按 §2 字段顺序位置传参同样可用（下游手写测试地图的常见写法）
    positional = TrueMap(
        3,
        3,
        ((0, 0, 0), (0, 0, 0), (0, 0, 0)),
        {Position(0, 1): 2},
        {},
        frozenset({Position(0, 1)}),
        frozenset(),
        frozenset(),
    )
    assert positional.height == 3 and positional.width == 3
    assert positional.remaining_treasures == frozenset({Position(0, 1)})
    assert len(positional.map_id) == 16


def test_map_id_is_static_content_fingerprint() -> None:
    """map_id 只由静态层决定：动态拾取宝藏后不变，静态布局不同则不同。"""
    grid = [[0, 0, 0], [0, 2, 0], [0, 0, 0]]
    first = TrueMap.from_grid(grid)
    second = TrueMap.from_grid(grid)
    assert first.map_id == second.map_id
    assert len(first.map_id) == 16

    picked = PathModule.sim_player_multi_step(first, Position(0, 0), (DOWN, RIGHT), 2)
    assert picked.final_pos == Position(1, 1)
    assert picked.true_map.remaining_treasures == frozenset()
    assert picked.true_map.map_id == first.map_id  # 动态状态不参与指纹

    different = TrueMap.from_grid([[0, 0, 0], [0, 0, 0], [0, 0, 0]])
    assert different.map_id != first.map_id


# ---------------------------------------------------------------------------
# BFS
# ---------------------------------------------------------------------------
def test_bfs_distances_respects_walls_and_omits_unreachable_cells() -> None:
    true_map = TrueMap.from_grid(
        [
            [0, 0, 0, 1, 0],
            [0, 1, 0, 1, 0],
            [0, 0, 0, 1, 0],
            [1, 1, 1, 1, 0],
        ]
    )
    distances = PathModule.bfs_distances(true_map, Position(0, 0))

    assert distances[Position(0, 0)] == 0
    assert distances[Position(1, 0)] == 1
    assert distances[Position(2, 2)] == 4
    assert Position(1, 1) not in distances  # 墙
    assert Position(0, 4) not in distances  # 被墙隔开，不可达 → 缺键
    assert Position(3, 4) not in distances
    # 距离表与最短路长度一致
    for target, expected in distances.items():
        path = PathModule.bfs_path(true_map, Position(0, 0), target)
        assert path is not None and len(path) == expected


def test_bfs_path_shortest_with_frozen_neighbor_order() -> None:
    true_map = TrueMap.from_grid([[0, 0], [0, 0]])
    assert PathModule.bfs_path(true_map, Position(0, 0), Position(0, 0)) == ()
    # 两条并列最短路 (RIGHT,DOWN) / (DOWN,RIGHT)：冻结邻序 上/左/下/右 → 先探索 DOWN
    assert PathModule.bfs_path(true_map, Position(0, 0), Position(1, 1)) == (DOWN, RIGHT)
    assert PathModule.bfs_path(true_map, Position(0, 0), Position(1, 0)) == (DOWN,)


def test_bfs_path_returns_none_when_unreachable() -> None:
    true_map = TrueMap.from_grid(
        [
            [0, 1, 0],
            [0, 1, 0],
            [0, 1, 0],
        ]
    )
    assert PathModule.bfs_path(true_map, Position(0, 0), Position(0, 2)) is None
    assert PathModule.bfs_path(true_map, Position(0, 0), Position(2, 0)) == (DOWN, DOWN)


def test_bfs_path_replays_to_target_through_kernel() -> None:
    """BFS 输出序列交给唯一移动内核重放必须精确到达目标（AI 只允许这样用）。"""
    true_map = TrueMap.from_grid(
        [
            [0, 0, 1, 0, 0],
            [0, 1, 0, 0, 0],
            [0, 0, 0, 1, 0],
        ]
    )
    path = PathModule.bfs_path(true_map, Position(0, 0), Position(2, 4))
    assert path is not None
    replay = PathModule.sim_player_multi_step(true_map, Position(0, 0), path, len(path))
    assert replay.final_pos == Position(2, 4)
    assert replay.valid_steps == len(path)
    assert replay.blocked_steps == 0


# ---------------------------------------------------------------------------
# 败方推开候选
# ---------------------------------------------------------------------------
def test_get_adjacent_empty_follows_neighbor_order() -> None:
    """邻序 上、左、下、右：依次被墙堵住后返回下一个候选，全堵返回 None。"""
    open_map = _empty_map(height=3, width=3)
    assert PathModule.get_adjacent_empty(open_map, Position(1, 1)) == Position(0, 1)

    up_walled = _empty_map(height=3, width=3, walls={Position(0, 1)})
    assert PathModule.get_adjacent_empty(up_walled, Position(1, 1)) == Position(1, 0)

    up_left_walled = _empty_map(
        height=3, width=3, walls={Position(0, 1), Position(1, 0)}
    )
    assert PathModule.get_adjacent_empty(up_left_walled, Position(1, 1)) == Position(2, 1)

    only_right_open = _empty_map(
        height=3,
        width=3,
        walls={Position(0, 1), Position(1, 0), Position(2, 1)},
    )
    assert PathModule.get_adjacent_empty(only_right_open, Position(1, 1)) == Position(1, 2)

    fully_walled = _empty_map(
        height=3,
        width=3,
        walls={Position(0, 1), Position(1, 0), Position(2, 1), Position(1, 2)},
    )
    assert PathModule.get_adjacent_empty(fully_walled, Position(1, 1)) is None


def test_get_adjacent_empty_skips_cells_that_ever_held_resources() -> None:
    """已拾取的宝藏格、已暴露的陷阱格都不算"空地"（D4：从未布置资源）。"""
    true_map = TrueMap.create(
        3,
        3,
        [[0, 0, 0], [0, 0, 0], [0, 0, 0]],
        treasures={Position(0, 1): 2},
        traps={Position(1, 0): -2},
        # 宝藏已被拾取、陷阱已触发：静态层仍在，动态层已消耗
        remaining_treasures=frozenset(),
        active_traps=frozenset(),
        revealed_traps=frozenset({Position(1, 0)}),
    )
    # UP=(0,1) 曾是宝藏、LEFT=(1,0) 曾是陷阱 → 跳过，返回 DOWN=(2,1)
    assert PathModule.get_adjacent_empty(true_map, Position(1, 1)) == Position(2, 1)


def test_get_adjacent_empty_skips_out_of_bounds_candidates() -> None:
    """角落起点：越界候选按邻序被跳过，返回界内第一个候选（UP/LEFT 越界 → DOWN）。"""
    true_map = _empty_map(height=2, width=2)
    assert PathModule.get_adjacent_empty(true_map, Position(0, 0)) == Position(1, 0)
    assert PathModule.get_adjacent_empty(true_map, Position(1, 1)) == Position(0, 1)


# ---------------------------------------------------------------------------
# 枚举 / 文案 / 模型契约
# ---------------------------------------------------------------------------
def test_direction_position_seat_contract() -> None:
    assert [d.value for d in Direction] == ["w", "a", "s", "d"]
    assert (UP.delta, LEFT.delta, DOWN.delta, RIGHT.delta) == ((-1, 0), (0, -1), (1, 0), (0, 1))
    assert str(LEFT) == "a"
    assert f"{RIGHT}" == "d"
    assert Direction("w") is UP
    assert Direction.from_char(" S ") is DOWN
    with pytest.raises(ValueError):
        Direction.from_char("q")

    assert Position(2, 3).moved(UP) == Position(1, 3)
    with pytest.raises(ValueError):
        Position(2, 3).moved("w")  # type: ignore[arg-type]

    assert Seat.A.opponent is Seat.B
    assert Seat.B.opponent is Seat.A
    assert str(Seat.A) == "A"


def test_frozen_message_templates_and_event_factories() -> None:
    blocked = GameEvent.blocked(Seat.B, Position(2, 1))
    assert blocked.kind == "BLOCKED" and blocked.actor is Seat.B
    assert blocked.message == "该方向为障碍物，无法通过，不消耗步数"
    assert blocked.score_delta == 0

    treasure = GameEvent.treasure(Seat.A, Position(1, 1), 5)
    assert treasure.kind == "TREASURE" and treasure.message == "拾取宝藏 +5 分"
    assert treasure.score_delta == 5

    trap = GameEvent.trap(Seat.A, Position(1, 1), -3)
    assert trap.kind == "TRAP" and trap.message == "踩到陷阱 -3 分"
    assert trap.score_delta == -3

    hidden = GameEvent.trap(Seat.A, Position(1, 1), -3, hidden=True)
    assert hidden.message == "【踩到隐藏陷阱！扣除 3 分，陷阱已暴露】"
    assert hidden.score_delta == -3

    assert GameEvent.battle(Seat.A, Seat.B).message == "触发对战：A +5 分，B -5 分"
    assert GameEvent.battle(Seat.B, Seat.A, win_score=8).message == "触发对战：B +8 分，A -8 分"
    assert GameEvent.battle_draw().message == "触发对战：分数相同，平局"
    assert GameEvent.game_over().message == "游戏结束：宝藏已全部拾取"


def test_view_map_is_rejected_by_path_module() -> None:
    """§4.6 视图-真值隔离：ViewMap 传入移动内核必须报 TypeError。"""
    view = ViewMap(
        height=2,
        width=2,
        cells=((0, 0), (0, 0)),
        spent_traps=frozenset(),
        mode=GameMode.HARD,
    )
    assert view.cell_value(0, 0) == 0
    assert view.in_bounds(1, 1) is True and view.in_bounds(2, 0) is False
    with pytest.raises(ValueError):
        view.cell_value(5, 5)

    with pytest.raises(TypeError):
        PathModule.sim_player_multi_step(view, Position(0, 0), (RIGHT,), 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PathModule.bfs_distances(view, Position(0, 0))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PathModule.get_adjacent_empty(view, Position(0, 0))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
def test_illegal_arguments_raise_value_error() -> None:
    true_map = _empty_map(height=3, width=3, walls={Position(1, 1)})

    with pytest.raises(ValueError):
        PathModule.sim_player_multi_step(true_map, Position(0, 0), (RIGHT,), -1)
    with pytest.raises(ValueError):
        PathModule.sim_player_multi_step(true_map, Position(-1, 0), (RIGHT,), 1)
    with pytest.raises(ValueError):
        PathModule.sim_player_multi_step(true_map, Position(0, 3), (RIGHT,), 1)
    with pytest.raises(ValueError):
        PathModule.sim_player_multi_step(true_map, Position(1, 1), (RIGHT,), 1)  # 起点是墙
    with pytest.raises(ValueError):
        PathModule.sim_player_multi_step(true_map, Position(0, 0), ("d",), 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PathModule.sim_player_multi_step(true_map, Position(0, 0), (RIGHT, "x"), 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PathModule.sim_player_multi_step(true_map, Position(0, 0), "d", 1)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        PathModule.bfs_distances(true_map, Position(9, 9))
    with pytest.raises(ValueError):
        PathModule.bfs_distances(true_map, Position(1, 1))
    with pytest.raises(ValueError):
        PathModule.bfs_path(true_map, Position(0, 0), Position(1, 1))  # 目标在墙上
    with pytest.raises(ValueError):
        PathModule.bfs_path(true_map, Position(0, 0), Position(3, 3))
    with pytest.raises(ValueError):
        PathModule.get_adjacent_empty(true_map, Position(3, 0))
    with pytest.raises(ValueError):
        PathModule.get_adjacent_empty(true_map, Position(1, 1))


def test_map_constructor_rejects_illegal_structures() -> None:
    from treasure_game.models import MapFormatError

    with pytest.raises(MapFormatError):
        TrueMap.from_grid([[0, 0], [0]])  # 非矩形
    with pytest.raises(MapFormatError):
        TrueMap.from_grid([])  # 空矩阵
    with pytest.raises(MapFormatError):
        TrueMap.from_grid([[0, 1.5]])  # 非整数
    with pytest.raises(ValueError):
        TrueMap.create(2, 2, [[0, 0], [0, 0]], treasures={Position(0, 0): 0})  # 宝藏值必须 >= 1
    with pytest.raises(ValueError):
        TrueMap.create(2, 2, [[0, 0], [0, 0]], traps={Position(0, 0): 0})  # 陷阱值必须 <= -1
    with pytest.raises(ValueError):
        TrueMap.create(2, 2, [[0, 0], [0, 0]], treasures={Position(2, 0): 2})  # 越界
    with pytest.raises(ValueError):
        TrueMap.create(2, 2, [[0, 0], [0, 0]], treasures={Position(0, 0): 2}, traps={Position(0, 0): -2})
    with pytest.raises(ValueError):
        TrueMap.create(2, 2, [[0, 0], [0, 0]], treasures={Position(0, 0): 2},
                       remaining_treasures=frozenset({Position(1, 1)}))  # 非子集


def test_raw_grid_passed_as_terrain_is_rejected() -> None:
    """把"资源矩阵"误当底图传入（未规范化的 list 行）会被立刻拦下，而不是静默生成坏地图。"""
    with pytest.raises(ValueError):
        TrueMap.create(2, 2, [[0, 0], [0, 2]], treasures={Position(1, 1): 2})


def test_true_map_supports_deepcopy_and_pickle() -> None:
    """MappingProxyType 自身不可序列化，因此 TrueMap 提供 __reduce__ 以支持搜索/实验的快照。"""
    import copy
    import pickle

    true_map = _empty_map(height=3, width=3, treasures={Position(0, 1): 2}, traps={Position(2, 2): -2})
    cloned = copy.deepcopy(true_map)
    restored = pickle.loads(pickle.dumps(true_map))

    assert cloned == true_map and cloned is not true_map
    assert restored == true_map
    assert restored.map_id == true_map.map_id
    assert restored.treasure_values == true_map.treasure_values
    # 克隆体可继续驱动内核且互不影响
    result = PathModule.sim_player_multi_step(cloned, Position(0, 0), (RIGHT,), 1)
    assert result.score_delta == 2 and cloned.remaining_treasures == frozenset({Position(0, 1)})


# ---------------------------------------------------------------------------
# 性质测试：随机走位的不变量 + 与独立参考模型互证
# ---------------------------------------------------------------------------
def _reference_play(
    true_map: TrueMap,
    start: Position,
    sequence: tuple[Direction, ...],
    limit: int,
) -> tuple[Position, int, int, int, frozenset[Position], frozenset[Position], frozenset[Position]]:
    """测试内的独立参考实现（故意朴素，用来互证内核）。"""
    pos = start
    score = 0
    valid = 0
    attempted = 0
    remaining = set(true_map.remaining_treasures)
    active = set(true_map.active_traps)
    revealed = set(true_map.revealed_traps)
    for direction in sequence:
        if valid >= limit:
            break
        attempted += 1
        nxt = pos.moved(direction)
        if not true_map.in_bounds(nxt) or true_map.terrain[nxt.row][nxt.col] == 1:
            continue
        pos = nxt
        valid += 1
        if pos in remaining:
            score += true_map.treasure_values[pos]
            remaining.discard(pos)
        if pos in active:
            score += true_map.trap_values[pos]
            active.discard(pos)
            revealed.add(pos)
    return pos, score, valid, attempted, frozenset(remaining), frozenset(active), frozenset(revealed)


def test_property_random_walks_match_reference_model() -> None:
    rng = random.Random(20260707)
    free_checked = 0
    for _ in range(300):
        height, width = 6, 6
        cells = [(r, c) for r in range(height) for c in range(width)]
        rng.shuffle(cells)
        cursor = 0

        grid = [[0] * width for _ in range(height)]
        for _ in range(rng.randint(0, 5)):
            r, c = cells[cursor]
            cursor += 1
            grid[r][c] = 1
        for _ in range(rng.randint(0, 4)):
            r, c = cells[cursor]
            cursor += 1
            grid[r][c] = rng.choice([2, 3, 5])
        for _ in range(rng.randint(0, 4)):
            r, c = cells[cursor]
            cursor += 1
            grid[r][c] = rng.choice([-1, -2, -4])

        true_map = TrueMap.from_grid(grid)
        free_cells = [Position(r, c) for r in range(height) for c in range(width) if grid[r][c] == 0]
        if not free_cells:
            continue
        free_checked += 1
        start = rng.choice(free_cells)
        limit = rng.randint(0, 6)
        sequence = tuple(rng.choice(list(Direction)) for _ in range(rng.randint(0, 12)))

        result = PathModule.sim_player_multi_step(true_map, start, sequence, limit)

        # 步数语义（§4.4）
        assert 0 <= result.valid_steps <= limit
        assert result.valid_steps <= result.attempted_steps <= len(sequence)
        assert result.blocked_steps == sum(1 for e in result.events if e.kind == "BLOCKED")
        assert result.valid_steps == sum(1 for e in result.events if e.kind == "MOVE")

        # 事件自洽：分数增量 = 事件增量之和；资源事件坐标合法且每次结算一次
        assert result.score_delta == sum(event.score_delta for event in result.events)
        resource_hits: set[Position] = set()
        for event in result.events:
            assert event.position is not None and true_map.in_bounds(event.position)
            if event.kind in {"TREASURE", "TRAP"}:
                assert event.position not in resource_hits  # 单次触发
                resource_hits.add(event.position)

        # 入参不变 + 静态指纹不变
        assert true_map.remaining_treasures == frozenset(
            Position(r, c) for r in range(height) for c in range(width) if grid[r][c] >= 2
        )
        assert result.true_map.map_id == true_map.map_id

        # 与独立参考模型互证
        expected = _reference_play(true_map, start, sequence, limit)
        assert (
            result.final_pos,
            result.score_delta,
            result.valid_steps,
            result.attempted_steps,
            result.true_map.remaining_treasures,
            result.true_map.active_traps,
            result.true_map.revealed_traps,
        ) == expected

        # 终点与最后一条 MOVE 事件一致
        moves = [e for e in result.events if e.kind == "MOVE"]
        assert result.final_pos == (moves[-1].position if moves else start)

    assert free_checked > 200
