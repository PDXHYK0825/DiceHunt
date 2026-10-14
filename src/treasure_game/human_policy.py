"""人工行为模型（D5）：有限策略集上的最坏情况（02 §2「人类行为模型」/ 06 §3）。

设计红线（06 §4.6、task-4 第 5 条）：

* 本模块**只接受** :class:`~treasure_game.models.ViewMap` 与
  :class:`~treasure_game.models.PublicStateView`，永远不接触 ``TrueMap`` / ``GameState``；
  传入真图会立刻抛 ``TypeError``（运行时类型守卫，不是注解约定）。
* 候选集完全由视图 + 公开信息生成：HARD 下未触发陷阱在视图里就是 ``0``，
  因此"看不见"它——人类并不知道自己会踩坑。
* 候选选定后才由搜索方/仿真器在真值图上执行并承担**真实后果**
  （OPT 的 MIN 节点从本集合中选对 AI 最不利的真实结果）。

策略（``strategy``）
--------------------
* ``GATHER``（默认）：走向视图上最近的可见宝藏；**陷阱盲**（不看格值正负）；
* ``RATIO``：走向视图性价比 ``value/max(dist,1)`` 最高的宝藏；陷阱盲；
* ``EVADE``：远离对手；陷阱盲；
* ``STOP``：本回合停步；陷阱盲；
* ``MIXED``：``rng`` 在候选集上均匀抽取（实验可复现）；陷阱盲；
* ``CAUTIOUS``：**唯一会读格值正负的策略**——把视图上可见的负值格当高成本，
  优先"途经可见负值格最少"的候选（详见 :meth:`HumanPolicy._cautious_key`）。
  HARD 下未触发陷阱在视图里就是 ``0``，所以它**照样看不见、照样会踩进去**；
  这正是 NORMAL vs HARD 对照要隔离的机制（spec §六 必做 7）。

``candidate_actions`` 对前五种策略是**策略无关**的确定性全集（几何口径，同视图 → 同列表、同顺序）；
``CAUTIOUS`` 在该全集之外追加"避开可见负值格"的绕行候选并按成本重排。

``model_id`` 是 W4 实验 CSV 的 ``human_model_id`` 列取值（``VIEW_HEURISTIC:{strategy}:v1``），
与搜索内部 MIN 策略集标识 :data:`HUMAN_MODEL_ID` 是两件事。

实现说明：内部 BFS 走**扁平整数下标**（``row * width + col``），只读 ``ViewMap.cells``；
热路径（搜索里每个 MIN 节点都会调用）不再为每个格子创建 ``Position``/``dict`` 结构。
"""

from __future__ import annotations

import random
from collections import deque

from treasure_game.models import (
    Direction,
    Position,
    PublicStateView,
    Seat,
    ViewMap,
)

__all__ = [
    "HumanPolicy",
    "HUMAN_MODEL_ID",
    "HUMAN_MODEL_ID_WORST_CASE",
    "HUMAN_MODEL_ID_PREFIX",
    "HUMAN_MODEL_ID_VERSION",
    "human_model_id",
    "STRATEGIES",
    "BLIND_STRATEGIES",
    "STRATEGY_GATHER",
    "STRATEGY_RATIO",
    "STRATEGY_EVADE",
    "STRATEGY_STOP",
    "STRATEGY_MIXED",
    "STRATEGY_CAUTIOUS",
]

#: 搜索 DP key 中的 ``human_model_id``：**默认**把人类预测为单一确定性启发式
#: （按 :data:`DEFAULT_HUMAN_MODEL_STRATEGY` 的策略走）。MIN 节点因此退化为单动作预测，
#: 不再对所有候选取"最坏情况"。数值后缀随策略名变化，确保缓存键不跨模型复用。
HUMAN_MODEL_ID = "view-heuristic-gather-v1"

#: 旧口径：MIN 使用本模块的**整个确定性策略集**并取对 AI 最不利者（"最坏情况"建模）。
#: 保留为可选模型，用于消融对照。
HUMAN_MODEL_ID_WORST_CASE = "view-policy-set-v1"

#: 单一启发式人类模型的默认策略。
DEFAULT_HUMAN_MODEL_STRATEGY = "GATHER"

#: W4 实验 CSV 的 ``human_model_id`` 列格式：``VIEW_HEURISTIC:{strategy}:v1``
#: （与 ``experiments/run_batch.py`` 的派生规则逐字一致）。
HUMAN_MODEL_ID_PREFIX = "VIEW_HEURISTIC"
HUMAN_MODEL_ID_VERSION = "v1"

STRATEGY_GATHER = "GATHER"
STRATEGY_RATIO = "RATIO"
STRATEGY_EVADE = "EVADE"
STRATEGY_STOP = "STOP"
STRATEGY_MIXED = "MIXED"
STRATEGY_CAUTIOUS = "CAUTIOUS"

#: ``choose`` 支持的策略名（大小写不敏感）。
STRATEGIES: tuple[str, ...] = (
    STRATEGY_GATHER,
    STRATEGY_RATIO,
    STRATEGY_EVADE,
    STRATEGY_STOP,
    STRATEGY_MIXED,
    STRATEGY_CAUTIOUS,
)

#: **陷阱盲**策略：候选与动作只看几何（可通行性/宝藏/坐标），不读格值正负。
BLIND_STRATEGIES: tuple[str, ...] = (
    STRATEGY_GATHER,
    STRATEGY_RATIO,
    STRATEGY_EVADE,
    STRATEGY_STOP,
    STRATEGY_MIXED,
)

#: 候选集里最多保留多少个"寻宝目标"前缀（控制 MIN 分支数）。
_MAX_TREASURE_TARGETS = 4

#: 邻序（与 PathModule 冻结邻序一致：上、左、下、右）+ 方向序号（序列字典序用）。
_NEIGHBOR_ORDER: tuple[Direction, ...] = (
    Direction.UP,
    Direction.LEFT,
    Direction.DOWN,
    Direction.RIGHT,
)
_NEIGHBOR_STEPS: tuple[tuple[int, int], ...] = tuple(
    direction.delta for direction in _NEIGHBOR_ORDER
)
_DIRECTION_RANK = {direction: index for index, direction in enumerate(_NEIGHBOR_ORDER)}


def human_model_id(strategy: str) -> str:
    """W4 实验/CSV 用的人类模型标识：``VIEW_HEURISTIC:{strategy}:v1``。"""
    return f"{HUMAN_MODEL_ID_PREFIX}:{str(strategy).strip().upper()}:{HUMAN_MODEL_ID_VERSION}"


# ---------------------------------------------------------------------------
# 视图守卫
# ---------------------------------------------------------------------------
def _require_view(view: object) -> ViewMap:
    """类型守卫：只接受 ``ViewMap``；``TrueMap``/``GameState`` 立刻抛 ``TypeError``。"""
    if isinstance(view, ViewMap):
        return view
    raise TypeError(
        f"HumanPolicy 只接受 ViewMap（视图-真值隔离），收到 {type(view).__name__}："
        "禁止把 TrueMap/GameState 交给人类模型"
    )


def _require_public(public: object) -> PublicStateView:
    if isinstance(public, PublicStateView):
        return public
    raise TypeError(f"HumanPolicy 只接受 PublicStateView，收到 {type(public).__name__}")


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


def _position_of(public: PublicStateView, seat: Seat) -> Position:
    """``PublicStateView`` 按座位取坐标（该类型本身没有 ``position_of``）。"""
    return public.pos_a if seat is Seat.A else public.pos_b


# ---------------------------------------------------------------------------
# 视图上的扁平 BFS（1 = 墙，其余（含宝藏/陷阱）可通行）
# ---------------------------------------------------------------------------
def _bfs_flat(
    view: ViewMap, start_index: int, avoid_negative: bool = False
) -> tuple[list[int], list[int], list[int]]:
    """视图 BFS，返回 ``(dist, parent, incoming_rank)``；不可达为 ``-1``。

    ``incoming_rank`` 是 :data:`_NEIGHBOR_ORDER` 的下标，用来把父链还原成方向序列。
    ``avoid_negative=True`` 时**不进入**视图上的负值格（= 把所有"看得见的陷阱"当墙绕开），
    用于 ``CAUTIOUS`` 生成绕行候选；HARD 下未触发陷阱是 ``0``，因此不受影响。
    """
    width = view.width
    height = view.height
    cells = view.cells
    size = width * height
    dist = [-1] * size
    parent = [-1] * size
    incoming = [-1] * size
    dist[start_index] = 0
    queue: deque[int] = deque([start_index])
    while queue:
        index = queue.popleft()
        row, col = divmod(index, width)
        step = dist[index] + 1
        for rank in range(4):
            drow, dcol = _NEIGHBOR_STEPS[rank]
            next_row = row + drow
            next_col = col + dcol
            if next_row < 0 or next_row >= height or next_col < 0 or next_col >= width:
                continue
            cell = cells[next_row][next_col]
            if view.is_wall(next_row, next_col):
                continue
            if avoid_negative and cell < 0:  # 看得见的陷阱格：绕开
                continue
            next_index = next_row * width + next_col
            if dist[next_index] >= 0:
                continue
            dist[next_index] = step
            parent[next_index] = index
            incoming[next_index] = rank
            queue.append(next_index)
    return dist, parent, incoming


def _bfs_distances_flat(
    view: ViewMap, start_index: int, avoid_negative: bool = False
) -> list[int]:
    """只求距离的视图 BFS（比全父链版更省），不可达为 ``-1``。"""
    width = view.width
    height = view.height
    cells = view.cells
    dist = [-1] * (width * height)
    dist[start_index] = 0
    queue: deque[int] = deque([start_index])
    while queue:
        index = queue.popleft()
        row, col = divmod(index, width)
        step = dist[index] + 1
        for rank in range(4):
            drow, dcol = _NEIGHBOR_STEPS[rank]
            next_row = row + drow
            next_col = col + dcol
            if next_row < 0 or next_row >= height or next_col < 0 or next_col >= width:
                continue
            cell = cells[next_row][next_col]
            if view.is_wall(next_row, next_col):
                continue
            if avoid_negative and cell < 0:
                continue
            next_index = next_row * width + next_col
            if dist[next_index] >= 0:
                continue
            dist[next_index] = step
            queue.append(next_index)
    return dist


def _prefix_flat(
    start_index: int,
    target_index: int,
    parent: list[int],
    incoming: list[int],
    dice_N: int,
) -> tuple[Direction, ...]:
    """把 BFS 父链还原成方向序列并截到 ``dice_N``；不可达/无步返回空序列。"""
    if dice_N <= 0 or target_index == start_index:
        return ()
    if parent[target_index] < 0:
        return ()
    steps: list[Direction] = []
    cursor = target_index
    while cursor != start_index and parent[cursor] >= 0:
        rank = incoming[cursor]
        if rank < 0:  # pragma: no cover - BFS 保证有入边
            return ()
        steps.append(_NEIGHBOR_ORDER[rank])
        cursor = parent[cursor]
    if cursor != start_index:  # pragma: no cover
        return ()
    steps.reverse()
    return tuple(steps[:dice_N])


def _treasure_indices(view: ViewMap) -> list[int]:
    """视图上可见宝藏格的扁平下标（含 1 分宝藏），按行优先（= ``(row, col)`` 字典序）。"""
    width = view.width
    found: list[int] = []
    for row_index, row in enumerate(view.cells):
        base = row_index * width
        for col_index, value in enumerate(row):
            if value >= 2 or Position(row_index, col_index) in view.treasure_cells:
                found.append(base + col_index)
    return found


def _treasure_value(view: ViewMap, index: int) -> int:
    return view.cells[index // view.width][index % view.width]


def _view_path_stats(
    view: ViewMap, start_index: int, sequence: tuple[Direction, ...], dice_N: int
) -> tuple[int, int, int, int, int]:
    """**只在视图上**逐条走一遍序列，返回 ``(trap_steps, trap_loss, gain, end_index, valid_steps)``。

    * ``trap_steps``：途经的**可见负值格**个数（HARD 下未触发陷阱显示 0 → 不计入）；
    * ``trap_loss``：这些格子的扣分绝对值之和；
    * ``gain``：途经的可见宝藏值之和；
    * 撞墙/越界不耗步（与 ``PathModule`` 同语义），走满 ``dice_N`` 即停。
    """
    width = view.width
    height = view.height
    cells = view.cells
    index = start_index
    trap_steps = 0
    trap_loss = 0
    gain = 0
    valid_steps = 0
    for direction in sequence:
        if valid_steps >= dice_N:
            break
        drow, dcol = direction.delta
        row, col = divmod(index, width)
        next_row = row + drow
        next_col = col + dcol
        if next_row < 0 or next_row >= height or next_col < 0 or next_col >= width:
            continue
        value = cells[next_row][next_col]
        if view.is_wall(next_row, next_col):
            continue
        index = next_row * width + next_col
        valid_steps += 1
        if value < 0:
            trap_steps += 1
            trap_loss += -value
        elif view.is_treasure(next_row, next_col):
            gain += value
    return trap_steps, trap_loss, gain, index, valid_steps


class HumanPolicy:
    """视图启发式人类模型（默认 ``GATHER``：走向视图上最近的可见宝藏）。

    参数
    ----
    strategy:
        ``GATHER`` / ``RATIO`` / ``EVADE`` / ``STOP`` / ``MIXED`` / ``CAUTIOUS``；
        决定 :meth:`choose` 的单动作选择。前五种是**陷阱盲**几何策略，
        :meth:`candidate_actions` 对它们恒为同一个确定性策略集全集（与 ``strategy`` 无关）；
        ``CAUTIOUS`` 会读视图格值正负（见 :meth:`_cautious_key`）。
    rng:
        仅 ``MIXED`` 使用；未注入时退化为固定种子 ``random.Random(0)``（ADR-08：搜索不消耗全局 RNG）。
    """

    __slots__ = ("_strategy", "_rng")

    def __init__(self, strategy: str = STRATEGY_GATHER, rng: random.Random | None = None) -> None:
        if not isinstance(strategy, str):
            raise ValueError(f"strategy 必须是 str，收到 {type(strategy).__name__}")
        normalized = strategy.strip().upper()
        if normalized not in STRATEGIES:
            raise ValueError(f"未知人类策略 {strategy!r}；可选：{', '.join(STRATEGIES)}")
        if rng is not None and not isinstance(rng, random.Random):
            raise ValueError(f"rng 必须是 random.Random，收到 {type(rng).__name__}")
        self._strategy = normalized
        self._rng = rng if rng is not None else random.Random(0)

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def trap_blind(self) -> bool:
        """是否**不读**视图格值正负（前五种策略为真，``CAUTIOUS`` 为假）。"""
        return self._strategy in BLIND_STRATEGIES

    @property
    def model_id(self) -> str:
        """W4 实验 CSV 的 ``human_model_id`` 列取值：``VIEW_HEURISTIC:{strategy}:v1``。

        与搜索内部 MIN 策略集标识 :data:`HUMAN_MODEL_ID` 不同：后者描述"MIN 用整个几何策略集"。
        """
        return human_model_id(self._strategy)

    # ------------------------------------------------------------------
    # 几何候选（所有策略的公共基础；顺序固定 → GATHER 行为逐位不变）
    # ------------------------------------------------------------------
    @staticmethod
    def _geometric_prefixes(
        view: ViewMap,
        start_index: int,
        dice_N: int,
        dist: list[int],
        parent: list[int],
        incoming: list[int],
        opponent_index: int,
    ) -> list[tuple[Direction, ...]]:
        """视图几何候选前缀（不含空动作）：最近 4 个可见宝藏 → 性价比最高 → 靠近对手 → 远离对手。"""
        prefixes: list[tuple[Direction, ...]] = []

        def push(sequence: tuple[Direction, ...] | None) -> None:
            if sequence and sequence not in prefixes:
                prefixes.append(sequence)

        reachable = [index for index in _treasure_indices(view) if dist[index] >= 0]
        by_distance = sorted(
            reachable,
            key=lambda index: (dist[index], -_treasure_value(view, index), index),
        )
        for target in by_distance[:_MAX_TREASURE_TARGETS]:
            push(_prefix_flat(start_index, target, parent, incoming, dice_N))

        best = HumanPolicy._best_ratio(view, reachable, dist)
        if best is not None:
            push(_prefix_flat(start_index, best, parent, incoming, dice_N))

        if opponent_index >= 0 and dist[opponent_index] >= 0:
            push(_prefix_flat(start_index, opponent_index, parent, incoming, dice_N))

        push(HumanPolicy._evade_flat(view, start_index, opponent_index, dice_N, dist, parent, incoming))
        return prefixes

    # ------------------------------------------------------------------
    # 候选集（确定性，只读视图）
    # ------------------------------------------------------------------
    def candidate_actions(
        self,
        view: ViewMap,
        seat: Seat,
        dice_N: int,
        public: PublicStateView,
    ) -> list[tuple[Direction, ...]]:
        """返回本回合的确定性候选序列（含空动作），顺序固定。

        * 陷阱盲策略（默认 ``GATHER`` 等五种）：空动作 → 最近若干可见宝藏前缀 →
          视图性价比最高宝藏前缀 → 靠近对手 → 远离对手（与 ``strategy`` 无关）；
        * ``CAUTIOUS``：在上述几何候选之外追加"避开可见负值格"的绕行候选，
          并按 :meth:`_cautious_key` 排序（负值格最少者在前）。

        所有序列都来自**视图**最短路前缀，长度 ``<= dice_N``。
        """
        current_view = _require_view(view)
        actor = _require_seat(seat)
        steps = _require_dice(dice_N)
        state_view = _require_public(public)
        width = current_view.width
        height = current_view.height

        start_pos = _position_of(state_view, actor)
        if not (0 <= start_pos.row < height and 0 <= start_pos.col < width):
            raise ValueError(f"座位 {actor} 的位置 {start_pos} 不在视图范围内")
        start_index = start_pos.row * width + start_pos.col
        dist, parent, incoming = _bfs_flat(current_view, start_index)

        opponent_pos = _position_of(state_view, actor.opponent)
        opponent_index = -1
        if 0 <= opponent_pos.row < height and 0 <= opponent_pos.col < width:
            opponent_index = opponent_pos.row * width + opponent_pos.col

        candidates: list[tuple[Direction, ...]] = [()]  # 停止（永远保留）
        for sequence in self._geometric_prefixes(
            current_view, start_index, steps, dist, parent, incoming, opponent_index
        ):
            candidates.append(sequence)

        if self._strategy == STRATEGY_CAUTIOUS:
            # 绕行候选：把视图上**看得见**的负值格当墙再算一次最短路。
            safe_dist, safe_parent, safe_incoming = _bfs_flat(
                current_view, start_index, avoid_negative=True
            )
            for sequence in self._geometric_prefixes(
                current_view, start_index, steps, safe_dist, safe_parent, safe_incoming,
                opponent_index,
            ):
                if sequence not in candidates:
                    candidates.append(sequence)
            ordered = sorted(
                candidates[1:],
                key=lambda sequence: self._cautious_key(
                    current_view, start_index, sequence, steps
                ),
            )
            return [()] + ordered

        return candidates

    # ------------------------------------------------------------------
    # CAUTIOUS 评分
    # ------------------------------------------------------------------
    @staticmethod
    def _cautious_key(
        view: ViewMap,
        start_index: int,
        sequence: tuple[Direction, ...],
        dice_N: int,
    ) -> tuple:
        """``CAUTIOUS`` 的确定性评分键（**只读视图**，越小越好）：

        1. ``trap_steps``：途经的**可见负值格**个数（HARD 未触发陷阱显示 0 → 看不见 → 不计入）；
        2. ``trap_loss``：这些格子的总扣分绝对值（并列时扣分少者优先）；
        3. ``-gain``：途经的可见宝藏总收益（越多越好）；
        4. 终点到最近可见宝藏的视图 BFS 距离（越短越好，与贪心同口径）；
        5. 有效步数（越少越好）；
        6. 方向序列字典序（上 < 左 < 下 < 右）。

        ①② 就是"优先不经过可见负值格、否则总扣分最小"；③—⑥ 只在与陷阱成本并列时破平局，
        保证完全确定（不消耗随机数）。
        """
        trap_steps, trap_loss, gain, end_index, valid_steps = _view_path_stats(
            view, start_index, sequence, dice_N
        )
        from_end = _bfs_distances_flat(view, end_index)
        nearest = view.width * view.height + 1
        for target in _treasure_indices(view):
            distance = from_end[target]
            if distance >= 0 and distance < nearest:
                nearest = distance
        rank = tuple(_DIRECTION_RANK[direction] for direction in sequence)
        return (trap_steps, trap_loss, -gain, nearest, valid_steps, rank)

    # ------------------------------------------------------------------
    @staticmethod
    def _best_ratio(view: ViewMap, reachable: list[int], dist: list[int]) -> int | None:
        """视图性价比最高的宝物下标（``value / max(dist,1)``，交叉相乘 + 贪心 tie-break）。"""
        best: int | None = None
        best_value = 0
        best_distance = 0
        best_den = 1
        for target in reachable:
            value = _treasure_value(view, target)
            distance = dist[target]
            denominator = distance if distance > 0 else 1
            if best is None:
                best, best_value, best_distance, best_den = target, value, distance, denominator
                continue
            left = value * best_den
            right = best_value * denominator
            if left > right:
                better = True
            elif left < right:
                better = False
            elif value != best_value:
                better = value > best_value
            elif distance != best_distance:
                better = distance < best_distance
            else:
                better = target < best
            if better:
                best, best_value, best_distance, best_den = target, value, distance, denominator
        return best

    @staticmethod
    def _evade_flat(
        view: ViewMap,
        start_index: int,
        opponent_index: int,
        dice_N: int,
        dist: list[int],
        parent: list[int],
        incoming: list[int],
    ) -> tuple[Direction, ...] | None:
        """在 ``dice_N`` 步可达格中选"离对手最远"者，返回**从起点出发**的最短路前缀。

        ``dist/parent/incoming`` 必须是**以起点为根**的 BFS 结果（否则父链方向就错了）。
        """
        if dice_N <= 0 or opponent_index < 0:
            return None
        size = view.width * view.height
        from_opponent = _bfs_distances_flat(view, opponent_index)
        unreachable = size + 1
        best_index = -1
        best_key: tuple[int, int] | None = None
        for index, distance in enumerate(dist):
            if distance <= 0 or distance > dice_N:
                continue
            key = (-(from_opponent[index] if from_opponent[index] >= 0 else unreachable), index)
            if best_key is None or key < best_key:
                best_index, best_key = index, key
        if best_index < 0:
            return None
        sequence = _prefix_flat(start_index, best_index, parent, incoming, dice_N)
        return sequence if sequence else None

    # ------------------------------------------------------------------
    # 单动作选择
    # ------------------------------------------------------------------
    def choose(
        self,
        view: ViewMap,
        seat: Seat,
        dice_N: int,
        public: PublicStateView,
    ) -> tuple[Direction, ...]:
        """按 ``strategy`` 从视图候选集中挑一个确定性动作。

        * ``GATHER``：最近可见宝藏（并列：分值高 → 坐标小）；无宝藏 → 空动作；
        * ``RATIO``：视图性价比最高宝藏；
        * ``EVADE``：远离对手；
        * ``STOP``：空动作；
        * ``MIXED``：``rng`` 在候选集上均匀抽取（实验可复现）；
        * ``CAUTIOUS``：按 :meth:`_cautious_key` 最小者（可见负值格最少优先）。
        """
        current_view = _require_view(view)
        actor = _require_seat(seat)
        steps = _require_dice(dice_N)
        state_view = _require_public(public)
        candidates = self.candidate_actions(current_view, actor, steps, state_view)

        if self._strategy == STRATEGY_STOP:
            return ()
        if self._strategy == STRATEGY_MIXED:
            return candidates[self._rng.randrange(len(candidates))]
        if self._strategy == STRATEGY_CAUTIOUS:
            width = current_view.width
            start_pos = _position_of(state_view, actor)
            start_index = start_pos.row * width + start_pos.col
            return min(
                candidates,
                key=lambda sequence: self._cautious_key(
                    current_view, start_index, sequence, steps
                ),
            )

        width = current_view.width
        height = current_view.height
        start_pos = _position_of(state_view, actor)
        start_index = start_pos.row * width + start_pos.col
        dist, parent, incoming = _bfs_flat(current_view, start_index)
        reachable = [index for index in _treasure_indices(current_view) if dist[index] >= 0]

        if self._strategy == STRATEGY_GATHER:
            if not reachable:
                return ()
            target = min(
                reachable,
                key=lambda index: (dist[index], -_treasure_value(current_view, index), index),
            )
            return _prefix_flat(start_index, target, parent, incoming, steps)

        if self._strategy == STRATEGY_RATIO:
            best = self._best_ratio(current_view, reachable, dist)
            if best is None:
                return ()
            return _prefix_flat(start_index, best, parent, incoming, steps)

        if self._strategy == STRATEGY_EVADE:
            opponent_pos = _position_of(state_view, actor.opponent)
            opponent_index = -1
            if 0 <= opponent_pos.row < height and 0 <= opponent_pos.col < width:
                opponent_index = opponent_pos.row * width + opponent_pos.col
            sequence = self._evade_flat(
                current_view, start_index, opponent_index, steps, dist, parent, incoming
            )
            return sequence if sequence is not None else ()

        raise AssertionError(f"未处理的策略：{self._strategy}")  # pragma: no cover
