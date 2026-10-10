"""数据模型与不变量（冻结定义见 ``docs/06-interface-freeze.md`` §2）。

约定
----
* 所有公开值对象都是 ``frozen=True, slots=True`` 的 dataclass：任何"修改"都必须返回新对象，
  调用方持有的入参永不被改动。
* ``TrueMap`` 把**静态层**（``terrain`` / ``treasure_values`` / ``trap_values`` / ``map_id``）与
  **动态层**（``remaining_treasures`` / ``active_traps`` / ``revealed_traps``）分开：
  动态状态不参与 ``map_id``，因此同一张地图在整局游戏中指纹恒定。
* 内部集合统一规范化为 ``tuple`` / ``frozenset`` / ``types.MappingProxyType``，杜绝可变别名。
* 构造时只做**廉价**校验（形状、坐标边界、资源分值符号、资源不重叠）；完整地图合法性
  （txt 语法、连通性、出生点）属于 ``MapModule`` 的职责。
* 本模块只依赖标准库：禁止 ``numpy`` / ``pytest`` 等第三方包进入运行时代码。
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import types
import typing
from collections.abc import Mapping

__all__ = [
    # 文案常量（§5 冻结，全项目唯一来源）
    "BLOCKED_MOVE_MESSAGE",
    "TREASURE_MESSAGE_TEMPLATE",
    "TRAP_MESSAGE_TEMPLATE",
    "HIDDEN_TRAP_MESSAGE_TEMPLATE",
    "BATTLE_WIN_MESSAGE_TEMPLATE",
    "BATTLE_DRAW_MESSAGE",
    "GAME_OVER_MESSAGE",
    "MAP_ID_LENGTH",
    # 枚举与值对象
    "GameMode",
    "Direction",
    "Position",
    "TrueMap",
    "ViewMap",
    "Seat",
    "GameState",
    "GameEvent",
    "MoveResult",
    "BattleResult",
    "SearchDiagnostics",
    "GameResult",
    "PublicStateView",
    "GameIO",
    # 异常
    "MapFormatError",
    "MapGenerationError",
    "IllegalStateError",
    # 工具
    "compute_map_id",
    "require_true_map",
]

# ---------------------------------------------------------------------------
# §5 冻结文案：全项目唯一来源，其他模块不得另抄一份（防止文案漂移）。
# ---------------------------------------------------------------------------
BLOCKED_MOVE_MESSAGE = "该方向为障碍物，无法通过，不消耗步数"
TREASURE_MESSAGE_TEMPLATE = "拾取宝藏 +{value} 分"
TRAP_MESSAGE_TEMPLATE = "踩到陷阱 {value} 分"
HIDDEN_TRAP_MESSAGE_TEMPLATE = "【踩到隐藏陷阱！扣除 {value} 分，陷阱已暴露】"
BATTLE_WIN_MESSAGE_TEMPLATE = "触发对战：{winner} +{win_score} 分，{loser} -{win_score} 分"
BATTLE_DRAW_MESSAGE = "触发对战：分数相同，平局"
GAME_OVER_MESSAGE = "游戏结束：宝藏已全部拾取"

#: ``map_id`` 取 sha1 十六进制前 16 位。
MAP_ID_LENGTH = 16


# ---------------------------------------------------------------------------
# 异常（§2 冻结）
# ---------------------------------------------------------------------------
class MapFormatError(ValueError):
    """txt 地图语法/结构非法，或矩阵编码不符合约定。"""


class MapGenerationError(ValueError):
    """随机地图生成在重试上限内仍无法满足约束。"""


class IllegalStateError(ValueError):
    """对局状态或调用顺序非法（重复结算、对已游玩地图导出等）。"""


# ---------------------------------------------------------------------------
# 基础枚举
# ---------------------------------------------------------------------------
class GameMode(enum.Enum):
    """三种游戏模式（spec §一）。"""

    EASY = "EASY"
    NORMAL = "NORMAL"
    HARD = "HARD"


class Direction(enum.Enum):
    """四个正交方向；``value`` 即控制台字符。

    邻序遍历固定为 **上、左、下、右**（BFS 与败方推开都依赖该顺序，见 §3）。
    """

    UP = "w"
    LEFT = "a"
    DOWN = "s"
    RIGHT = "d"

    @property
    def delta(self) -> tuple[int, int]:
        """返回 ``(drow, dcol)``：UP=(-1,0) LEFT=(0,-1) DOWN=(1,0) RIGHT=(0,1)。"""
        if self is Direction.UP:
            return (-1, 0)
        if self is Direction.LEFT:
            return (0, -1)
        if self is Direction.DOWN:
            return (1, 0)
        return (0, 1)

    def __str__(self) -> str:
        return self.value

    def __format__(self, format_spec: str) -> str:
        return format(self.value, format_spec)

    @classmethod
    def from_char(cls, char: str) -> "Direction":
        """把 ``w/a/s/d``（大小写皆可）转成方向；非法字符抛 ``ValueError``。

        控制台 IO 负责调用；核心内核只接受 ``Direction`` 实例。
        """
        if not isinstance(char, str):
            raise ValueError(f"方向字符必须是 str，收到 {type(char).__name__}")
        text = char.strip().lower()
        for direction in cls:
            if direction.value == text:
                return direction
        raise ValueError(f"非法方向字符：{char!r}（只接受 w/a/s/d）")


class Seat(enum.Enum):
    """两个座位；人机标准映射 A=Human、B=Opt（§01 公共类型）。"""

    A = "A"
    B = "B"

    @property
    def opponent(self) -> "Seat":
        return Seat.B if self is Seat.A else Seat.A

    def __str__(self) -> str:
        return self.value

    def __format__(self, format_spec: str) -> str:
        return format(self.value, format_spec)


@dataclasses.dataclass(frozen=True, slots=True)
class Position:
    """零基 ``(row, col)`` 栅格坐标。"""

    row: int
    col: int

    def moved(self, direction: Direction) -> "Position":
        """返回沿 ``direction`` 走一格后的新坐标（不做边界检查）。"""
        if not isinstance(direction, Direction):
            raise ValueError(f"direction 必须是 Direction，收到 {type(direction).__name__}")
        drow, dcol = direction.delta
        return Position(self.row + drow, self.col + dcol)


# ---------------------------------------------------------------------------
# 地图
# ---------------------------------------------------------------------------
def _require_position(value: object, name: str = "pos") -> Position:
    if not isinstance(value, Position):
        raise ValueError(f"{name} 必须是 Position，收到 {type(value).__name__}")
    return value


def _normalize_terrain(
    terrain: typing.Iterable[typing.Iterable[int]], height: int, width: int
) -> tuple[tuple[int, ...], ...]:
    rows = tuple(terrain)
    if len(rows) != height:
        raise ValueError(f"terrain 行数 {len(rows)} 与 height {height} 不一致")
    normalized: list[tuple[int, ...]] = []
    converted = False
    for index, row in enumerate(rows):
        if type(row) is tuple:
            current = row
        else:
            current = tuple(row)
            converted = True
        if len(current) != width:
            raise ValueError(f"terrain 第 {index} 行长度 {len(current)} 与 width {width} 不一致")
        normalized.append(current)
    if converted:
        # 从"原始"底图（非规范化 tuple）构造时顺手扫描取值，挡住"把资源矩阵当底图传"的常见错误。
        # 已经是 tuple-of-tuples 时跳过扫描：PathModule 的 dataclasses.replace() 热路径
        # 必须保持 O(H + 资源数)，不能每次调用都做 O(H*W) 全扫。
        for row_index, row in enumerate(normalized):
            for col_index, cell in enumerate(row):
                if cell != 0 and cell != 1:
                    raise ValueError(
                        f"terrain[{row_index}][{col_index}]={cell!r} 非法：底图只允许 0（空地）/ 1（墙）；"
                        "宝藏与陷阱分值请放入 treasure_values / trap_values，"
                        "或用 TrueMap.from_grid(原始整数矩阵) 一步构造"
                    )
    return tuple(normalized)


def _normalize_value_layer(
    values: typing.Union[Mapping[Position, int], typing.Iterable[tuple[Position, int]]],
    *,
    height: int,
    width: int,
    is_treasure: bool,
    name: str,
) -> Mapping[Position, int]:
    items: dict[Position, int] = {}
    for key, value in dict(values).items():  # type: ignore[arg-type]
        if not isinstance(key, Position):
            raise ValueError(f"{name} 的键必须是 Position，收到 {type(key).__name__}")
        if not (0 <= key.row < height and 0 <= key.col < width):
            raise ValueError(f"{name} 坐标 {key} 越界（地图 {height}x{width}）")
        if type(value) is not int:
            raise ValueError(f"{name} 分值必须是 int，收到 {type(value).__name__}")
        if is_treasure and value < 2:
            raise ValueError(f"{name} 分值必须 >= 2（1 保留给墙），收到 {value}")
        if not is_treasure and value > -1:
            raise ValueError(f"{name} 分值必须 <= -1，收到 {value}")
        items[key] = value
    return types.MappingProxyType(items)


def _rebuild_true_map(
    height: int,
    width: int,
    terrain: tuple[tuple[int, ...], ...],
    treasure_values: Mapping[Position, int],
    trap_values: Mapping[Position, int],
    remaining_treasures: frozenset[Position],
    active_traps: frozenset[Position],
    revealed_traps: frozenset[Position],
    map_id: str,
) -> "TrueMap":
    """``TrueMap.__reduce__`` 的重建入口（模块级函数，保证 pickle 能按名解析）。"""
    return TrueMap(
        height=height,
        width=width,
        terrain=terrain,
        treasure_values=treasure_values,
        trap_values=trap_values,
        remaining_treasures=remaining_treasures,
        active_traps=active_traps,
        revealed_traps=revealed_traps,
        map_id=map_id,
    )


def compute_map_id(
    height: int,
    width: int,
    terrain: typing.Iterable[typing.Iterable[int]],
    treasure_values: Mapping[Position, int],
    trap_values: Mapping[Position, int],
) -> str:
    """静态内容指纹：``sha1`` 十六进制前 :data:`MAP_ID_LENGTH` 位。

    只覆盖**静态层**（尺寸、底图、宝藏/陷阱原始布局与分值），
    不含 ``remaining_treasures`` / ``active_traps`` / ``revealed_traps``，因此整局恒定；
    资源按坐标排序后再摘要，保证与插入顺序无关。
    """
    digest = hashlib.sha1()
    digest.update(f"{height}x{width}\n".encode("utf-8"))
    for row in terrain:
        digest.update((",".join(str(cell) for cell in row) + "\n").encode("utf-8"))
    for pos, value in sorted(treasure_values.items(), key=lambda item: (item[0].row, item[0].col)):
        digest.update(f"T{pos.row},{pos.col}:{value}\n".encode("utf-8"))
    for pos, value in sorted(trap_values.items(), key=lambda item: (item[0].row, item[0].col)):
        digest.update(f"X{pos.row},{pos.col}:{value}\n".encode("utf-8"))
    return digest.hexdigest()[:MAP_ID_LENGTH]


@dataclasses.dataclass(frozen=True, slots=True)
class TrueMap:
    """底层真实地图：静态底图 + 资源原值 + 动态剩余/已揭示集合。

    动态层与静态层分离是 ADR-04 的核心：已拾取的宝藏只从 ``remaining_treasures`` 移除，
    已触发的陷阱从 ``active_traps`` 移除并**永久**留在 ``revealed_traps``，
    因此"历史暴露"不会因为格子被清空而丢失。

    ``map_id`` 为静态内容指纹；留空（默认 ``""``）时构造期自动计算，
    手写测试地图无需自己算指纹。

    ``terrain`` 是**纯底图**，只允许 ``0``（空地）/ ``1``（墙）；资源分值必须放进
    ``treasure_values`` / ``trap_values``（从原始整数矩阵构造请用 :meth:`from_grid`）。
    构造期做廉价校验（形状、坐标边界、分值符号、两层不重叠、动态集合为静态键子集）；
    完整的 txt 语法/连通性校验属于 ``MapModule`` 的职责。
    """

    height: int
    width: int
    terrain: tuple[tuple[int, ...], ...]
    treasure_values: Mapping[Position, int]
    trap_values: Mapping[Position, int]
    remaining_treasures: frozenset[Position]
    active_traps: frozenset[Position]
    revealed_traps: frozenset[Position]
    map_id: str = ""

    def __post_init__(self) -> None:
        if type(self.height) is not int or type(self.width) is not int:
            raise ValueError("height/width 必须是 int")
        if self.height <= 0 or self.width <= 0:
            raise ValueError(f"height/width 必须为正，收到 {self.height}x{self.width}")

        terrain = _normalize_terrain(self.terrain, self.height, self.width)
        treasures = _normalize_value_layer(
            self.treasure_values, height=self.height, width=self.width, is_treasure=True,
            name="treasure_values",
        )
        traps = _normalize_value_layer(
            self.trap_values, height=self.height, width=self.width, is_treasure=False,
            name="trap_values",
        )
        overlap = set(treasures) & set(traps)
        if overlap:
            raise ValueError(f"同一格不能既是宝藏又是陷阱：{sorted(overlap, key=lambda p: (p.row, p.col))}")

        remaining = frozenset(self.remaining_treasures)
        active = frozenset(self.active_traps)
        revealed = frozenset(self.revealed_traps)
        if not remaining <= set(treasures):
            raise ValueError("remaining_treasures 必须是 treasure_values 键的子集")
        if not active <= set(traps):
            raise ValueError("active_traps 必须是 trap_values 键的子集")
        if not revealed <= set(traps):
            raise ValueError("revealed_traps 必须是 trap_values 键的子集")

        object.__setattr__(self, "terrain", terrain)
        object.__setattr__(self, "treasure_values", treasures)
        object.__setattr__(self, "trap_values", traps)
        object.__setattr__(self, "remaining_treasures", remaining)
        object.__setattr__(self, "active_traps", active)
        object.__setattr__(self, "revealed_traps", revealed)
        if not self.map_id:
            object.__setattr__(
                self, "map_id",
                compute_map_id(self.height, self.width, terrain, treasures, traps),
            )

    # -- 构造便捷入口（追加 API，不改变冻结字段） --------------------------
    @classmethod
    def create(
        cls,
        height: int,
        width: int,
        terrain: typing.Iterable[typing.Iterable[int]],
        treasures: typing.Optional[Mapping[Position, int]] = None,
        traps: typing.Optional[Mapping[Position, int]] = None,
        *,
        remaining_treasures: typing.Optional[typing.Iterable[Position]] = None,
        active_traps: typing.Optional[typing.Iterable[Position]] = None,
        revealed_traps: typing.Optional[typing.Iterable[Position]] = None,
        map_id: str = "",
    ) -> "TrueMap":
        """初始局面构造器：默认所有宝藏都"剩余"、所有陷阱都"未触发"。"""
        treasure_values = dict(treasures or {})
        trap_values = dict(traps or {})
        return cls(
            height=height,
            width=width,
            terrain=terrain,
            treasure_values=treasure_values,
            trap_values=trap_values,
            remaining_treasures=(
                frozenset(treasure_values) if remaining_treasures is None
                else frozenset(remaining_treasures)
            ),
            active_traps=(
                frozenset(trap_values) if active_traps is None else frozenset(active_traps)
            ),
            revealed_traps=frozenset() if revealed_traps is None else frozenset(revealed_traps),
            map_id=map_id,
        )

    @classmethod
    def from_grid(cls, grid: typing.Iterable[typing.Iterable[int]], *, map_id: str = "") -> "TrueMap":
        """从 txt 同构的整数矩阵构造：``0`` 空地 / ``1`` 墙 / ``>=2`` 宝藏 / ``<=-1`` 陷阱。

        与 D1 决策一致：``1`` 唯一表示墙，宝藏分值从 ``2`` 起。矩阵非矩形或含非整数时抛
        :class:`MapFormatError`（``ValueError`` 子类）。
        """
        rows: list[tuple[int, ...]] = []
        for raw_row in grid:
            row = tuple(raw_row)
            for value in row:
                if type(value) is not int:
                    raise MapFormatError(f"地图矩阵只能含 int，收到 {type(value).__name__}")
            rows.append(row)
        if not rows or not rows[0]:
            raise MapFormatError("地图矩阵不能为空")
        width = len(rows[0])
        if any(len(row) != width for row in rows):
            raise MapFormatError("地图矩阵必须是矩形")
        height = len(rows)

        terrain: list[tuple[int, ...]] = []
        treasure_values: dict[Position, int] = {}
        trap_values: dict[Position, int] = {}
        for row_index, row in enumerate(rows):
            terrain_row: list[int] = []
            for col_index, value in enumerate(row):
                pos = Position(row_index, col_index)
                if value == 0:
                    terrain_row.append(0)
                elif value == 1:
                    terrain_row.append(1)
                elif value >= 2:
                    terrain_row.append(0)
                    treasure_values[pos] = value
                else:
                    terrain_row.append(0)
                    trap_values[pos] = value
            terrain.append(tuple(terrain_row))
        return cls.create(
            height=height,
            width=width,
            terrain=tuple(terrain),
            treasures=treasure_values,
            traps=trap_values,
            map_id=map_id,
        )

    # -- 查询 --------------------------------------------------------------
    def in_bounds(self, pos: Position) -> bool:
        """坐标是否落在地图矩形内。"""
        _require_position(pos)
        return 0 <= pos.row < self.height and 0 <= pos.col < self.width

    def is_wall(self, pos: Position) -> bool:
        """是否是障碍物；越界视为不可通行，一并返回 ``True``。"""
        _require_position(pos)
        if not (0 <= pos.row < self.height and 0 <= pos.col < self.width):
            return True
        return self.terrain[pos.row][pos.col] == 1

    def is_walkable(self, pos: Position) -> bool:
        """是否可落脚（在界内且非墙）。"""
        _require_position(pos)
        if not (0 <= pos.row < self.height and 0 <= pos.col < self.width):
            return False
        return self.terrain[pos.row][pos.col] != 1

    def cell_value(self, pos: Position) -> int:
        """真值视图值：墙 ``1``、剩余宝藏 ``+v``、陷阱 ``-v``（无论是否已触发）、其他 ``0``。"""
        _require_position(pos)
        if not (0 <= pos.row < self.height and 0 <= pos.col < self.width):
            raise ValueError(f"cell_value 坐标越界：{pos}（地图 {self.height}x{self.width}）")
        if self.terrain[pos.row][pos.col] == 1:
            return 1
        if pos in self.remaining_treasures:
            return self.treasure_values[pos]
        if pos in self.active_traps:
            return self.trap_values[pos]
        if pos in self.revealed_traps:
            return self.trap_values[pos]
        return 0

    def treasure_total(self) -> int:
        """静态宝藏总分（不含动态状态）。"""
        return sum(self.treasure_values.values())

    def is_finished(self) -> bool:
        """终局条件：地图上再无剩余宝藏。"""
        return not self.remaining_treasures

    def __reduce__(self):
        """支持 ``copy.deepcopy`` / ``pickle``（``MappingProxyType`` 自身不可序列化）。

        搜索或实验需要复制/保存真值状态时可直接 ``deepcopy(state.true_map)`` 或 pickle 整局状态，
        不必自行拆层重建。
        """
        return (
            _rebuild_true_map,
            (
                self.height,
                self.width,
                self.terrain,
                dict(self.treasure_values),
                dict(self.trap_values),
                self.remaining_treasures,
                self.active_traps,
                self.revealed_traps,
                self.map_id,
            ),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ViewMap:
    """按模式脱敏后的展示地图（只给人工界面/日志，绝不回传给移动内核）。

    * EASY/NORMAL：全部格可见；
    * HARD：未触发陷阱显示 ``0``，已触发陷阱显示原始负值，``spent_traps`` 标记其不再扣分。
    """

    height: int
    width: int
    cells: tuple[tuple[int, ...], ...]
    spent_traps: frozenset[Position]
    mode: GameMode

    def __post_init__(self) -> None:
        if type(self.height) is not int or type(self.width) is not int:
            raise ValueError("height/width 必须是 int")
        if self.height <= 0 or self.width <= 0:
            raise ValueError(f"height/width 必须为正，收到 {self.height}x{self.width}")
        if not isinstance(self.mode, GameMode):
            raise ValueError(f"mode 必须是 GameMode，收到 {type(self.mode).__name__}")
        cells = tuple(tuple(row) for row in self.cells)
        if len(cells) != self.height or any(len(row) != self.width for row in cells):
            raise ValueError(
                f"cells 形状与 {self.height}x{self.width} 不一致："
                f"{len(cells)}x{len(cells[0]) if cells else 0}"
            )
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "spent_traps", frozenset(self.spent_traps))

    def in_bounds(self, row: int, col: int) -> bool:
        """给定行列是否在视图范围内。"""
        return 0 <= row < self.height and 0 <= col < self.width

    def cell_value(self, row: int, col: int) -> int:
        """读取视图格值；越界抛 ``ValueError``。"""
        if type(row) is not int or type(col) is not int:
            raise ValueError("row/col 必须是 int")
        if not (0 <= row < self.height and 0 <= col < self.width):
            raise ValueError(f"cell_value 越界：({row}, {col})（视图 {self.height}x{self.width}）")
        return self.cells[row][col]


def clone_true_map(
    true_map: TrueMap,
    *,
    remaining_treasures: frozenset[Position] | None = None,
    active_traps: frozenset[Position] | None = None,
    revealed_traps: frozenset[Position] | None = None,
) -> TrueMap:
    """快速克隆真值图，只替换动态集合（供移动内核/搜索内部使用）。

    与 ``dataclasses.replace`` 的区别：**跳过构造期校验与规范化**，因为调用方传入的
    ``true_map`` 已经是校验过的 ``TrueMap``，三个动态集合按构造只可能是其静态键的子集。
    这是移动内核热路径（搜索中会被调用上万次）的性能关键：全量 ``__post_init__``
    会让每次调用都做 ``O(H + 资源数)`` 的重复校验与集合哈希。

    普通业务代码请用公开构造器（``TrueMap(...)`` / :meth:`TrueMap.create` /
    :meth:`TrueMap.from_grid`），不要用本函数绕开校验。
    """
    clone = object.__new__(TrueMap)
    object.__setattr__(clone, "height", true_map.height)
    object.__setattr__(clone, "width", true_map.width)
    object.__setattr__(clone, "terrain", true_map.terrain)
    object.__setattr__(clone, "treasure_values", true_map.treasure_values)
    object.__setattr__(clone, "trap_values", true_map.trap_values)
    object.__setattr__(
        clone, "remaining_treasures",
        true_map.remaining_treasures if remaining_treasures is None else remaining_treasures,
    )
    object.__setattr__(
        clone, "active_traps",
        true_map.active_traps if active_traps is None else active_traps,
    )
    object.__setattr__(
        clone, "revealed_traps",
        true_map.revealed_traps if revealed_traps is None else revealed_traps,
    )
    object.__setattr__(clone, "map_id", true_map.map_id)
    return clone


def require_true_map(value: object) -> TrueMap:
    """入口守卫：把 ``ViewMap``（或其他类型）冒充 ``TrueMap`` 时抛 ``TypeError``。

    ``PathModule`` / ``AiModule`` / ``BattleModule`` / ``Simulator`` 都应先过这道闸，
    保证"视图-真值隔离"（§4.6）在运行时也成立。
    """
    if isinstance(value, ViewMap):
        raise TypeError("该接口只接受 TrueMap；ViewMap 是人工展示对象，禁止用于规则/搜索")
    if not isinstance(value, TrueMap):
        raise TypeError(f"true_map 必须是 TrueMap，收到 {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# 对局状态与事件
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True, slots=True)
class GameState:
    """一局对局的完整真值状态（只允许机器/规则层持有）。"""

    true_map: TrueMap
    pos_a: Position
    pos_b: Position
    score_a: int
    score_b: int
    turn: Seat
    mode: GameMode
    turn_index: int = 0

    def position_of(self, seat: Seat) -> Position:
        """追加便捷入口：按座位取坐标。"""
        if seat is Seat.A:
            return self.pos_a
        if seat is Seat.B:
            return self.pos_b
        raise ValueError(f"seat 必须是 Seat，收到 {seat!r}")

    def score_of(self, seat: Seat) -> int:
        """追加便捷入口：按座位取分数。"""
        if seat is Seat.A:
            return self.score_a
        if seat is Seat.B:
            return self.score_b
        raise ValueError(f"seat 必须是 Seat，收到 {seat!r}")


@dataclasses.dataclass(frozen=True, slots=True)
class GameEvent:
    """一条有序、只读的对局事件；``kind`` 取值见 §2。

    工厂方法集中生产 §5 的冻结文案，避免各模块各写一份导致文案漂移。
    """

    kind: str
    actor: Seat | None
    position: Position | None
    score_delta: int
    message: str

    #: 事件类型常量（``kind`` 的合法取值）
    MOVE = "MOVE"
    BLOCKED = "BLOCKED"
    TREASURE = "TREASURE"
    TRAP = "TRAP"
    BATTLE = "BATTLE"
    GAME_OVER = "GAME_OVER"
    INFO = "INFO"

    @staticmethod
    def move(actor: Seat | None, position: Position) -> "GameEvent":
        """成功踏入一格的 MOVE 事件（不结算分数）。"""
        return GameEvent(
            kind=GameEvent.MOVE,
            actor=actor,
            position=position,
            score_delta=0,
            message=f"移动到 ({position.row}, {position.col})",
        )

    @staticmethod
    def blocked(actor: Seat | None, position: Position) -> "GameEvent":
        """撞墙/越界的 BLOCKED 事件；``position`` 为**未发生移动的当前坐标**（始终在界内）。"""
        return GameEvent(
            kind=GameEvent.BLOCKED,
            actor=actor,
            position=position,
            score_delta=0,
            message=BLOCKED_MOVE_MESSAGE,
        )

    @staticmethod
    def treasure(actor: Seat | None, position: Position, value: int) -> "GameEvent":
        """拾取宝藏事件（``value`` 为原始正值）。"""
        return GameEvent(
            kind=GameEvent.TREASURE,
            actor=actor,
            position=position,
            score_delta=value,
            message=TREASURE_MESSAGE_TEMPLATE.format(value=value),
        )

    @staticmethod
    def trap(actor: Seat | None, position: Position, value: int, *, hidden: bool = False) -> "GameEvent":
        """踩中陷阱事件。

        ``value`` 为原始负值（如 ``-2``）。``hidden=True`` 表示困难模式下首次踩中隐藏陷阱，
        文案切换为冻结的"【踩到隐藏陷阱！扣除 X 分，陷阱已暴露】"（X 为绝对值）；
        仅由知道模式的调用方（Simulator/IO）使用——纯移动内核不感知模式。
        """
        if hidden:
            message = HIDDEN_TRAP_MESSAGE_TEMPLATE.format(value=abs(value))
        else:
            message = TRAP_MESSAGE_TEMPLATE.format(value=value)
        return GameEvent(
            kind=GameEvent.TRAP,
            actor=actor,
            position=position,
            score_delta=value,
            message=message,
        )

    @staticmethod
    def battle(
        winner: Seat,
        loser: Seat,
        win_score: int = 5,
        actor: Seat | None = None,
    ) -> "GameEvent":
        """对战结算事件（分数不等）。"""
        return GameEvent(
            kind=GameEvent.BATTLE,
            actor=actor,
            position=None,
            score_delta=0,
            message=BATTLE_WIN_MESSAGE_TEMPLATE.format(
                winner=winner, loser=loser, win_score=win_score
            ),
        )

    @staticmethod
    def battle_draw(actor: Seat | None = None) -> "GameEvent":
        """对战平局事件。"""
        return GameEvent(
            kind=GameEvent.BATTLE, actor=actor, position=None, score_delta=0,
            message=BATTLE_DRAW_MESSAGE,
        )

    @staticmethod
    def game_over(position: Position | None = None, actor: Seat | None = None) -> "GameEvent":
        """终局事件。"""
        return GameEvent(
            kind=GameEvent.GAME_OVER, actor=actor, position=position, score_delta=0,
            message=GAME_OVER_MESSAGE,
        )

    @staticmethod
    def info(
        message: str,
        actor: Seat | None = None,
        position: Position | None = None,
        score_delta: int = 0,
    ) -> "GameEvent":
        """自由文本 INFO 事件（不得用于替代上述冻结文案）。"""
        return GameEvent(
            kind=GameEvent.INFO, actor=actor, position=position, score_delta=score_delta,
            message=message,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class MoveResult:
    """一次移动阶段的增量结果；**不**直接修改 ``GameState`` 的分数或对手位置。"""

    true_map: TrueMap
    final_pos: Position
    score_delta: int
    valid_steps: int
    attempted_steps: int
    events: tuple[GameEvent, ...]
    blocked_steps: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))


@dataclasses.dataclass(frozen=True, slots=True)
class BattleResult:
    """一次回合终点对战的结算结果；地图不变。"""

    triggered: bool
    pos_a: Position
    pos_b: Position
    score_a: int
    score_b: int
    events: tuple[GameEvent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))


@dataclasses.dataclass(frozen=True, slots=True)
class SearchDiagnostics:
    """单次 AI 决策的可观察证据（实验与"预测=重放"一致性断言依赖它）。"""

    algorithm: str
    nodes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    elapsed_ns: int = 0
    root_move_seq: tuple[Direction, ...] = ()
    root_final_pos: Position | None = None
    root_score_delta: int = 0
    approximate: bool = False
    depth: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_move_seq", tuple(self.root_move_seq))


@dataclasses.dataclass(frozen=True, slots=True)
class GameResult:
    """一整局的最终结果。"""

    mode: GameMode
    final_state: GameState
    winner: str | None
    score_a: int
    score_b: int
    turns: int
    events: tuple[GameEvent, ...]
    draw_timeout: bool = False
    metrics: Mapping[str, object] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        # 保持普通 dict（JSON 可序列化，便于 W4 导出 CSV/JSON）；只做防御性浅拷贝。
        object.__setattr__(self, "metrics", dict(self.metrics))


@dataclasses.dataclass(frozen=True, slots=True)
class PublicStateView:
    """人工界面唯一可见信息：绝不含 ``TrueMap``，也不含未揭示陷阱坐标。"""

    mode: GameMode
    pos_a: Position
    pos_b: Position
    score_a: int
    score_b: int
    turn: Seat
    turn_index: int
    dice_N: int
    remaining_treasure_count: int


@typing.runtime_checkable
class GameIO(typing.Protocol):
    """表现层适配器协议；``Simulator`` 只认这个协议（ADR-09）。"""

    def render(
        self,
        view: ViewMap,
        state_view: PublicStateView,
        events: tuple[GameEvent, ...],
    ) -> None:
        ...

    def next_action(
        self,
        seat: Seat,
        dice_N: int,
        view: ViewMap,
        state_view: PublicStateView,
    ) -> tuple[Direction, ...]:
        ...

    def log(self, message: str) -> None:
        ...
