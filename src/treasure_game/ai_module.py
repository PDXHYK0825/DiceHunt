"""AI 模块（spec §四 3 / 06 §3）：两套 AI 的统一入口 + 模式路由 + 诊断。

路由（spec §四 5、06 §3）

* :meth:`AiModule.get_greedy_move` → **永远是贪心**（EASY 模式机器对手）；
* :meth:`AiModule.get_opt_move` → **永远是 OPT**（NORMAL/HARD），
  是否走分治由面积阈值 ``V >= partition_threshold`` 与 ``use_partition`` 决定；
  具体选哪套 AI 由 ``Simulator`` 依模式决定，本模块不自作主张降级。

诊断

* ``get_diagnostics()`` 仅在**至少一次决策之后**有效，否则抛 ``IllegalStateError``；
* ``root_final_pos`` / ``root_score_delta`` 由返回序列再交给
  :meth:`PathModule.sim_player_multi_step` 重放得到（唯一移动真相源，预测 = 重放）；
* 走分治时 ``algorithm == "opt+partition"`` 且 ``approximate=True``。

附加（追加 API，不改冻结签名）：``get_root_utility()`` / ``get_root_eval()`` 暴露搜索内部
``U = scoreOpt − scoreHuman``（报告口径 ``eval = −U``）；``partition_report()`` 返回分块统计。
"""

from __future__ import annotations

import dataclasses
import random
import time

from treasure_game.divide_conquer_module import (
    DEFAULT_PARTITION_THRESHOLD,
    DivideConquerModule,
    PartitionReport,
)
from treasure_game.greedy_ai import GreedyAI
from treasure_game.human_policy import DEFAULT_HUMAN_MODEL_STRATEGY
from treasure_game.models import (
    Direction,
    GameMode,
    GameState,
    IllegalStateError,
    Position,
    SearchDiagnostics,
    Seat,
    require_true_map,
)
from treasure_game.opt_ai import HUMAN_MODEL_HEURISTIC, OptAI, OptConfig
from treasure_game.path_module import PathModule

__all__ = ["AiModule", "ALGORITHM_GREEDY", "ALGORITHM_OPT", "ALGORITHM_PARTITION",
           "HUMAN_MODEL_HEURISTIC"]

ALGORITHM_GREEDY = "greedy"
ALGORITHM_OPT = "opt"
ALGORITHM_PARTITION = "opt+partition"


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


class AiModule:
    """两套 AI 的统一入口（贪心 / OPT，含大地图分治近似）。

    参数
    ----
    depth, candidate_limit, use_cache, use_pruning:
        直接用于构造 :class:`OptConfig`（默认 ``3 / 24 / True / True``）。
    partition_threshold:
        面积 ``V >= threshold``（默认 144）时 :meth:`get_opt_move` 才走分治入口。
    rng:
        预留给实验侧随机人类模型；ADR-08 要求搜索不消耗 RNG，故 OPT/贪心路径完全确定性。
    use_partition:
        **追加参数**（默认 ``True``）：``False`` 时强制单体 OPT（``Simulator`` 的 ``SeatPolicy.OPT``
        可用它区分 ``OPT_PARTITION``）；阈值判定仍然生效。
    exhaustive_candidates / chance_pruning:
        **追加参数**，透传给 :class:`OptConfig`（小图正确性基线与机会节点区间剪枝，默认关闭）。
    local_depth_drop / local_candidate_divisor:
        **追加参数**，分治**局部子问题**的规模控制：局部深度 = ``max(1, depth - local_depth_drop)``、
        局部候选上限 = ``max(4, candidate_limit // local_candidate_divisor)``
        （``candidate_limit <= 0`` 时保持"不限"）。分治的意义是"牺牲精度换速度"（02 §3）：
        全图深度 ``d`` 的搜索在 ``C`` 个块上重复执行，若每块都与全图同规模则反而更慢；
        局部子问题只负责"本块关注目标"的比较，最终由**全图** Path → Battle 合并评分。
        这就是 02 §3 中"每块候选至多 ``K_c``"的工程实现。
    """

    __slots__ = (
        "_config",
        "_opt",
        "_greedy",
        "_partition",
        "_use_partition",
        "_rng",
        "_diagnostics",
        "_root_utility",
        "_local_nodes",
        "_local_hits",
        "_local_misses",
        "_local_seat",
        "_local_depth_drop",
        "_local_candidate_divisor",
        "_calculation_details",
    )

    def __init__(
        self,
        depth: int = 3,
        candidate_limit: int = 24,
        partition_threshold: int = DEFAULT_PARTITION_THRESHOLD,
        use_cache: bool = True,
        use_pruning: bool = True,
        rng: random.Random | None = None,
        *,
        use_partition: bool = True,
        exhaustive_candidates: bool = False,
        chance_pruning: bool = False,
        local_depth_drop: int = 1,
        local_candidate_divisor: int = 2,
        human_model: str = HUMAN_MODEL_HEURISTIC,
        human_model_strategy: str = DEFAULT_HUMAN_MODEL_STRATEGY,
    ) -> None:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
            raise ValueError(f"depth 必须是 >= 1 的 int，收到 {depth!r}")
        if isinstance(candidate_limit, bool) or not isinstance(candidate_limit, int):
            raise ValueError(f"candidate_limit 必须是 int，收到 {candidate_limit!r}")
        if (
            isinstance(partition_threshold, bool)
            or not isinstance(partition_threshold, int)
            or partition_threshold <= 0
        ):
            raise ValueError(f"partition_threshold 必须是正整数，收到 {partition_threshold!r}")
        if rng is not None and not isinstance(rng, random.Random):
            raise ValueError(f"rng 必须是 random.Random，收到 {type(rng).__name__}")
        if (
            isinstance(local_depth_drop, bool)
            or not isinstance(local_depth_drop, int)
            or local_depth_drop < 0
        ):
            raise ValueError(f"local_depth_drop 必须是 >= 0 的 int，收到 {local_depth_drop!r}")
        if (
            isinstance(local_candidate_divisor, bool)
            or not isinstance(local_candidate_divisor, int)
            or local_candidate_divisor < 1
        ):
            raise ValueError(
                f"local_candidate_divisor 必须是 >= 1 的 int，收到 {local_candidate_divisor!r}"
            )
        self._config = OptConfig(
            depth=depth,
            candidate_limit=candidate_limit,
            use_cache=use_cache,
            use_pruning=use_pruning,
            chance_pruning=chance_pruning,
            exhaustive_candidates=exhaustive_candidates,
            human_model=human_model,
            human_model_strategy=human_model_strategy,
        )
        self._opt = OptAI(self._config)
        self._greedy = GreedyAI()
        self._partition = DivideConquerModule(partition_threshold=partition_threshold)
        self._use_partition = bool(use_partition)
        self._rng = rng
        self._diagnostics: SearchDiagnostics | None = None
        self._root_utility: float | None = None
        self._local_nodes = 0
        self._local_hits = 0
        self._local_misses = 0
        self._local_seat: Seat = Seat.B
        self._local_depth_drop = local_depth_drop
        self._local_candidate_divisor = local_candidate_divisor
        self._calculation_details: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # 配置查询（追加 API）
    # ------------------------------------------------------------------
    @property
    def config(self) -> OptConfig:
        return self._config

    @property
    def use_partition(self) -> bool:
        return self._use_partition

    @property
    def partition_threshold(self) -> int:
        return self._partition.partition_threshold

    # ------------------------------------------------------------------
    # 贪心（EASY）
    # ------------------------------------------------------------------
    def get_greedy_move(
        self, state: GameState, dice_N: int, seat: Seat = Seat.B
    ) -> tuple[Direction, ...]:
        """贪心 AI 决策（永远不走分治、不做博弈推演）。"""
        current_state = _require_state(state)
        steps = _require_dice(dice_N)
        actor = _require_seat(seat)
        true_map = require_true_map(current_state.true_map)

        start_ns = time.perf_counter_ns()
        sequence = self._greedy.decide(current_state, steps, actor)
        replay = PathModule.sim_player_multi_step(
            true_map, current_state.position_of(actor), sequence, steps, actor=actor
        )
        elapsed_ns = time.perf_counter_ns() - start_ns
        self._diagnostics = SearchDiagnostics(
            algorithm=ALGORITHM_GREEDY,
            nodes=0,
            cache_hits=0,
            cache_misses=0,
            elapsed_ns=elapsed_ns,
            root_move_seq=tuple(sequence),
            root_final_pos=replay.final_pos,
            root_score_delta=replay.score_delta,
            approximate=False,
            depth=0,
        )
        self._root_utility = None
        self._record_calculation(current_state)
        return tuple(sequence)

    # ------------------------------------------------------------------
    # OPT（NORMAL/HARD，可选分治）
    # ------------------------------------------------------------------
    def get_opt_move(
        self, state: GameState, dice_N: int, seat: Seat = Seat.B
    ) -> tuple[Direction, ...]:
        """OPT 决策；面积 ``V >= partition_threshold`` 且 ``use_partition`` 时走分治近似。"""
        current_state = _require_state(state)
        steps = _require_dice(dice_N)
        actor = _require_seat(seat)
        true_map = require_true_map(current_state.true_map)
        area = true_map.height * true_map.width

        start_ns = time.perf_counter_ns()
        if self._use_partition and area >= self._partition.partition_threshold:
            self._local_nodes = 0
            self._local_hits = 0
            self._local_misses = 0
            self._local_seat = actor
            sequence = self._partition.solve(current_state, steps, actor, self._solve_local)
            report = self._partition.last_report()
            replay = PathModule.sim_player_multi_step(
                true_map, current_state.position_of(actor), sequence, steps, actor=actor
            )
            elapsed_ns = time.perf_counter_ns() - start_ns
            self._diagnostics = SearchDiagnostics(
                algorithm=ALGORITHM_PARTITION,
                nodes=self._local_nodes,
                cache_hits=self._local_hits,
                cache_misses=self._local_misses,
                elapsed_ns=elapsed_ns,
                root_move_seq=tuple(sequence),
                root_final_pos=replay.final_pos,
                root_score_delta=replay.score_delta,
                approximate=True,  # 分治 = 近似（02 §3）
                depth=self._config.depth,
            )
            # 分治合并的即时 U（单层评分口径，非全树搜索值）。
            self._root_utility = float(report.merged_u)
            self._record_calculation(current_state, self._partition.root_candidates())
            return tuple(sequence)

        sequence = self._opt.decide(current_state, steps, actor)
        self._diagnostics = self._opt.diagnostics()
        self._root_utility = self._opt.root_utility()
        self._record_calculation(current_state, self._opt.root_candidates())
        return tuple(sequence)

    def _solve_local(
        self, state: GameState, dice_N: int, allowed_positions: frozenset[Position]
    ) -> tuple[Direction, ...]:
        """分治注入用局部求解回调：只限制关注目标集合，并把局部子问题限制在更小规模。

        局部配置 = 全局配置 + ``allowed_targets``/``partition_context`` +
        更浅的深度（``depth - local_depth_drop``）与更小的候选上限
        （``candidate_limit // local_candidate_divisor``）：块内只做"关注目标"的比较，
        真正的取舍由 :class:`DivideConquerModule` 在全图上合并评分。
        """
        allowed = frozenset(allowed_positions)
        tag = "local:" + ",".join(
            f"{pos.row}.{pos.col}" for pos in sorted(allowed, key=lambda p: (p.row, p.col))
        )
        local_depth = max(1, self._config.depth - self._local_depth_drop)
        if self._config.candidate_limit > 0:
            local_limit = max(4, self._config.candidate_limit // self._local_candidate_divisor)
        else:
            local_limit = 0
        config = dataclasses.replace(
            self._config,
            depth=local_depth,
            candidate_limit=local_limit,
            allowed_targets=allowed,
            partition_context=tag,
        )
        local_ai = OptAI(config)
        sequence = local_ai.decide(state, dice_N, self._local_seat)
        diagnostics = local_ai.diagnostics()
        self._local_nodes += diagnostics.nodes
        self._local_hits += diagnostics.cache_hits
        self._local_misses += diagnostics.cache_misses
        return sequence

    def local_depth(self) -> int:
        """分治局部子问题实际使用的深度（追加 API，便于实验记录）。"""
        return max(1, self._config.depth - self._local_depth_drop)

    def local_candidate_limit(self) -> int:
        """分治局部子问题实际使用的候选上限（追加 API，便于实验记录）。"""
        if self._config.candidate_limit <= 0:
            return 0
        return max(4, self._config.candidate_limit // self._local_candidate_divisor)

    # ------------------------------------------------------------------
    # 诊断（追加 API + 冻结 API）
    # ------------------------------------------------------------------
    def get_calculation_details(self) -> tuple[str, ...]:
        """最近一次决策的实际计算记录；困难模式不公开隐藏信息相关的估值。"""
        self.get_diagnostics()
        return self._calculation_details

    def _record_calculation(
        self,
        state: GameState,
        candidates: tuple[tuple[tuple[Direction, ...], float, bool], ...] = (),
    ) -> None:
        diagnostics = self.get_diagnostics()
        hidden = state.mode is GameMode.HARD

        def moves(sequence: tuple[Direction, ...]) -> str:
            return " ".join(direction.value for direction in sequence) or "end（停留）"

        lines = [f"算法：{diagnostics.algorithm}；方向 w/a/s/d = 上/左/下/右。"]
        if diagnostics.algorithm == ALGORITHM_GREEDY:
            lines.append("计算方法：BFS 求可达宝藏距离，按分值/距离比较；同分优先分值高、距离短、坐标小。")
            lines.extend(self._greedy.calculation_details())
        else:
            limit = str(self._config.candidate_limit) if self._config.candidate_limit > 0 else "不限"
            lines.append(
                f"搜索深度 {diagnostics.depth}；每个节点候选上限 {limit}；"
                f"对手模型 {self._config.human_model} / {self._config.human_model_strategy}；"
                "未来骰子按 1～6 等概率推演。"
            )
            lines.append(
                f"α-β 剪枝：{'开启' if self._config.use_pruning else '关闭'}；"
                f"记忆化缓存：{'开启' if self._config.use_cache else '关闭'}；"
                f"机会节点剪枝：{'开启' if self._config.use_pruning and self._config.chance_pruning else '关闭'}。"
            )
            if diagnostics.approximate:
                report = self._partition.last_report()
                lines.append(
                    f"分治近似：{report.blocks} 个块，其中 {report.blocks_with_resources} 个含宝藏；"
                    f"局部深度 {self.local_depth()}，局部候选上限 {self.local_candidate_limit()}；"
                    f"局部候选 {report.local_candidates}，跨块候选 {report.cross_block_candidates}，"
                    f"合并比较 {report.merged_candidates} 个方案。"
                )
            lines.append(
                f"本轮实际比较 {len(candidates)} 个根候选；"
                + ("困难模式隐藏候选估值及预计得分。" if hidden else
                   "U = AI 分数 − 对手分数；" + ("合并使用即时分差。" if diagnostics.approximate else "搜索值用于比较未来分差。"))
            )
            for sequence, value, exact in candidates:
                selected = "【选中】" if sequence == diagnostics.root_move_seq else ""
                estimate = "" if hidden else (
                    f"：U = {value:.3f}" if exact else f"：U ≤ {value:.3f}（上界已较差，剪枝排除）"
                )
                lines.append(f"候选 {moves(sequence)}{estimate}{selected}")
            lines.append("选择依据：分差估值最高；同分时依次比较距宝藏距离、有效步数、方向顺序。")
        lines.append(f"最终动作：{moves(diagnostics.root_move_seq)}。")
        if not hidden:
            pos = diagnostics.root_final_pos
            lines.append(
                f"预计移动终点：第 {pos.row + 1} 行第 {pos.col + 1} 列；"
                f"本轮移动预计得分 {diagnostics.root_score_delta:+d}（未含终点对战）。"
            )
        lines.append(
            f"搜索统计：节点 {diagnostics.nodes}，缓存命中 {diagnostics.cache_hits}，"
            f"未命中 {diagnostics.cache_misses}；算法耗时 {diagnostics.elapsed_ns / 1e6:.3f} ms。"
        )
        self._calculation_details = tuple(lines)

    def get_diagnostics(self) -> SearchDiagnostics:
        """最近一次决策的诊断；未决策过（构造后直接调用）抛 ``IllegalStateError``。"""
        if self._diagnostics is None:
            raise IllegalStateError("尚未执行任何决策：请先调用 get_greedy_move / get_opt_move")
        return self._diagnostics

    def get_root_utility(self) -> float:
        """搜索内部根效用 ``U = scoreOpt − scoreHuman``（贪心路径没有，抛 ``IllegalStateError``）。"""
        if self._root_utility is None:
            raise IllegalStateError("当前没有 OPT 搜索效用：最近一次决策不是 get_opt_move")
        return self._root_utility

    def get_root_eval(self) -> float:
        """报告口径 ``eval = scoreHuman − scoreOpt = −U``。"""
        return -self.get_root_utility()

    def partition_report(self) -> PartitionReport | None:
        """最近一次分治求解的分块统计（未走分治则为 ``None``）。"""
        if self._diagnostics is not None and self._diagnostics.algorithm == ALGORITHM_PARTITION:
            return self._partition.last_report()
        return None
