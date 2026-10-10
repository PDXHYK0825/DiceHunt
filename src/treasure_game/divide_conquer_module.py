"""分治分块优化（仅大地图 OPT，spec §四 6 / 02 §3 / ADR-10）。

红线（任务书 + 02 §3）：

* **禁止把块边界当墙**：切分只用于"关注资源集合"的划分，不改变全图坐标与可通行性；
  跨块路线一律用**全图** BFS，任何返回序列都由全图
  :meth:`PathModule.sim_player_multi_step` 验证（``valid_steps <= dice_N``、不穿墙）。
* 子问题（由 ``AiModule`` 注入的 ``solve_local`` 回调）只限制"关注的目标资源集合"，
  **不删**真实墙/陷阱/宝藏/对手；局部集合外的资源仍会真实触发。
* 本模块**不得** import ``opt_ai`` / ``ai_module``（避免环）：局部求解能力通过
  ``solve_local(state, dice_N, allowed_positions) -> tuple[Direction, ...]`` 注入。
* 结果是**近似**：``AiModule`` 必须把 ``SearchDiagnostics.approximate`` 置为 ``True``。

合并口径：汇总「各块局部候选 + 全局空动作 + 跨块前缀」，对每条候选用**全图** Path → Battle
评分（``U = scoreActor − scoreOpponent``），按与 OPT 相同的 tie-break 选本回合动作。
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Iterable

from treasure_game.battle_module import BattleModule
from treasure_game.models import (
    Direction,
    GameState,
    IllegalStateError,
    Position,
    Seat,
    TrueMap,
    require_true_map,
)
from treasure_game.path_module import PathModule

__all__ = ["DivideConquerModule", "PartitionReport", "DEFAULT_PARTITION_THRESHOLD"]

_INF = float("inf")
_TIE_EPS = 1e-9

_NEIGHBOR_ORDER: tuple[Direction, ...] = (
    Direction.UP,
    Direction.LEFT,
    Direction.DOWN,
    Direction.RIGHT,
)
_DIRECTION_RANK = {direction: index for index, direction in enumerate(_NEIGHBOR_ORDER)}

#: 分块面积阈值默认值（与 06 §3 冻结签名一致：``V >= 144`` 才走分治）。
DEFAULT_PARTITION_THRESHOLD = 144


def _require_dice(dice_N: object) -> int:
    if isinstance(dice_N, bool) or not isinstance(dice_N, int):
        raise ValueError(f"dice_N 必须是 int，收到 {type(dice_N).__name__}：{dice_N!r}")
    if dice_N < 0:
        raise ValueError(f"dice_N 必须 >= 0，收到 {dice_N}")
    return dice_N


def _require_seat(seat: object) -> Seat:
    if not isinstance(seat, Seat):
        raise ValueError(f"seat 必须是 Seat，收到 {type(seat).__name__}：{seat!r}")
    return seat


def _require_state(state: object) -> GameState:
    if not isinstance(state, GameState):
        raise ValueError(f"state 必须是 GameState，收到 {type(state).__name__}")
    return state


def _split(total: int, parts: int) -> list[tuple[int, int]]:
    """把 ``[0, total)`` 确定性地切成 ``parts`` 段近等长区间（前 ``rem`` 段各多 1）。"""
    base, rem = divmod(total, parts)
    bands: list[tuple[int, int]] = []
    start = 0
    for index in range(parts):
        size = base + (1 if index < rem else 0)
        bands.append((start, start + size))
        start += size
    return bands


@dataclasses.dataclass(frozen=True, slots=True)
class PartitionReport:
    """一次分治求解的可观察证据（实验记录用；不进入冻结的 ``SearchDiagnostics``）。"""

    threshold: int
    used: bool
    blocks: int
    blocks_with_resources: int
    resource_count: int
    local_candidates: int
    cross_block_candidates: int
    merged_candidates: int
    merged_u: int
    elapsed_ns: int


class DivideConquerModule:
    """确定性 4×4 近似矩形分块 + 全图合并（大地图 OPT 专用）。

    * ``partition_threshold``：面积 ``V >= threshold`` 时 :class:`AiModule` 才走本入口；
    * ``block_rows`` / ``block_cols``：行/列切成几段（默认 4×4 = 至多 16 块，边缘允许不足）。
    """

    __slots__ = ("partition_threshold", "block_rows", "block_cols", "_last_report")

    def __init__(
        self,
        partition_threshold: int = DEFAULT_PARTITION_THRESHOLD,
        block_rows: int = 4,
        block_cols: int = 4,
    ) -> None:
        for name, value in (
            ("partition_threshold", partition_threshold),
            ("block_rows", block_rows),
            ("block_cols", block_cols),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数，收到 {value!r}")
        self.partition_threshold = partition_threshold
        self.block_rows = block_rows
        self.block_cols = block_cols
        self._last_report: PartitionReport | None = None

    # ------------------------------------------------------------------
    # 分块
    # ------------------------------------------------------------------
    def block_bounds(self, true_map: TrueMap) -> list[tuple[int, int, int, int]]:
        """返回 ``[(row0, row1, col0, col1)]``（行优先顺序，半开区间），只含非空块。"""
        m = require_true_map(true_map)
        bounds: list[tuple[int, int, int, int]] = []
        for row0, row1 in _split(m.height, self.block_rows):
            if row1 <= row0:
                continue
            for col0, col1 in _split(m.width, self.block_cols):
                if col1 <= col0:
                    continue
                bounds.append((row0, row1, col0, col1))
        return bounds

    @staticmethod
    def block_index_of(
        true_map: TrueMap, pos: Position, bounds: Iterable[tuple[int, int, int, int]]
    ) -> int:
        """给定坐标落在第几个块（不在任何块内返回 ``-1``）。"""
        for index, (row0, row1, col0, col1) in enumerate(bounds):
            if row0 <= pos.row < row1 and col0 <= pos.col < col1:
                return index
        return -1

    def last_report(self) -> PartitionReport:
        """最近一次 ``solve`` 的分块/候选统计（未调用过则抛 ``IllegalStateError``）。"""
        if self._last_report is None:
            raise IllegalStateError("尚未执行任何分治求解：请先调用 DivideConquerModule.solve(...)")
        return self._last_report

    # ------------------------------------------------------------------
    # 求解
    # ------------------------------------------------------------------
    def solve(
        self,
        state: GameState,
        dice_N: int,
        seat: Seat,
        solve_local: Callable[..., tuple[Direction, ...]],
    ) -> tuple[Direction, ...]:
        """分块求解本回合动作；返回序列一定能在**全图**上合法重放。

        ``solve_local(state, dice_N, allowed_positions)`` 由调用方注入（``AiModule`` 传入
        OPT 纯核）：``allowed_positions`` 是 ``frozenset[Position]``，只限制"关注的目标资源"。
        """
        current_state = _require_state(state)
        steps = _require_dice(dice_N)
        actor = _require_seat(seat)
        if not callable(solve_local):
            raise ValueError("solve_local 必须可调用：solve_local(state, dice_N, allowed_positions)")
        m = require_true_map(current_state.true_map)
        start = current_state.position_of(actor)
        opponent = current_state.position_of(actor.opponent)
        score_actor = current_state.score_of(actor)
        score_opponent = current_state.score_of(actor.opponent)

        # 空骰 / 无剩余宝藏也要走完整合并流程（结果落回全局空动作），保持口径一致。
        start_ns = time.perf_counter_ns()
        bounds = self.block_bounds(m)
        distances = PathModule.bfs_distances(m, start)

        candidates: list[tuple[Direction, ...]] = [()]  # 全局空动作
        local_candidates = 0
        blocks_with_resources = 0

        # 1) 逐块局部求解：只限制关注目标集合，真实图/对手/资源全部保留。
        for row0, row1, col0, col1 in bounds:
            targets = frozenset(
                pos
                for pos in m.remaining_treasures
                if row0 <= pos.row < row1 and col0 <= pos.col < col1
            )
            if not targets:
                continue
            blocks_with_resources += 1
            local_sequence = solve_local(current_state, steps, targets)
            if not isinstance(local_sequence, tuple):
                local_sequence = tuple(local_sequence)
            for direction in local_sequence:
                if not isinstance(direction, Direction):
                    raise ValueError(f"solve_local 返回了非 Direction 元素：{direction!r}")
            local_sequence = local_sequence[:steps]
            if local_sequence and local_sequence not in candidates:
                candidates.append(local_sequence)
            local_candidates += 1

        # 2) 跨块前缀：每块的"入口" = 块内离当前座位最近的可通行格（全图 BFS 距离）。
        cross_block_candidates = 0
        for row0, row1, col0, col1 in bounds:
            entry: Position | None = None
            entry_distance = _INF
            for row in range(row0, row1):
                for col in range(col0, col1):
                    pos = Position(row, col)
                    if m.is_wall(pos):
                        continue
                    distance = distances.get(pos)
                    if distance is None or distance == 0:
                        continue
                    if distance < entry_distance or (
                        distance == entry_distance
                        and entry is not None
                        and (pos.row, pos.col) < (entry.row, entry.col)
                    ):
                        entry, entry_distance = pos, distance
            if entry is None:
                continue
            path = PathModule.bfs_path(m, start, entry)
            if not path:
                continue
            sequence = tuple(path[:steps])
            if sequence and sequence not in candidates:
                candidates.append(sequence)
                cross_block_candidates += 1

        # 3) 合并：全图 Path → Battle 评分，采用与 OPT 相同的 U / tie-break 规则。
        start_block = self.block_index_of(m, start, bounds)
        scored: list[tuple[float, int, float, tuple[Direction, ...], bool]] = []
        for sequence in candidates:
            result = PathModule.sim_player_multi_step(m, start, sequence, steps, actor=actor)
            if actor is Seat.A:
                battle = BattleModule.trigger_battle(
                    result.true_map,
                    result.final_pos,
                    opponent,
                    score_actor + result.score_delta,
                    score_opponent,
                    actor_a=actor,
                    actor_b=actor.opponent,
                )
                utility = battle.score_a - battle.score_b
                final_pos = battle.pos_a
            else:
                battle = BattleModule.trigger_battle(
                    result.true_map,
                    opponent,
                    result.final_pos,
                    score_opponent,
                    score_actor + result.score_delta,
                    actor_a=actor.opponent,
                    actor_b=actor,
                )
                utility = battle.score_b - battle.score_a
                final_pos = battle.pos_b
            distance = self._nearest_treasure_distance(
                result.true_map, final_pos, result.true_map.remaining_treasures
            )
            crossed = self.block_index_of(result.true_map, final_pos, bounds) != start_block
            scored.append((float(utility), result.valid_steps, distance, sequence, crossed))

        best_u = max(item[0] for item in scored)
        tied = [item for item in scored if item[0] >= best_u - _TIE_EPS]

        def tie_key(item: tuple) -> tuple:
            _u, valid_steps, distance, sequence, _crossed = item
            rank = tuple(_DIRECTION_RANK[direction] for direction in sequence)
            return (distance, valid_steps, rank)

        chosen = min(tied, key=tie_key)
        elapsed_ns = time.perf_counter_ns() - start_ns
        self._last_report = PartitionReport(
            threshold=self.partition_threshold,
            used=True,
            blocks=len(bounds),
            blocks_with_resources=blocks_with_resources,
            resource_count=len(m.remaining_treasures),
            local_candidates=local_candidates,
            cross_block_candidates=cross_block_candidates,
            merged_candidates=len(scored),
            merged_u=int(chosen[0]),
            elapsed_ns=elapsed_ns,
        )
        return chosen[3]

    # ------------------------------------------------------------------
    @staticmethod
    def _nearest_treasure_distance(
        true_map: TrueMap, pos: Position, remaining: frozenset[Position]
    ) -> float:
        if not remaining:
            return 0.0
        distances = PathModule.bfs_distances(true_map, pos)
        best = _INF
        for target in remaining:
            distance = distances.get(target)
            if distance is not None and distance < best:
                best = float(distance)
        return best if best < _INF else float(true_map.height * true_map.width + 1)
