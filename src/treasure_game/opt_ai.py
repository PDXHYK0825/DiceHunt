"""OPT Expecti-Max（NORMAL/HARD）：限深 + 安全 α-β + 记忆化 DP（02 §2 / 06 §3）。

效用口径（ADR-05，最容易做反的地方）
-----------------------------------
* 报告口径 ``eval = scoreHuman − scoreOpt``；
* **搜索内部** ``U = scoreOpt − scoreHuman``：MAX 节点取最大 ``U``、MIN 取最小、叶值 = ``U``。
  直接把 ``eval`` 拿去做 MAX 会让 AI 主动求败。

节点
----
* ``MAX``：当前行动者 = 机器 AI，骰子 ``dice_N`` **已知**（根骰不再掷）；
* ``MIN``：当前行动者 = 人类模型，候选**只由 ViewMap + PublicStateView 生成**
  （:mod:`treasure_game.human_policy`），随后在 ``TrueMap`` 上执行并承担真实后果；
  MIN 在有限策略集中选**对 AI 最不利的真实结果**；
* ``CHANCE``：下一座位回合之前的骰子 1..6，等权 ``1/6`` 求期望；
* 每次状态转移都走 :meth:`PathModule.sim_player_multi_step`（唯一移动真相源）
  与 :meth:`BattleModule.trigger_battle`（回合末对战）。

安全剪枝
--------
* MAX/MIN 兄弟节点做标准 α-β（窗口 = ``U`` 的下/上界），只用**已求得的精确子值**；
* chance 节点默认把六个分支各自传**全窗口** ``(-inf, +inf)`` 求精确值，父节点再剪枝；
* ``OptConfig.chance_pruning=True`` 才启用机会节点区间剪枝：已展开质量 ``S``（子值精确）、
  未展开质量 ``q``，用保守全域 ``[U0-R, U0+R]`` 得到合法区间 ``[S+q·lo, S+q·hi]``；
  仅当上界 ``<= alpha`` 或下界 ``>= beta`` 时返回带 ``UPPER/LOWER`` 标记的**界**
  （绝不当作精确期望）。默认关闭。

记忆化 DP
---------
key 至少含 ``map_id``、规范化座位、两人坐标、双方分数、``remaining_treasures``、
``active_traps``、``revealed_traps``、``turn``、``node_kind``、``known_dice_or_None``、
``remaining_depth``、``mode``、``human_model_id``、``candidate_limit``、``partition_context``；
**只有精确值**才写入缓存（界/超时/部分展开绝不入缓存），每次 :meth:`OptAI.decide` 开始时清空。

「最优」口径（D6）：仅指**给定深度与候选集内最优 + 安全剪枝下与原树同值**，
不是信息不完全博弈的全局最优。
"""

from __future__ import annotations

import dataclasses
import functools
import time

from treasure_game.battle_module import BattleModule
from treasure_game.human_policy import (
    DEFAULT_HUMAN_MODEL_STRATEGY,
    HUMAN_MODEL_ID,
    HUMAN_MODEL_ID_PREFIX,
    HUMAN_MODEL_ID_VERSION,
    HUMAN_MODEL_ID_WORST_CASE,
    STRATEGIES,
    HumanPolicy,
)
from treasure_game.map_module import MapModule
from treasure_game.models import (
    Direction,
    GameState,
    IllegalStateError,
    Position,
    PublicStateView,
    SearchDiagnostics,
    Seat,
    TrueMap,
    ViewMap,
    require_true_map,
)
from treasure_game.path_module import PathModule

__all__ = [
    "OptConfig",
    "OptAI",
    "HUMAN_MODEL_HEURISTIC",
    "HUMAN_MODEL_WORST_CASE",
    "HUMAN_MODEL_ID_WORST_CASE",
    "ROLE_MAX",
    "ROLE_MIN",
    "ROLE_CHANCE",
    "ROLE_LEAF",
    "SEARCH_ALGORITHM",
]

_INF = float("inf")
_TIE_EPS = 1e-9

#: DP key 中的节点类型标签。
ROLE_MAX = "MAX"
ROLE_MIN = "MIN"
ROLE_CHANCE = "CHANCE"
ROLE_LEAF = "LEAF"

#: ``SearchDiagnostics.algorithm`` 取值（单体 OPT）。
SEARCH_ALGORITHM = "opt"

#: ``OptConfig.human_model`` 取值：
#: ``heuristic`` = 单一确定性策略预测人类（默认，MIN 退化为单动作）；
#: ``worst_case`` = 枚举策略集取对 AI 最不利者（旧口径，消融用）。
HUMAN_MODEL_HEURISTIC = "heuristic"
HUMAN_MODEL_WORST_CASE = "worst_case"
_HUMAN_MODELS = (HUMAN_MODEL_HEURISTIC, HUMAN_MODEL_WORST_CASE)

#: 缓存条目上限：超过后只停止写入（不影响正确性），每次 ``decide`` 仍会清空。
_CACHE_LIMIT = 400_000
#: 候选生成缓存上限（同样只影响性能）。
_CAND_CACHE_LIMIT = 100_000

#: 邻序遍历（与 PathModule 冻结邻序一致：上、左、下、右）。
_NEIGHBOR_ORDER: tuple[Direction, ...] = (
    Direction.UP,
    Direction.LEFT,
    Direction.DOWN,
    Direction.RIGHT,
)
_DIRECTION_RANK = {direction: index for index, direction in enumerate(_NEIGHBOR_ORDER)}


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 搜索值：区间包络 [lo, hi]；lo == hi 表示精确值
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True, slots=True)
class _Bound:
    """子树值包络：真实值必落在 ``[lo, hi]``；``lo == hi`` 即精确值。"""

    lo: float
    hi: float

    @property
    def exact(self) -> bool:
        return self.lo == self.hi


def _exact(value: float) -> _Bound:
    return _Bound(value, value)


# ---------------------------------------------------------------------------
# 搜索节点状态（规范化：opt = 当前 AI / MAX，human = 对手 / MIN）
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True, slots=True)
class _Node:
    m: TrueMap
    pos_opt: Position
    pos_human: Position
    s_opt: int
    s_human: int

    @property
    def u(self) -> int:
        return self.s_opt - self.s_human


# ---------------------------------------------------------------------------
# 配置（06 §3 冻结字段 + 允许追加的默认值字段）
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class OptConfig:
    """OPT 配置。

    冻结字段：``depth`` / ``candidate_limit`` / ``use_cache`` / ``use_pruning``。
    追加字段（全部有默认值，不改变冻结语义）：

    * ``chance_pruning``：机会节点区间剪枝，**默认关闭**（安全初版为六面全枚举求精确值）；
    * ``exhaustive_candidates``：候选改为穷举长度 ``0..N`` 的四方向序列并按完整结果去重
      （小地图正确性基线；仍受 ``candidate_limit`` 约束）；
    * ``allowed_targets``：``None`` = 全图；否则只把该集合内的宝藏作为**关注目标**
      （分治局部子问题用；不删真实墙/陷阱/宝藏/对手）；
    * ``partition_context``：并入 DP key，防止局部子问题与全图混用；
    * ``human_model_id``：MIN 人类模型标识（并入 DP key）；
    * ``human_model``：人类建模方式。``"heuristic"``（默认）把人类预测为**按
      `human_model_strategy` 的单一确定性策略**走棋，MIN 随之退化为单动作预测；
      ``"worst_case"`` 恢复旧口径——枚举整个策略集并取对 AI 最不利者。两者产出不同
      ``human_model_id``，缓存不会跨模型复用。
    * ``human_model_strategy``：``human_model="heuristic"`` 时使用的策略名（见
      :data:`treasure_game.human_policy.STRATEGIES`）。
    """

    depth: int = 3
    candidate_limit: int = 24
    use_cache: bool = True
    use_pruning: bool = True
    chance_pruning: bool = False
    exhaustive_candidates: bool = False
    allowed_targets: frozenset[Position] | None = None
    partition_context: str = ""
    human_model_id: str = HUMAN_MODEL_ID
    human_model: str = HUMAN_MODEL_HEURISTIC
    human_model_strategy: str = DEFAULT_HUMAN_MODEL_STRATEGY

    def __post_init__(self) -> None:
        """校验并派生 ``human_model_id``，使其与 (``human_model``, ``human_model_strategy``) 一致。

        冻结字段 ``human_model_id`` 仍可显式覆盖；未显式改（或改回默认值）时按模型推导，
        保证"配置里的标识"与"搜索实际用的模型"一致——否则 DP key 会写着 GATHER 却用 RATIO，
        缓存可能跨模型复用（静默错误）。
        """
        if self.human_model not in _HUMAN_MODELS:
            raise ValueError(
                f"OptConfig.human_model 必须是 {'/'.join(_HUMAN_MODELS)} 之一，"
                f"收到 {self.human_model!r}"
            )
        if self.human_model == HUMAN_MODEL_WORST_CASE:
            expected = HUMAN_MODEL_ID_WORST_CASE
        else:
            strategy = str(self.human_model_strategy).strip().upper()
            if strategy not in STRATEGIES:
                raise ValueError(
                    f"OptConfig.human_model_strategy 未知策略 {self.human_model_strategy!r}；"
                    f"可选：{', '.join(STRATEGIES)}"
                )
            expected = f"{HUMAN_MODEL_ID_PREFIX}:{strategy}:{HUMAN_MODEL_ID_VERSION}"
        if self.human_model_id in (HUMAN_MODEL_ID, HUMAN_MODEL_ID_WORST_CASE):
            object.__setattr__(self, "human_model_id", expected)


def _target_cmp(a: tuple[Position, int, int], b: tuple[Position, int, int]) -> int:
    """候选排序口径（与贪心一致，整数交叉相乘）：比率 desc → 分值 desc → 距离 asc → 坐标 asc。"""
    pos_a, value_a, dist_a = a
    pos_b, value_b, dist_b = b
    left = value_a * dist_b if dist_b > 0 else value_a
    right = value_b * dist_a if dist_a > 0 else value_b
    if left != right:
        return -1 if left > right else 1
    if value_a != value_b:
        return -1 if value_a > value_b else 1
    if dist_a != dist_b:
        return -1 if dist_a < dist_b else 1
    if pos_a != pos_b:
        return -1 if (pos_a.row, pos_a.col) < (pos_b.row, pos_b.col) else 1
    return 0


# ---------------------------------------------------------------------------
# 搜索内核
# ---------------------------------------------------------------------------
class _Search:
    """一次 :meth:`OptAI.decide` 的搜索上下文（缓存按决策界定作用域，避免无界增长）。"""

    __slots__ = (
        "config",
        "root_map",
        "mode",
        "opt_seat",
        "human_seat",
        "opt_is_b",
        "map_id",
        "human",
        "nodes",
        "cache_hits",
        "cache_misses",
        "cache",
        "_bfs_cache",
        "_path_cache",
        "_cand_cache",
        "_view_cache",
        "_root_turn_index",
        "_root_depth",
        "root_value",
        "human_model_id",
    )

    def __init__(self, config: OptConfig, state: GameState, seat: Seat) -> None:
        self.config = config
        self.root_map = require_true_map(state.true_map)
        self.mode = state.mode
        self.opt_seat = seat
        self.human_seat = seat.opponent
        self.opt_is_b = seat is Seat.B
        self.map_id = self.root_map.map_id
        # MIN 的人类模型：策略集口径恒为确定性（rng 仅 MIXED 用）；默认按单一策略预测。
        self.human = HumanPolicy(config.human_model_strategy)
        # 模型标识直接取配置值：OptConfig.__post_init__ 已保证它与 human_model /
        # human_model_strategy 一致（显式覆盖优先）。此处**不再**二次推导，避免两处口径分叉。
        self.human_model_id = config.human_model_id
        self.nodes = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache: dict[tuple, float] = {}
        self._bfs_cache: dict[Position, dict[Position, int]] = {}
        self._path_cache: dict[tuple[Position, Position], tuple[Direction, ...] | None] = {}
        self._cand_cache: dict[tuple, tuple[tuple[Direction, ...], ...]] = {}
        self._view_cache: dict[tuple, ViewMap] = {}
        self._root_turn_index = state.turn_index
        self._root_depth = config.depth
        self.root_value = float("-inf")

    # ------------------------------------------------------------------
    # 静态查询缓存（BFS/最短路只依赖静态底图，可安全跨节点复用）
    # ------------------------------------------------------------------
    def _bfs(self, start: Position) -> dict[Position, int]:
        cached = self._bfs_cache.get(start)
        if cached is None:
            cached = PathModule.bfs_distances(self.root_map, start)
            self._bfs_cache[start] = cached
        return cached

    def _path(self, start: Position, target: Position) -> tuple[Direction, ...] | None:
        key = (start, target)
        if key in self._path_cache:
            return self._path_cache[key]
        path = PathModule.bfs_path(self.root_map, start, target)
        self._path_cache[key] = path
        return path

    def _view(self, m: TrueMap) -> ViewMap:
        """按模式生成人工可见视图（复用 ``MapModule``，不另写第二份脱敏逻辑）。"""
        key = (m.remaining_treasures, m.active_traps, m.revealed_traps)
        cached = self._view_cache.get(key)
        if cached is None:
            cached = MapModule(m).get_view_map(self.mode)
            self._view_cache[key] = cached
        return cached

    # ------------------------------------------------------------------
    # DP key（02 §2「精确 DP key」）
    # ------------------------------------------------------------------
    def _key(self, role: str, node: _Node, dice: int | None, depth: int) -> tuple:
        """精确 DP key（02 §2）：任一成分不同 → 不同 key。

        含 ``map_id``、规范化座位、两人坐标/分数、三个动态资源集合、``turn``（行动座位）、
        ``node_kind``、``known_dice_or_None``、``remaining_depth``、``mode``、
        ``human_model_id``、``candidate_limit``、``partition_context``。
        """
        m = node.m
        if role == ROLE_MAX:
            turn: str | None = self.opt_seat.value
        elif role == ROLE_MIN:
            turn = self.human_seat.value
        else:  # CHANCE / LEAF：不在某个座位手上
            turn = None
        return (
            self.map_id,
            self.opt_seat.value,
            turn,
            role,
            node.pos_opt,
            node.pos_human,
            node.s_opt,
            node.s_human,
            m.remaining_treasures,
            m.active_traps,
            m.revealed_traps,
            dice,
            depth,
            self.mode.value,
            self.human_model_id,
            self.config.candidate_limit,
            self.config.partition_context,
        )

    def _store(self, key: tuple, value: float) -> _Bound:
        """只把**精确值**写入缓存。"""
        if self.config.use_cache and len(self.cache) < _CACHE_LIMIT:
            self.cache[key] = value
        return _exact(value)

    def _lookup(self, key: tuple) -> _Bound | None:
        if not self.config.use_cache:
            return None
        hit = self.cache.get(key)
        if hit is None:
            self.cache_misses += 1
            return None
        self.cache_hits += 1
        return _exact(hit)

    # ------------------------------------------------------------------
    # 叶值：U = scoreOpt − scoreHuman（无额外未声明潜力项）
    # ------------------------------------------------------------------
    def _leaf(self, node: _Node) -> _Bound:
        key = self._key(ROLE_LEAF, node, None, 0)
        if self.config.use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return _exact(hit)
            self.cache_misses += 1
        self.nodes += 1
        return self._store(key, float(node.s_opt - node.s_human))

    # ------------------------------------------------------------------
    # 状态转移：Path（唯一移动真相源）→ 回合末 Battle
    # ------------------------------------------------------------------
    def _apply(
        self, node: _Node, actor_is_opt: bool, seq: tuple[Direction, ...], dice: int
    ) -> tuple[_Node, object]:
        m = node.m
        if actor_is_opt:
            result = PathModule.sim_player_multi_step(m, node.pos_opt, seq, dice, actor=self.opt_seat)
            pos_opt, s_opt = result.final_pos, node.s_opt + result.score_delta
            pos_human, s_human = node.pos_human, node.s_human
        else:
            result = PathModule.sim_player_multi_step(
                m, node.pos_human, seq, dice, actor=self.human_seat
            )
            pos_human, s_human = result.final_pos, node.s_human + result.score_delta
            pos_opt, s_opt = node.pos_opt, node.s_opt
        new_map = result.true_map

        # 回合末对战：唯一入口 BattleModule（未重合时 triggered=False、分数位置全不变）
        if self.opt_is_b:
            battle = BattleModule.trigger_battle(
                new_map, pos_human, pos_opt, s_human, s_opt,
                actor_a=self.human_seat, actor_b=self.opt_seat,
            )
            pos_human, pos_opt = battle.pos_a, battle.pos_b
            s_human, s_opt = battle.score_a, battle.score_b
        else:
            battle = BattleModule.trigger_battle(
                new_map, pos_opt, pos_human, s_opt, s_human,
                actor_a=self.opt_seat, actor_b=self.human_seat,
            )
            pos_opt, pos_human = battle.pos_a, battle.pos_b
            s_opt, s_human = battle.score_a, battle.score_b
        return _Node(new_map, pos_opt, pos_human, s_opt, s_human), result

    def _expand(
        self,
        node: _Node,
        candidates: tuple[tuple[Direction, ...], ...] | list[tuple[Direction, ...]],
        actor_is_opt: bool,
        dice: int,
    ) -> list[tuple[_Node, object, tuple[Direction, ...], int]]:
        """展开候选并按**完整结果**去重（同结果只保留首个候选）。"""
        expanded: list[tuple[_Node, object, tuple[Direction, ...], int]] = []
        seen: set[tuple] = set()
        for seq in candidates:
            child, result = self._apply(node, actor_is_opt, seq, dice)
            m2 = child.m
            outcome = (
                child.pos_opt,
                child.pos_human,
                child.s_opt,
                child.s_human,
                m2.remaining_treasures,
                m2.active_traps,
                m2.revealed_traps,
            )
            if outcome in seen:
                continue
            seen.add(outcome)
            expanded.append((child, result, seq, child.u))
        return expanded

    # ------------------------------------------------------------------
    # 候选生成
    # ------------------------------------------------------------------
    def _ordered_targets(self, m: TrueMap, start: Position) -> list[tuple[Position, int, int]]:
        """当前关注目标（``allowed_targets`` 过滤后）按贪心口径排序；不可达者剔除。"""
        distances = self._bfs(start)
        allowed = self.config.allowed_targets
        entries: list[tuple[Position, int, int]] = []
        for target in m.remaining_treasures:
            if allowed is not None and target not in allowed:
                continue
            distance = distances.get(target)
            if distance is None:
                continue
            entries.append((target, m.treasure_values[target], distance))
        entries.sort(key=functools.cmp_to_key(_target_cmp))
        return entries

    def _evade_sequence(
        self, m: TrueMap, start: Position, opponent: Position, dice: int
    ) -> tuple[Direction, ...] | None:
        """``dice`` 步内可达格中"离对手最远"者（并列取 (row,col) 小）的最短路前缀。"""
        if dice <= 0:
            return None
        distances = self._bfs(start)
        from_opponent = self._bfs(opponent)
        unreachable = m.height * m.width + 1
        best: Position | None = None
        best_key: tuple[int, int, int] | None = None
        for pos, distance in distances.items():
            if distance <= 0 or distance > dice:
                continue
            key = (-from_opponent.get(pos, unreachable), pos.row, pos.col)
            if best_key is None or key < best_key:
                best, best_key = pos, key
        if best is None:
            return None
        path = self._path(start, best)
        if not path:
            return None
        return tuple(path[:dice])

    def _opt_candidates(
        self, m: TrueMap, pos_opt: Position, pos_human: Position, dice: int
    ) -> tuple[tuple[Direction, ...], ...]:
        """AI（MAX）候选：空动作 + 宝藏前缀 + 靠近对手 + 远离对手（确定性顺序）。

        ``exhaustive_candidates=True`` 时改为穷举长度 ``0..N`` 的四方向序列
        （小地图正确性基线），仍按 ``candidate_limit`` 截断。
        """
        if dice <= 0:
            return ((),)
        key = (
            pos_opt,
            pos_human,
            dice,
            m.remaining_treasures,
            self.config.allowed_targets,
            self.config.exhaustive_candidates,
            self.config.candidate_limit,
        )
        cached = self._cand_cache.get(key)
        if cached is not None:
            return cached

        candidates: list[tuple[Direction, ...]] = [()]

        def push(sequence: tuple[Direction, ...] | None) -> None:
            if sequence and sequence not in candidates:
                candidates.append(sequence)

        if self.config.exhaustive_candidates:
            frontier: list[tuple[Direction, ...]] = [()]
            pool: list[tuple[Direction, ...]] = []
            for _length in range(1, dice + 1):
                nxt: list[tuple[Direction, ...]] = []
                for prefix in frontier:
                    for direction in _NEIGHBOR_ORDER:
                        nxt.append(prefix + (direction,))
                pool.extend(nxt)
                frontier = nxt
            pool.sort(key=lambda s: (len(s), tuple(_DIRECTION_RANK[d] for d in s)))
            for sequence in pool:
                if sequence not in candidates:
                    candidates.append(sequence)
        else:
            for target, _value, _distance in self._ordered_targets(m, pos_opt):
                path = self._path(pos_opt, target)
                if path:
                    push(tuple(path[:dice]))
            approach = self._path(pos_opt, pos_human)
            if approach:
                push(tuple(approach[:dice]))
            push(self._evade_sequence(m, pos_opt, pos_human, dice))

        limit = self.config.candidate_limit
        if limit > 0 and len(candidates) > limit:
            candidates = candidates[:limit]
        result = tuple(candidates)
        if len(self._cand_cache) < _CAND_CACHE_LIMIT:
            self._cand_cache[key] = result
        return result

    # ------------------------------------------------------------------
    # 公开信息视图（MIN 唯一可见输入）
    # ------------------------------------------------------------------
    def _human_candidates(
        self, view: ViewMap, public: PublicStateView, dice: int
    ) -> tuple[tuple[Direction, ...], ...]:
        """MIN 节点的人类动作集合 —— 取决于 ``config.human_model``。

        * ``heuristic``（默认）：按单一确定性策略**预测一个动作**。MIN 遍历因此只有一项，
          语义上等价于"照预测走"，不再假设人类会挑最伤 AI 的一手。人类仍只看到
          ``ViewMap``（隐藏陷阱对它不可见），真实后果由 ``_expand`` 在 ``TrueMap`` 上结算。
        * ``worst_case``：返回整个确定性策略集，由 :meth:`search_min` 取对 AI 最不利者。

        两者都**不接触真值图**，视图-真值隔离不变。
        """
        if self.config.human_model == HUMAN_MODEL_WORST_CASE:
            return tuple(self.human.candidate_actions(view, self.human_seat, dice, public))
        predicted = self.human.choose(view, self.human_seat, dice, public)
        return (tuple(predicted),)

    def _public(self, node: _Node, dice: int, depth: int) -> PublicStateView:
        if self.opt_is_b:
            pos_a, pos_b = node.pos_human, node.pos_opt
            score_a, score_b = node.s_human, node.s_opt
        else:
            pos_a, pos_b = node.pos_opt, node.pos_human
            score_a, score_b = node.s_opt, node.s_human
        return PublicStateView(
            mode=self.mode,
            pos_a=pos_a,
            pos_b=pos_b,
            score_a=score_a,
            score_b=score_b,
            turn=self.human_seat,
            turn_index=self._root_turn_index + (self._root_depth - depth),
            dice_N=dice,
            remaining_treasure_count=len(node.m.remaining_treasures),
        )

    # ------------------------------------------------------------------
    # MAX（AI 回合，骰子已知）
    # ------------------------------------------------------------------
    def search_max(self, node: _Node, dice: int, depth: int, alpha: float, beta: float) -> _Bound:
        if depth <= 0 or node.m.is_finished():
            return self._leaf(node)
        key = self._key(ROLE_MAX, node, dice, depth)
        cached = self._lookup(key)
        if cached is not None:
            return cached
        self.nodes += 1

        candidates = self._opt_candidates(node.m, node.pos_opt, node.pos_human, dice)
        expanded = self._expand(node, candidates, True, dice)
        # 走子排序只影响剪枝效率、不影响值：即时 U 高者优先。
        expanded.sort(key=lambda item: -item[3])

        pruning = self.config.use_pruning
        low = -_INF
        high = -_INF
        pruned = False
        for child, _result, _seq, _u in expanded:
            if pruning and low >= beta:
                pruned = True
                break
            child_alpha = low if (pruning and low > alpha) else alpha
            if depth - 1 <= 0 or child.m.is_finished():
                bound = self._leaf(child)
            else:
                bound = self.search_chance(child, depth - 1, child_alpha, beta)
            if bound.lo > low:
                low = bound.lo
            if bound.hi > high:
                high = bound.hi

        if pruned or low >= beta:
            return _Bound(low, _INF)  # LOWER：真实值 >= low >= beta
        if low == high:
            return self._store(key, low)
        if high <= alpha:
            return _Bound(-_INF, high)  # UPPER：真实值 <= high <= alpha
        return _Bound(low, high)

    # ------------------------------------------------------------------
    # MIN（人类模型回合，候选只来自 ViewMap + PublicStateView）
    # ------------------------------------------------------------------
    def search_min(self, node: _Node, dice: int, depth: int, alpha: float, beta: float) -> _Bound:
        if depth <= 0 or node.m.is_finished():
            return self._leaf(node)
        key = self._key(ROLE_MIN, node, dice, depth)
        cached = self._lookup(key)
        if cached is not None:
            return cached
        self.nodes += 1

        view = self._view(node.m)
        public = self._public(node, dice, depth)
        candidates = self._human_candidates(view, public, dice)
        expanded = self._expand(node, candidates, False, dice)
        # 对 AI 最不利（即时 U 最小）者优先展开。
        expanded.sort(key=lambda item: item[3])

        pruning = self.config.use_pruning
        high = _INF
        low = _INF
        pruned = False
        for child, _result, _seq, _u in expanded:
            if pruning and high <= alpha:
                pruned = True
                break
            child_beta = high if (pruning and high < beta) else beta
            if depth - 1 <= 0 or child.m.is_finished():
                bound = self._leaf(child)
            else:
                bound = self.search_chance(child, depth - 1, alpha, child_beta)
            if bound.lo < low:
                low = bound.lo
            if bound.hi < high:
                high = bound.hi

        if pruned or high <= alpha:
            return _Bound(-_INF, high)  # UPPER：真实值 <= high <= alpha
        if low == high:
            return self._store(key, low)
        if low >= beta:
            return _Bound(low, _INF)  # LOWER
        return _Bound(low, high)

    # ------------------------------------------------------------------
    # CHANCE（骰子 1..6 等权）
    # ------------------------------------------------------------------
    def search_chance(self, node: _Node, depth: int, alpha: float, beta: float) -> _Bound:
        if depth <= 0 or node.m.is_finished():
            return self._leaf(node)
        key = self._key(ROLE_CHANCE, node, None, depth)
        cached = self._lookup(key)
        if cached is not None:
            return cached
        self.nodes += 1

        m = node.m
        # 保守全域 [U0-R, U0+R]：两玩家未来最多各取一次资源 + 每个后续回合最多 ±10 分差。
        treasure_sum = sum(m.treasure_values[pos] for pos in m.remaining_treasures)
        trap_sum = sum(-m.trap_values[pos] for pos in m.active_traps)
        budget = 2 * (treasure_sum + trap_sum) + 10 * (depth + 1)
        lo_global = node.u - budget
        hi_global = node.u + budget

        prune_chance = self.config.use_pruning and self.config.chance_pruning
        total = 0.0
        for faces, dice in enumerate(range(1, 7), start=1):
            # 安全初版：每个机会分支都传全窗口，保证子值精确（区间才可用作合法包络）。
            bound = self.search_min(node, dice, depth, -_INF, _INF)
            total += bound.lo
            if prune_chance:
                remaining_faces = 6 - faces
                lo_bound = (total + remaining_faces * lo_global) / 6.0
                hi_bound = (total + remaining_faces * hi_global) / 6.0
                if hi_bound <= alpha:
                    return _Bound(-_INF, hi_bound)  # UPPER：绝不当作精确期望
                if lo_bound >= beta:
                    return _Bound(lo_bound, _INF)  # LOWER
        return self._store(key, total / 6.0)

    # ------------------------------------------------------------------
    # 根节点：对所有根候选求**精确值**后按 tie-break 选动作
    # ------------------------------------------------------------------
    def _nearest_treasure_distance(self, m: TrueMap, pos: Position) -> float:
        remaining = m.remaining_treasures
        if not remaining:
            return 0.0
        distances = self._bfs(pos)
        best: float = _INF
        for target in remaining:
            distance = distances.get(target)
            if distance is not None and distance < best:
                best = float(distance)
        return best if best < _INF else float(m.height * m.width + 1)

    def run(self, state: GameState, dice: int) -> tuple[Direction, ...]:
        node = _Node(
            self.root_map,
            state.position_of(self.opt_seat),
            state.position_of(self.human_seat),
            state.score_of(self.opt_seat),
            state.score_of(self.human_seat),
        )
        depth = self.config.depth
        candidates = self._opt_candidates(node.m, node.pos_opt, node.pos_human, dice)
        expanded = self._expand(node, candidates, True, dice)
        expanded.sort(key=lambda item: -item[3])  # 即时 U 高者优先，便于尽早收紧 alpha

        pruning = self.config.use_pruning
        best_lower = -_INF
        results: list[tuple[float, _Node, object, tuple[Direction, ...]]] = []
        for child, result, seq, _u in expanded:
            child_alpha = best_lower if (pruning and best_lower > -_INF) else -_INF
            if depth - 1 <= 0 or child.m.is_finished():
                bound = self._leaf(child)
            else:
                bound = self.search_chance(child, depth - 1, child_alpha, _INF)
            if not bound.exact:
                # 界不能直接用于 tie-break：先排除"严格更差"，其余用全窗口重搜求精确值。
                if bound.hi < best_lower - _TIE_EPS:
                    continue
                bound = self.search_chance(child, depth - 1, -_INF, _INF)
            value = bound.lo
            if value > best_lower:
                best_lower = value
            results.append((value, child, result, seq))
        if not results:  # 理论不可达：候选至少含空动作
            raise IllegalStateError("OPT 根节点没有可评估的候选动作")  # pragma: no cover

        self.root_value = best_lower
        tied = [item for item in results if item[0] >= best_lower - _TIE_EPS]

        def tie_key(item: tuple[float, _Node, object, tuple[Direction, ...]]) -> tuple:
            _value, child, result, seq = item
            distance = self._nearest_treasure_distance(child.m, result.final_pos)
            rank = tuple(_DIRECTION_RANK[direction] for direction in seq)
            return (distance, result.valid_steps, rank)

        return min(tied, key=tie_key)[3]


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------
class OptAI:
    """NORMAL/HARD 的 Expecti-Max AI（``decide`` 返回本回合方向序列）。

    每次 ``decide`` 新建搜索上下文：DP 缓存按决策界定作用域，避免无界增长；
    ``diagnostics().root_final_pos/root_score_delta`` 由返回序列再走一次
    ``PathModule.sim_player_multi_step`` 得到，保证"预测 = 重放"。
    """

    __slots__ = ("_config", "_diagnostics", "_root_utility")

    def __init__(self, config: OptConfig | None = None) -> None:
        if config is not None and not isinstance(config, OptConfig):
            raise ValueError(f"config 必须是 OptConfig，收到 {type(config).__name__}")
        self._config = config if config is not None else OptConfig()
        self._diagnostics: SearchDiagnostics | None = None
        self._root_utility: float | None = None

    @property
    def config(self) -> OptConfig:
        return self._config

    # ------------------------------------------------------------------
    def decide(
        self, state: GameState, dice_N: int, seat: Seat = Seat.B
    ) -> tuple[Direction, ...]:
        """返回本回合的动作序列（长度 <= ``dice_N``），内部推演全部走 Path 纯内核。"""
        current_state = _require_state(state)
        steps = _require_dice(dice_N)
        actor = _require_seat(seat)
        true_map = require_true_map(current_state.true_map)
        depth = self._config.depth
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
            raise ValueError(f"OptConfig.depth 必须是 >= 1 的 int，收到 {depth!r}")
        limit = self._config.candidate_limit
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f"OptConfig.candidate_limit 必须是 int，收到 {limit!r}")
        model = self._config.human_model
        if model not in _HUMAN_MODELS:
            raise ValueError(
                f"OptConfig.human_model 必须是 {'/'.join(_HUMAN_MODELS)} 之一，收到 {model!r}"
            )

        start_ns = time.perf_counter_ns()
        search = _Search(self._config, current_state, actor)
        sequence = search.run(current_state, steps)
        elapsed_ns = time.perf_counter_ns() - start_ns

        # 根预测 = 把返回序列交给 Path 重放（唯一移动真相源）。
        replay = PathModule.sim_player_multi_step(
            true_map, current_state.position_of(actor), sequence, steps, actor=actor
        )
        self._root_utility = search.root_value
        self._diagnostics = SearchDiagnostics(
            algorithm=SEARCH_ALGORITHM,
            nodes=search.nodes,
            cache_hits=search.cache_hits,
            cache_misses=search.cache_misses,
            elapsed_ns=elapsed_ns,
            root_move_seq=tuple(sequence),
            root_final_pos=replay.final_pos,
            root_score_delta=replay.score_delta,
            approximate=False,
            depth=depth,
        )
        return tuple(sequence)

    # ------------------------------------------------------------------
    # 诊断与根效用（追加 API，不改冻结签名）
    # ------------------------------------------------------------------
    def diagnostics(self) -> SearchDiagnostics:
        """最近一次 ``decide`` 的可观察证据；未决策过则抛 ``IllegalStateError``。"""
        if self._diagnostics is None:
            raise IllegalStateError("尚未执行任何决策：请先调用 OptAI.decide(...)")
        return self._diagnostics

    def root_utility(self) -> float:
        """根节点**搜索内部**效用 ``U = scoreOpt − scoreHuman``（精确值）。"""
        if self._root_utility is None:
            raise IllegalStateError("尚未执行任何决策：请先调用 OptAI.decide(...)")
        return self._root_utility

    def root_eval(self) -> float:
        """报告口径 ``eval = scoreHuman − scoreOpt = −U``。"""
        return -self.root_utility()
