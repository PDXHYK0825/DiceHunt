"""移动语义唯一真相源（``docs/06-interface-freeze.md`` §3/§4，ADR-03）。

本模块是全项目**唯一**的逐格移动结算实现：

* 撞墙/越界 → 坐标、地图、分数、步数全不变，产生 ``BLOCKED`` 事件，文案逐字冻结；
* 合法踏入 → ``valid_steps += 1``，并**立即**结算资源（宝藏消失加分 / 陷阱消失扣分）；
* ``valid_steps`` 达到 ``max_valid_step`` 立即停止（含 ``0``），忽略剩余指令、不自动补步；
* 返回**新的** ``TrueMap``，入参对象内容永不被修改。

贪心 AI、Expecti-Max、分治模块的内部推演都必须调用 :meth:`PathModule.sim_player_multi_step`
（或其等价的纯内核委托），不得另写第二份结算逻辑。

坐标系：零基 ``(row, col)``，``row`` 向下增大；邻序遍历固定 **上、左、下、右**。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from treasure_game.models import (
    BLOCKED_MOVE_MESSAGE,
    Direction,
    GameEvent,
    MoveResult,
    Position,
    Seat,
    TrueMap,
    clone_true_map,
    require_true_map,
)

__all__ = ["PathModule", "NEIGHBOR_ORDER"]

#: 邻序遍历顺序（冻结）：上、左、下、右。BFS 并列最短路取舍与败方推开都依赖它。
NEIGHBOR_ORDER: tuple[Direction, ...] = (
    Direction.UP,
    Direction.LEFT,
    Direction.DOWN,
    Direction.RIGHT,
)

#: ``(方向, drow, dcol)`` 预展开，供 BFS 内部循环使用（避免热路径上反复取 delta）。
_NEIGHBOR_STEPS: tuple[tuple[Direction, int, int], ...] = tuple(
    (direction, direction.delta[0], direction.delta[1]) for direction in NEIGHBOR_ORDER
)


def _require_start(true_map: TrueMap, value: object, name: str) -> Position:
    """校验"必须站在合法格子上"的位置参数：类型、边界、非墙。"""
    if not isinstance(value, Position):
        raise ValueError(f"{name} 必须是 Position，收到 {type(value).__name__}")
    if not true_map.in_bounds(value):
        raise ValueError(
            f"{name} 越界：{value}（地图 {true_map.height}x{true_map.width}）"
        )
    if true_map.terrain[value.row][value.col] == 1:
        raise ValueError(f"{name} 不能是障碍物：{value}")
    return value


def _require_step_limit(value: object) -> int:
    if type(value) is not int:
        raise ValueError(f"max_valid_step 必须是 int，收到 {type(value).__name__}")
    if value < 0:
        raise ValueError(f"max_valid_step 必须 >= 0，收到 {value}")
    return value


def _normalize_move_seq(move_seq: object) -> tuple[Direction, ...]:
    """把 ``move_seq`` 规范化为 ``tuple[Direction, ...]``；非方向元素立即抛 ``ValueError``。"""
    if isinstance(move_seq, (str, bytes)):
        raise ValueError("move_seq 必须是 Direction 序列，不能是字符串（IO 层负责 w/a/s/d 转换）")
    if type(move_seq) is tuple:  # 常见情形：直接复用，避免热路径上的重复建元组
        for index, item in enumerate(move_seq):
            if not isinstance(item, Direction):
                raise ValueError(
                    f"move_seq[{index}] 必须是 Direction，收到 {type(item).__name__}：{item!r}"
                )
        return move_seq
    if not isinstance(move_seq, Iterable):
        raise ValueError(f"move_seq 必须是 Direction 序列，收到 {type(move_seq).__name__}")
    normalized: list[Direction] = []
    for index, item in enumerate(move_seq):
        if not isinstance(item, Direction):
            raise ValueError(
                f"move_seq[{index}] 必须是 Direction，收到 {type(item).__name__}：{item!r}"
            )
        normalized.append(item)
    return tuple(normalized)


class PathModule:
    """寻路与移动模拟模块（全部方法均为 ``@staticmethod``，无实例状态）。"""

    #: 撞墙/越界文案（与 :data:`treasure_game.models.BLOCKED_MOVE_MESSAGE` 同一常量）
    BLOCKED_MESSAGE = BLOCKED_MOVE_MESSAGE

    # ------------------------------------------------------------------
    # 移动内核
    # ------------------------------------------------------------------
    @staticmethod
    def sim_player_multi_step(
        true_map: TrueMap,
        start_pos: Position,
        move_seq: tuple[Direction, ...],
        max_valid_step: int,
        actor: Seat = Seat.A,
    ) -> MoveResult:
        """逐条执行 ``move_seq``，最多完成 ``max_valid_step`` 次成功移动。

        语义（§4.1–§4.4）：

        * 每条指令先判越界/墙：失败则坐标、地图、分数、``valid_steps`` 均不变，
          追加一条 ``BLOCKED`` 事件（``actor`` = ``actor`` 参数，``position`` = 当前未移动的坐标），
          继续处理下一条指令；**不消耗**有效步数，也**不自动补步**。
        * 合法踏入：``valid_steps += 1``，追加 ``MOVE`` 事件；若该格是剩余宝藏则立即拾取
          （``score_delta += value``，从 ``remaining_treasures`` 移除，追加 ``TREASURE`` 事件）；
          若是未触发陷阱则立即触发（``score_delta += value``（负值），从 ``active_traps`` 移除、
          加入 ``revealed_traps`` 永久保留，追加 ``TRAP`` 事件）。资源只结算一次，重复踏入不再变动。
        * ``valid_steps`` 达到 ``max_valid_step``（含 ``0``）立即停止，忽略剩余指令。

        参数校验（非法输入抛 ``ValueError``；``ViewMap`` 冒充真值抛 ``TypeError``）：

        * ``max_valid_step < 0``；
        * ``start_pos`` 越界或落在障碍物上；
        * ``move_seq`` 中出现非 ``Direction`` 元素。

        :returns: :class:`MoveResult`：新 ``TrueMap``、终点坐标、分数增量、有效/尝试/撞墙步数、有序事件。
        """
        current_map = require_true_map(true_map)
        start = _require_start(current_map, start_pos, "start_pos")
        limit = _require_step_limit(max_valid_step)
        sequence = _normalize_move_seq(move_seq)
        if actor is not None and not isinstance(actor, Seat):
            raise ValueError(f"actor 必须是 Seat 或 None，收到 {type(actor).__name__}")

        terrain = current_map.terrain
        height = current_map.height
        width = current_map.width

        pos = start
        score_delta = 0
        valid_steps = 0
        attempted_steps = 0
        blocked_steps = 0
        events: list[GameEvent] = []

        # 写时复制：没有任何资源触发时完全不复制集合，避免搜索热路径上的多余开销。
        remaining = current_map.remaining_treasures
        active = current_map.active_traps
        revealed = current_map.revealed_traps
        mutable = False

        for direction in sequence:
            if valid_steps >= limit:
                break  # 已完成 N 次有效移动 → 忽略剩余指令
            attempted_steps += 1

            drow, dcol = direction.delta
            next_row = pos.row + drow
            next_col = pos.col + dcol
            if (
                next_row < 0
                or next_row >= height
                or next_col < 0
                or next_col >= width
                or terrain[next_row][next_col] == 1
            ):
                blocked_steps += 1
                events.append(GameEvent.blocked(actor, pos))
                continue

            pos = Position(next_row, next_col)
            valid_steps += 1
            events.append(GameEvent.move(actor, pos))

            if pos in remaining:
                if not mutable:
                    remaining = set(remaining)
                    active = set(active)
                    revealed = set(revealed)
                    mutable = True
                value = current_map.treasure_values[pos]
                remaining.discard(pos)
                score_delta += value
                events.append(GameEvent.treasure(actor, pos, value))
            if pos in active:
                if not mutable:
                    remaining = set(remaining)
                    active = set(active)
                    revealed = set(revealed)
                    mutable = True
                value = current_map.trap_values[pos]
                active.discard(pos)
                revealed.add(pos)  # 永久保留：暴露历史不因陷阱失效而丢失
                score_delta += value
                events.append(GameEvent.trap(actor, pos, value))

        # 任何操作都返回新的 TrueMap（§4.3）；内容不变时同样返回新对象。
        # 用 clone_true_map 而非 dataclasses.replace：数据已校验过，不必在搜索热路径上重复校验。
        if mutable:
            result_map = clone_true_map(
                current_map,
                remaining_treasures=frozenset(remaining),
                active_traps=frozenset(active),
                revealed_traps=frozenset(revealed),
            )
        else:
            result_map = clone_true_map(current_map)

        return MoveResult(
            true_map=result_map,
            final_pos=pos,
            score_delta=score_delta,
            valid_steps=valid_steps,
            attempted_steps=attempted_steps,
            events=tuple(events),
            blocked_steps=blocked_steps,
        )

    # ------------------------------------------------------------------
    # 邻格查询（对战败方推开）
    # ------------------------------------------------------------------
    @staticmethod
    def get_adjacent_empty(true_map: TrueMap, origin: Position) -> Position | None:
        """返回 ``origin`` 相邻的第一个"从未布置过资源"的空地，无候选返回 ``None``。

        候选条件（D4 决策）：底图 ``terrain == 0``、且该坐标**不在** ``treasure_values`` /
        ``trap_values`` 的键中——即历史上从未放过宝藏或陷阱，已拾取的宝藏格与已触发的陷阱格
        都不算"空"。遍历顺序固定 **上、左、下、右**；``origin`` 越界或为墙抛 ``ValueError``。
        """
        current_map = require_true_map(true_map)
        start = _require_start(current_map, origin, "origin")

        for direction in NEIGHBOR_ORDER:
            candidate = start.moved(direction)
            if not current_map.in_bounds(candidate):
                continue
            if current_map.terrain[candidate.row][candidate.col] != 0:
                continue
            if candidate in current_map.treasure_values or candidate in current_map.trap_values:
                continue
            return candidate
        return None

    # ------------------------------------------------------------------
    # BFS（连通性 / 距离 / 最短路）
    # ------------------------------------------------------------------
    @staticmethod
    def bfs_distances(true_map: TrueMap, start: Position) -> dict[Position, int]:
        """以底图墙为障碍的 BFS 距离表：``start`` 为 ``0``，不可达的格子**缺键**。

        资源不阻挡通行，也不触发任何效果（BFS 只读静态底图）。``start`` 越界/为墙抛 ``ValueError``。
        内部用扁平下标遍历，末尾一次性物化 ``dict[Position, int]``（贪心 AI 会按宝藏逐个调用它）。
        """
        current_map = require_true_map(true_map)
        origin = _require_start(current_map, start, "start")

        heights = current_map.height
        widths = current_map.width
        terrain = current_map.terrain

        size = heights * widths
        levels = [-1] * size  # -1 = 未访问；避免为每个格子创建 Position 对象
        start_index = origin.row * widths + origin.col
        levels[start_index] = 0

        queue: deque[int] = deque([start_index])
        while queue:
            index = queue.popleft()
            row, col = divmod(index, widths)
            step = levels[index] + 1
            for _direction, drow, dcol in _NEIGHBOR_STEPS:
                next_row = row + drow
                next_col = col + dcol
                if next_row < 0 or next_row >= heights or next_col < 0 or next_col >= widths:
                    continue
                if terrain[next_row][next_col] == 1:
                    continue
                next_index = next_row * widths + next_col
                if levels[next_index] >= 0:
                    continue
                levels[next_index] = step
                queue.append(next_index)

        return {
            Position(index // widths, index % widths): step
            for index, step in enumerate(levels)
            if step >= 0
        }

    @staticmethod
    def bfs_path(
        true_map: TrueMap, start: Position, target: Position
    ) -> tuple[Direction, ...] | None:
        """同权最短路的方向序列；``start == target`` 返回 ``()``，不可达返回 ``None``。

        并列最短路按冻结邻序 **上、左、下、右** 决定取舍（结果是确定的、可复现的）。
        ``start``/``target`` 越界或为墙抛 ``ValueError``。
        """
        current_map = require_true_map(true_map)
        origin = _require_start(current_map, start, "start")
        goal = _require_start(current_map, target, "target")
        if origin == goal:
            return ()

        heights = current_map.height
        widths = current_map.width
        terrain = current_map.terrain

        size = heights * widths
        start_index = origin.row * widths + origin.col
        goal_index = goal.row * widths + goal.col
        visited = bytearray(size)
        visited[start_index] = 1
        parents = [-1] * size
        incoming: list[Direction | None] = [None] * size

        queue: deque[int] = deque([start_index])
        while queue:
            index = queue.popleft()
            row, col = divmod(index, widths)
            for direction, drow, dcol in _NEIGHBOR_STEPS:
                next_row = row + drow
                next_col = col + dcol
                if next_row < 0 or next_row >= heights or next_col < 0 or next_col >= widths:
                    continue
                if terrain[next_row][next_col] == 1:
                    continue
                next_index = next_row * widths + next_col
                if visited[next_index]:
                    continue
                visited[next_index] = 1
                parents[next_index] = index
                incoming[next_index] = direction
                queue.append(next_index)

        if not visited[goal_index]:
            return None

        steps: list[Direction] = []
        cursor = goal_index
        while cursor != start_index:
            direction = incoming[cursor]
            assert direction is not None  # 只有起点缺 incoming，而循环在起点终止
            steps.append(direction)
            cursor = parents[cursor]
        steps.reverse()
        return tuple(steps)
