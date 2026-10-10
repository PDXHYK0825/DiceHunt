"""对战结算模块（BattleModule）：回合移动**全部结束之后**的终点对战（06 §3/§4.8）。

规则（spec §二「对战结算规则」）：

1. 仅由 ``Simulator`` 在每个回合移动阶段结束后、且双方坐标重合时调用一次；
   中途路过对手不结算。
2. ``pos_a != pos_b`` → ``triggered=False``，位置与分数**完全不变**。
3. 重合且同分 → 平局：``triggered=True``，分数、位置均不变，不推开。
4. 重合且分数不等 → **先按原始分数比较**：胜者 ``+win_score``、败者 ``-win_score``；
   败方再用 ``PathModule.get_adjacent_empty`` 推到相邻合法空地（上/左/下/右第一个
   "底图 0 且从未布置资源"的格子），无候选则原地不动但分数照改。

本模块不消耗宝藏/陷阱、不递归触发第二次对战、不修改 ``TrueMap``。
"""

from __future__ import annotations

from treasure_game.models import (
    BattleResult,
    GameEvent,
    Position,
    Seat,
    TrueMap,
    require_true_map,
)
from treasure_game.path_module import PathModule

__all__ = ["BattleModule"]


def _require_position(true_map: TrueMap, value: object, name: str) -> Position:
    """对战坐标必须落在界内的可落脚格子上（墙/越界抛 ``ValueError``）。"""
    if not isinstance(value, Position):
        raise ValueError(f"{name} 必须是 Position，收到 {type(value).__name__}")
    if not true_map.in_bounds(value):
        raise ValueError(f"{name} 越界：{value}（地图 {true_map.height}x{true_map.width}）")
    if true_map.is_wall(value):
        raise ValueError(f"{name} 不能是障碍物：{value}")
    return value


def _require_score(value: object, name: str) -> int:
    """分数允许任意整数（含负），但必须是 ``int``。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是 int，收到 {type(value).__name__}")
    return value


def _require_seat(value: object, name: str) -> Seat:
    if not isinstance(value, Seat):
        raise ValueError(f"{name} 必须是 Seat，收到 {type(value).__name__}")
    return value


class BattleModule:
    """终点对战结算（无实例状态，唯一公开入口为 ``@staticmethod trigger_battle``）。"""

    @staticmethod
    def trigger_battle(
        true_map: TrueMap,
        pos_a: Position,
        pos_b: Position,
        score_a: int,
        score_b: int,
        actor_a: Seat = Seat.A,
        actor_b: Seat = Seat.B,
        win_score: int = 5,
    ) -> BattleResult:
        """结算一次终点对战，返回 :class:`BattleResult`（地图不变，分数/坐标见返回对象）。

        事件：分数不等时产生**恰好一条** ``BATTLE`` 事件（``models.GameEvent.battle`` 工厂，
        冻结文案 ``触发对战：{胜者} +5 分，{败者} -5 分``）；平局时为
        ``触发对战：分数相同，平局``。未触发对战时不产生事件。
        """
        current_map = require_true_map(true_map)
        a_pos = _require_position(current_map, pos_a, "pos_a")
        b_pos = _require_position(current_map, pos_b, "pos_b")
        a_score = _require_score(score_a, "score_a")
        b_score = _require_score(score_b, "score_b")
        seat_a = _require_seat(actor_a, "actor_a")
        seat_b = _require_seat(actor_b, "actor_b")
        if isinstance(win_score, bool) or not isinstance(win_score, int) or win_score < 0:
            raise ValueError(f"win_score 必须是非负整数，收到 {win_score!r}")

        # 1) 未重合：不结算，位置与分数逐字不变。
        if a_pos != b_pos:
            return BattleResult(
                triggered=False,
                pos_a=a_pos,
                pos_b=b_pos,
                score_a=a_score,
                score_b=b_score,
                events=(),
            )

        # 2) 重合且同分：平局，不推开、不改分。
        if a_score == b_score:
            return BattleResult(
                triggered=True,
                pos_a=a_pos,
                pos_b=b_pos,
                score_a=a_score,
                score_b=b_score,
                events=(GameEvent.battle_draw(),),
            )

        # 3) 重合且分数不等：先按原始分数比较，再改分（避免改分后比较漂移）。
        if a_score > b_score:
            winner_seat, loser_seat = seat_a, seat_b
            new_score_a, new_score_b = a_score + win_score, b_score - win_score
            loser_is_b = True
        else:
            winner_seat, loser_seat = seat_b, seat_a
            new_score_a, new_score_b = a_score - win_score, b_score + win_score
            loser_is_b = False

        # 败方推开：只看静态从未布置资源的相邻空地；无候选则原地不动（spec 用例 5）。
        push_target = PathModule.get_adjacent_empty(current_map, a_pos)
        new_pos_a, new_pos_b = a_pos, b_pos
        if push_target is not None:
            if loser_is_b:
                new_pos_b = push_target
            else:
                new_pos_a = push_target

        event = GameEvent.battle(winner=winner_seat, loser=loser_seat, win_score=win_score)
        return BattleResult(
            triggered=True,
            pos_a=new_pos_a,
            pos_b=new_pos_b,
            score_a=new_score_a,
            score_b=new_score_b,
            events=(event,),
        )
