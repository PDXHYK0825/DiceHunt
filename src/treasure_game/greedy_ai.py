"""贪心 AI（EASY 模式，spec §四 3-1 / 02 §1 / 06 §3）。

规则（逐条对应 02 §1）：

* 从当前机器座位对**剩余宝藏**做 BFS，只用可达者；距离 = 成功移动数（不按撞墙尝试数计）。
* 性价比 ``value / max(distance, 1)``；比较用**整数交叉相乘**，避免浮点误差。
* tie-break 顺序：比率高 → 宝藏分值高 → 距离短 → 目标 ``(row, col)`` 字典序小。
* 输出 ``PathModule.bfs_path`` 的**前缀**（截到 ``dice_N`` 条）；无宝藏/不可达返回空序列。

本模块只依赖 ``models`` 与 ``path_module``：不预判对手、不做博弈推演、不考虑沿途陷阱，
也**不使用**分治（spec §四 6：贪心 AI 不需要分治优化）。所有推演都走 ``PathModule`` 纯内核。
"""

from __future__ import annotations

from treasure_game.models import (
    Direction,
    GameState,
    Position,
    Seat,
    TrueMap,
    require_true_map,
)
from treasure_game.path_module import PathModule

__all__ = ["GreedyAI"]


def _require_dice(dice_N: object) -> int:
    """骰值必须是非负 ``int``（bool 不算）；非法值抛 ``ValueError``。"""
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


class GreedyAI:
    """EASY 模式机器对手：BFS 性价比最高的宝藏，直接冲。

    ``decide`` 完全由**真值图**驱动（AI 永远读 ``TrueMap``，见 06 §4.6），
    但输出只依赖"自身位置 + 剩余宝藏 + 底图墙"，与对手坐标无关
    （02 §1 验证点：贪心不看人类座位）。
    """

    __slots__ = ("_calculation_details",)

    def __init__(self) -> None:
        self._calculation_details: list[str] = []

    def calculation_details(self) -> tuple[str, ...]:
        return tuple(self._calculation_details)

    # ------------------------------------------------------------------
    # 目标选择
    # ------------------------------------------------------------------
    @staticmethod
    def choose_target(
        true_map: TrueMap, start: Position, dice_N: int, *, details: list[str] | None = None
    ) -> Position | None:
        """按 02 §1 的比率 + tie-break 选出目标宝藏；无可行目标返回 ``None``。

        允许 ``start`` 恰好站在宝藏上（距离 0 → 分母按 1 处理）。
        """
        current_map = require_true_map(true_map)
        if not current_map.remaining_treasures:
            return None
        distances = PathModule.bfs_distances(current_map, start)

        best: Position | None = None
        best_value = 0
        best_distance = 0
        best_ratio_left = 0  # 比率交叉相乘的左项：value * best_den
        best_ratio_right = 1  # 比率交叉相乘的右项：best_value * den
        for target in sorted(current_map.remaining_treasures, key=lambda p: (p.row, p.col)):
            distance = distances.get(target)
            if details is not None:
                label = f"第 {target.row + 1} 行第 {target.col + 1} 列"
                value = current_map.treasure_values[target]
                details.append(
                    f"宝藏 {label}：{value} 分，不可达，排除。" if distance is None else
                    f"宝藏 {label}：{value} 分，BFS 距离 {distance}，"
                    f"性价比 {value}/{max(distance, 1)} = {value / max(distance, 1):.3f}。"
                )
            if distance is None:  # 不可达资源绝不作为候选
                continue
            value = current_map.treasure_values[target]
            denominator = distance if distance > 0 else 1
            if best is None:
                best, best_value, best_distance = target, value, distance
                best_ratio_left, best_ratio_right = value, denominator
                continue
            left = value * best_ratio_right  # value / den  vs  best_value / best_den
            right = best_value * denominator
            if left > right:
                better = True
            elif left < right:
                better = False
            elif value != best_value:  # 比率相同 → 分值高者优先
                better = value > best_value
            elif distance != best_distance:  # 再同 → 距离短者优先
                better = distance < best_distance
            else:  # 最后 → (row, col) 字典序小者优先（迭代序已是字典序，显式比较以求稳定）
                better = (target.row, target.col) < (best.row, best.col)
            if better:
                best, best_value, best_distance = target, value, distance
                best_ratio_left, best_ratio_right = value, denominator
        del best_ratio_left
        if details is not None:
            details.append(
                "没有可达宝藏，本轮停留。" if best is None else
                f"选定目标：第 {best.row + 1} 行第 {best.col + 1} 列；取最短路径的本轮步数前缀。"
            )
        return best

    # ------------------------------------------------------------------
    # 决策入口（06 §3 冻结签名）
    # ------------------------------------------------------------------
    def decide(
        self, state: GameState, dice_N: int, seat: Seat = Seat.B
    ) -> tuple[Direction, ...]:
        """返回本回合的前缀移动序列（长度 <= ``dice_N``），全部由 Path 核可重放。

        * ``dice_N == 0``、无剩余宝藏、目标不可达 → 空序列；
        * 序列是 ``PathModule.bfs_path`` 的结果前缀，因此不含撞墙指令（``blocked_steps == 0``）。
        """
        current_state = _require_state(state)
        steps = _require_dice(dice_N)
        actor = _require_seat(seat)
        true_map = require_true_map(current_state.true_map)
        self._calculation_details = []
        if steps == 0 or true_map.is_finished():
            self._calculation_details.append("没有可用步数或剩余宝藏，本轮停留。")
            return ()

        start = current_state.position_of(actor)
        target = self.choose_target(true_map, start, steps, details=self._calculation_details)
        if target is None:
            return ()
        path = PathModule.bfs_path(true_map, start, target)
        if not path:  # None（不可达）或 ()（起点即目标）
            return ()
        return tuple(path[:steps])
