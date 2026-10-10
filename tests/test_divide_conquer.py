"""``DivideConquerModule`` 单元测试（大地图 OPT 分治近似，02 §3 / ADR-10）。

覆盖：
* 确定性 4×4 近似矩形切块（覆盖完整、不重不漏、可复现）；
* **禁止把块边界当墙**：输出序列在全图 Path 上可重放（步数 ≤ N、``blocked_steps == 0``）；
* 跨块资源真实触发；局部子问题只限制"关注目标集合"，不删真实墙/陷阱/宝藏/对手；
* 阈值路由：``V >= partition_threshold`` 才走分治，``approximate=True``；阈值以下走单体 OPT；
* 与真实 OPT 纯核（经 ``AiModule`` 注入）联动时的"预测 = 重放"。
"""

from __future__ import annotations

import pytest

from treasure_game.ai_module import AiModule
from treasure_game.divide_conquer_module import DivideConquerModule, PartitionReport
from treasure_game.models import (
    Direction,
    GameMode,
    GameState,
    IllegalStateError,
    Position,
    Seat,
    TrueMap,
)
from treasure_game.path_module import PathModule

LEFT = Direction.LEFT
RIGHT = Direction.RIGHT
DOWN = Direction.DOWN


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _empty_map(
    height: int,
    width: int,
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


def _make_stub(record: list[tuple[GameState, int, frozenset[Position]]]):
    """最小局部求解器：返回"到最近允许目标"的全图 BFS 最短路前缀。"""

    def solve_local(
        state: GameState, dice_N: int, allowed_positions: frozenset[Position]
    ) -> tuple[Direction, ...]:
        record.append((state, dice_N, frozenset(allowed_positions)))
        start = state.position_of(Seat.B)
        best: tuple[Direction, ...] | None = None
        for target in sorted(allowed_positions, key=lambda p: (p.row, p.col)):
            path = PathModule.bfs_path(state.true_map, start, target)
            if path is None:
                continue
            if best is None or len(path) < len(best):
                best = path
        return tuple(best[:dice_N]) if best else ()

    return solve_local


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------
def test_blocks_cover_map_exactly_and_are_deterministic() -> None:
    true_map = _empty_map(12, 12)
    module = DivideConquerModule()
    bounds = module.block_bounds(true_map)
    assert len(bounds) == 16
    covered: list[tuple[int, int]] = []
    for row0, row1, col0, col1 in bounds:
        assert row1 > row0 and col1 > col0
        covered.extend((r, c) for r in range(row0, row1) for c in range(col0, col1))
    assert sorted(covered) == sorted(
        (r, c) for r in range(12) for c in range(12)
    )  # 恰好覆盖一次
    assert module.block_bounds(true_map) == bounds  # 确定性
    assert DivideConquerModule(block_rows=2, block_cols=3).block_bounds(true_map) != bounds


def test_blocks_tolerate_uneven_splits() -> None:
    """11×13 也切成 4×4 块：边缘允许不足，但仍不重不漏。"""
    true_map = _empty_map(11, 13)
    module = DivideConquerModule()
    bounds = module.block_bounds(true_map)
    assert len(bounds) == 16
    covered = [(r, c) for row0, row1, col0, col1 in bounds for r in range(row0, row1) for c in range(col0, col1)]
    assert len(covered) == 11 * 13
    assert len(set(covered)) == 11 * 13


# ---------------------------------------------------------------------------
# 全图可重放 / 跨块触发
# ---------------------------------------------------------------------------
def test_solve_output_replays_on_full_map_without_wall_penetration() -> None:
    walls = frozenset({Position(r, 5) for r in range(12) if r not in (5, 6)})
    true_map = _empty_map(
        12, 12, walls=walls,
        treasures={Position(0, 11): 4, Position(11, 0): 4, Position(5, 7): 2, Position(11, 11): 6},
        traps={Position(3, 3): -2},
    )
    state = _state(true_map, Position(0, 0), Position(11, 10))
    record: list[tuple[GameState, int, frozenset[Position]]] = []
    module = DivideConquerModule()
    sequence = module.solve(state, 2, Seat.B, _make_stub(record))
    assert isinstance(sequence, tuple) and len(sequence) <= 2

    replay = PathModule.sim_player_multi_step(true_map, Position(11, 10), sequence, 2, actor=Seat.B)
    assert replay.blocked_steps == 0  # 不穿墙
    assert replay.valid_steps <= 2
    report = module.last_report()
    assert report.used is True
    assert report.blocks == 16


def test_cross_block_resource_is_actually_triggered() -> None:
    """AI 在 (5,5)，宝藏 +8 在 (5,7)（另一个块）→ 合并结果真的走过去并拾取。"""
    true_map = _empty_map(
        12, 12, treasures={Position(5, 7): 8, Position(11, 11): 2}
    )
    state = _state(true_map, Position(0, 0), Position(5, 5))
    module = DivideConquerModule()
    bounds = module.block_bounds(true_map)
    start_block = module.block_index_of(true_map, Position(5, 5), bounds)
    target_block = module.block_index_of(true_map, Position(5, 7), bounds)
    assert start_block != target_block  # 确实跨块

    sequence = module.solve(state, 2, Seat.B, _make_stub([]))
    assert sequence == (RIGHT, RIGHT)
    replay = PathModule.sim_player_multi_step(true_map, Position(5, 5), sequence, 2, actor=Seat.B)
    assert replay.final_pos == Position(5, 7)
    assert replay.score_delta == 8
    assert Position(5, 7) not in replay.true_map.remaining_treasures


def test_local_solver_only_restricts_targets() -> None:
    """注入回调收到的状态与真实状态一致：只多一个"关注目标集合"，其余一律不删。"""
    true_map = _empty_map(
        12, 12,
        walls=frozenset({Position(6, 6)}),
        treasures={Position(1, 1): 4, Position(10, 10): 2, Position(10, 1): 2},
        traps={Position(4, 4): -2},
    )
    state = _state(true_map, Position(0, 0), Position(11, 11))
    record: list[tuple[GameState, int, frozenset[Position]]] = []
    module = DivideConquerModule()
    module.solve(state, 2, Seat.B, _make_stub(record))

    assert record, "solve_local 至少要被调用一次"
    for received_state, dice_N, allowed in record:
        assert received_state is state  # 完整真值状态原样传入
        assert dice_N == 2
        assert allowed  # 非空关注集合
        assert allowed <= state.true_map.remaining_treasures  # 只可能是剩余宝藏的子集
        assert received_state.true_map.trap_values == true_map.trap_values  # 陷阱没被删
        assert received_state.true_map.terrain == true_map.terrain  # 墙没被删
        assert received_state.pos_a == state.pos_a and received_state.pos_b == state.pos_b


def test_report_counts_blocks_and_candidates() -> None:
    true_map = _empty_map(12, 12, treasures={Position(0, 11): 2, Position(11, 0): 2})
    state = _state(true_map, Position(0, 0), Position(6, 6))
    module = DivideConquerModule()
    module.solve(state, 2, Seat.B, _make_stub([]))
    report = module.last_report()
    assert isinstance(report, PartitionReport)
    assert report.used is True
    assert report.blocks == 16
    assert report.blocks_with_resources == 2
    assert report.resource_count == 2
    assert report.local_candidates == 2
    assert report.merged_candidates >= 3  # 空动作 + 两个局部候选
    assert report.elapsed_ns > 0


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------
def test_error_paths() -> None:
    true_map = _empty_map(12, 12, treasures={Position(0, 0): 2})
    state = _state(true_map, Position(5, 5), Position(6, 6))
    module = DivideConquerModule()
    with pytest.raises(IllegalStateError):
        module.last_report()
    with pytest.raises(ValueError):
        module.solve(state, 2, Seat.B, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        module.solve(state, -1, Seat.B, _make_stub([]))
    with pytest.raises(ValueError):
        DivideConquerModule(partition_threshold=0)
    with pytest.raises(ValueError):
        DivideConquerModule(block_rows=-1)

    def bad_solver(state_, dice_N, allowed):
        return ("d",)  # 非 Direction 元素

    with pytest.raises(ValueError):
        module.solve(state, 2, Seat.B, bad_solver)  # type: ignore[arg-type]


def test_solve_local_sequence_is_truncated_to_dice() -> None:
    """注入回调若返回超长序列，合并阶段按骰值截断（步数语义 <= N）。"""
    true_map = _empty_map(12, 12, treasures={Position(11, 11): 2})
    state = _state(true_map, Position(0, 0), Position(6, 6))

    def long_solver(state_, dice_N, allowed):
        return (RIGHT,) * 20

    module = DivideConquerModule()
    sequence = module.solve(state, 2, Seat.B, long_solver)  # type: ignore[arg-type]
    assert len(sequence) <= 2
    replay = PathModule.sim_player_multi_step(true_map, Position(0, 0), sequence, 2, actor=Seat.B)
    assert replay.valid_steps <= 2
    assert replay.blocked_steps == 0


# ---------------------------------------------------------------------------
# 阈值路由（AiModule）与真实 OPT 纯核联动
# ---------------------------------------------------------------------------
def test_threshold_routing() -> None:
    small = _empty_map(8, 8, treasures={Position(0, 7): 2, Position(7, 0): 2}, traps={Position(3, 3): -2})
    small_state = _state(small, Position(0, 0), Position(7, 7))
    module = AiModule(depth=2, candidate_limit=8)  # 64 < 144 → 不分治
    module.get_opt_move(small_state, 2, Seat.B)
    assert module.get_diagnostics().algorithm == "opt"
    assert module.get_diagnostics().approximate is False

    big = _empty_map(12, 12, treasures={Position(0, 11): 2, Position(11, 0): 2}, traps={Position(6, 6): -2})
    big_state = _state(big, Position(0, 0), Position(11, 11))
    module.get_opt_move(big_state, 1, Seat.B)
    assert module.get_diagnostics().algorithm == "opt+partition"
    assert module.get_diagnostics().approximate is True  # 分治 = 近似
    assert module.partition_report() is not None

    no_partition = AiModule(depth=2, candidate_limit=8, use_partition=False)
    no_partition.get_opt_move(big_state, 1, Seat.B)
    assert no_partition.get_diagnostics().algorithm == "opt"
    assert no_partition.get_diagnostics().approximate is False
    assert no_partition.partition_report() is None


def test_partition_with_real_opt_core_predicts_and_replays() -> None:
    walls = frozenset({Position(r, 4) for r in range(12) if r != 6})
    big = _empty_map(
        12, 12, walls=walls,
        treasures={Position(0, 11): 4, Position(11, 0): 4, Position(6, 6): 2},
        traps={Position(2, 2): -2, Position(9, 9): -2},
    )
    state = _state(big, Position(0, 0), Position(11, 11))
    module = AiModule(depth=2, candidate_limit=8)
    sequence = module.get_opt_move(state, 2, Seat.B)
    diagnostics = module.get_diagnostics()
    replay = PathModule.sim_player_multi_step(big, Position(11, 11), sequence, 2, actor=Seat.B)

    assert diagnostics.algorithm == "opt+partition"
    assert diagnostics.approximate is True
    assert diagnostics.nodes > 0
    assert replay.blocked_steps == 0
    assert diagnostics.root_final_pos == replay.final_pos
    assert diagnostics.root_score_delta == replay.score_delta
    assert diagnostics.root_move_seq == sequence
