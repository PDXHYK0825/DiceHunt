"""地图模块（MapModule）：txt 地图读写、参数化随机生成、三模式视图生成。

冻结契约见 ``Project/docs/06-interface-freeze.md`` §2/§3/§4：

* ``MapModule.generate`` 是 ``@staticmethod``，只用调用方注入的 ``rng``（ADR-08）；
* ``get_view_map`` 的 EASY/NORMAL 全量可见，HARD 只把**未触发**陷阱伪装成空地；
* ``load_txt`` / ``save_txt`` 使用 D3 文本格式，只导出**初始布局**。

本模块不执行移动、不判定对战、不持有对局动态状态。
"""

from __future__ import annotations

import pathlib
import random
import re
from collections.abc import Iterable, Mapping

from treasure_game.models import (
    GameMode,
    IllegalStateError,
    MapFormatError,
    MapGenerationError,
    Position,
    TrueMap,
    ViewMap,
    require_true_map,
)
from treasure_game.path_module import PathModule

__all__ = ["MapModule"]

WALL: int = 1
EMPTY: int = 0

# 总权重 1000：1–10 分各 9.9%，15/20 分各 0.5%。
TREASURE_VALUES: tuple[int, ...] = (*range(1, 11), 15, 20)
TREASURE_WEIGHTS: tuple[int, ...] = (99,) * 10 + (5, 5)

_INT_RE = re.compile(r"^[+-]?\d+$")


# --------------------------------------------------------------------------- #
# 构造与取值辅助
# --------------------------------------------------------------------------- #


def _value_map(layer: object) -> dict[Position, int]:
    """把静态资源层规范成 ``dict[Position, int]``。

    兼容 models.py 的两种等价表示：``Mapping[Position, int]``（06 §2 冻结）
    与 ``tuple[tuple[Position, int], ...]``（00 数据模型表）。
    """
    return {pos: int(value) for pos, value in dict(layer).items()}  # type: ignore[arg-type]


def _build_true_map(
    height: int,
    width: int,
    terrain: Iterable[Iterable[int]],
    treasures: Mapping[Position, int],
    traps: Mapping[Position, int],
) -> TrueMap:
    """构造一张**全新初始** ``TrueMap``：所有宝藏剩余、所有陷阱未触发、``map_id`` 交给 models 计算。"""
    return TrueMap(
        height=int(height),
        width=int(width),
        terrain=tuple(tuple(int(value) for value in row) for row in terrain),
        treasure_values=dict(treasures),
        trap_values=dict(traps),
        remaining_treasures=frozenset(treasures),
        active_traps=frozenset(traps),
        revealed_traps=frozenset(),
    )


def _validate_true_map_shape(true_map: TrueMap) -> None:
    """最小结构校验：矩形底图、只含 0/1、资源不越界且不落墙。"""
    height, width = true_map.height, true_map.width
    if (
        isinstance(height, bool)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or not isinstance(width, int)
    ):
        raise ValueError(f"地图尺寸必须为整数：height={height!r}, width={width!r}")
    if height <= 0 or width <= 0:
        raise ValueError(f"地图尺寸必须为正：height={height}, width={width}")
    terrain = true_map.terrain
    if len(terrain) != height or any(len(row) != width for row in terrain):
        raise ValueError("地图底图必须是 height×width 的矩形")
    for row in terrain:
        for value in row:
            if value not in (EMPTY, WALL):
                raise ValueError(f"底图只允许 0（空地）/1（墙），收到 {value!r}")
    for pos in list(_value_map(true_map.treasure_values)) + list(_value_map(true_map.trap_values)):
        if not (0 <= pos.row < height and 0 <= pos.col < width):
            raise ValueError(f"资源坐标越界：{pos}")
        if terrain[pos.row][pos.col] == WALL:
            raise ValueError(f"资源落在墙体上：{pos}")


def _check_birth_points(true_map: TrueMap, pos_a: Position, pos_b: Position, source: str = "") -> None:
    """出生点必须在界内、是空地（非墙且不占资源）且互不重合（D3）。"""
    treasures = _value_map(true_map.treasure_values)
    traps = _value_map(true_map.trap_values)
    where = f"（{source}）" if source else ""
    for label, pos in (("A", pos_a), ("B", pos_b)):
        if not isinstance(pos, Position):
            raise MapFormatError(f"出生点 {label} 必须是 Position，收到 {type(pos).__name__}{where}")
        if not (0 <= pos.row < true_map.height and 0 <= pos.col < true_map.width):
            raise MapFormatError(f"出生点 {label} 越界：{pos}{where}")
        if true_map.terrain[pos.row][pos.col] == WALL:
            raise MapFormatError(f"出生点 {label} 位于墙体：{pos}{where}")
        if pos in treasures or pos in traps:
            raise MapFormatError(f"出生点 {label} 与资源格重叠：{pos}{where}")
    if pos_a == pos_b:
        raise MapFormatError(f"两个出生点重合：{pos_a}{where}")


def _check_connectivity(true_map: TrueMap, pos_a: Position, source: str = "") -> None:
    """所有非墙格必须与出生点 A 连通（BFS 复用 ``PathModule.bfs_distances``，D9）。"""
    total = true_map.height * true_map.width
    walls = sum(1 for row in true_map.terrain for value in row if value == WALL)
    reachable = PathModule.bfs_distances(true_map, pos_a)
    if len(reachable) != total - walls:
        where = f"（{source}）" if source else ""
        raise MapFormatError(
            f"地图不连通：{total - walls} 个非墙格中只有 {len(reachable)} 个可从出生点 A 到达{where}"
        )


def _parse_int_tokens(text: str, expected: int, what: str) -> list[int]:
    parts = text.split()
    if len(parts) != expected:
        raise MapFormatError(f"{what} 需要 {expected} 个整数，实际 {len(parts)} 个：{text!r}")
    values: list[int] = []
    for token in parts:
        if not _INT_RE.match(token):
            raise MapFormatError(f"{what} 含非整数内容：{token!r}")
        values.append(int(token))
    return values


def _parse_birth_point(text: str, label: str) -> Position:
    """解析 ``A row col`` / ``row col`` 两种合法写法（D3 冻结格式带标签）。"""
    parts = text.split()
    if len(parts) == 3:
        if parts[0].upper() != label:
            raise MapFormatError(f"出生点行标签应为 {label!r}，实际 {parts[0]!r}：{text!r}")
        numbers = parts[1:]
    elif len(parts) == 2:
        numbers = parts
    else:
        raise MapFormatError(f"出生点 {label} 行需要 2—3 个字段（{label} row col），实际：{text!r}")
    for token in numbers:
        if not _INT_RE.match(token):
            raise MapFormatError(f"出生点 {label} 含非整数坐标：{token!r}")
    return Position(int(numbers[0]), int(numbers[1]))


# --------------------------------------------------------------------------- #
# MapModule
# --------------------------------------------------------------------------- #


class MapModule:
    """真值地图的解析/生成/导出与按模式脱敏视图（不执行移动与对战）。"""

    __slots__ = ("_true_map",)

    def __init__(self, true_map: TrueMap) -> None:
        self._true_map = require_true_map(true_map)
        _validate_true_map_shape(self._true_map)

    @property
    def true_map(self) -> TrueMap:
        return self._true_map

    # ------------------------------------------------------------------ 视图

    def get_view_map(self, mode: GameMode) -> ViewMap:
        """按模式生成人工可见视图（06 §4.6，视图-真值隔离）。

        EASY/NORMAL：完整信息（墙 1、剩余宝藏 +v、未触发陷阱 -v、已触发陷阱 -v、空地 0）。
        HARD：**未触发**陷阱显示 0（伪装空地）；已触发陷阱显示其原始负值，
        并在 ``spent_traps`` 中标记（再踩不扣分，D2）。

        返回全新的 frozen ``ViewMap``：``cells`` 是新建的 ``tuple[tuple[int, ...], ...]``，
        不暴露 ``TrueMap`` 的任何内部引用。
        """
        if not isinstance(mode, GameMode):
            raise ValueError(f"非法游戏模式：{mode!r}")
        true_map = self._true_map
        treasures = _value_map(true_map.treasure_values)
        traps = _value_map(true_map.trap_values)
        remaining = frozenset(true_map.remaining_treasures)
        active = frozenset(true_map.active_traps)
        revealed = frozenset(true_map.revealed_traps)
        hide_traps = mode is GameMode.HARD

        cells: list[tuple[int, ...]] = []
        spent: set[Position] = set()
        for row in range(true_map.height):
            view_row: list[int] = []
            for col in range(true_map.width):
                pos = Position(row, col)
                if true_map.terrain[row][col] == WALL:
                    view_row.append(WALL)
                elif pos in remaining:
                    view_row.append(treasures.get(pos, EMPTY))
                elif pos in revealed:
                    view_row.append(traps.get(pos, EMPTY))
                    spent.add(pos)
                elif pos in active:
                    view_row.append(EMPTY if hide_traps else traps.get(pos, EMPTY))
                else:
                    view_row.append(EMPTY)
            cells.append(tuple(view_row))
        return ViewMap(
            height=true_map.height,
            width=true_map.width,
            cells=tuple(cells),
            spent_traps=frozenset(spent),
            mode=mode,
            treasure_cells=remaining,
        )

    # -------------------------------------------------------------- txt 读取

    @staticmethod
    def load_txt(path: pathlib.Path) -> tuple[TrueMap, Position, Position]:
        """读取 D3 文本地图。

        格式：首行 ``height width``；随后 ``height`` 行、每行 ``width`` 个整数；
        再两行 ``A row col`` 与 ``B row col``。
        单格语义：``0`` 空地、``1`` 墙、``+1`` 为 1 分宝藏、``>=2`` 宝藏分值、``<0`` 陷阱扣分。

        语法/维度/出生点/连通非法抛 :class:`MapFormatError`；文件不存在等 I/O 错误透传 ``OSError``。
        """
        path = pathlib.Path(path)
        raw = path.read_text(encoding="utf-8-sig")  # OSError 直接透传
        lines = [line.strip() for line in raw.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            raise MapFormatError(f"地图文件为空：{path}")

        height, width = _parse_int_tokens(lines[0], 2, "首行 height width")
        if height <= 0 or width <= 0:
            raise MapFormatError(f"地图尺寸必须为正：height={height}, width={width}（{path}）")
        expected_lines = height + 3  # 首行 + height 行矩阵 + A 行 + B 行
        if len(lines) != expected_lines:
            raise MapFormatError(
                f"地图行数不符：期望 {expected_lines} 行（首行 + {height} 行矩阵 + 2 行出生点），"
                f"实际 {len(lines)} 行（{path}）"
            )

        grid = [_parse_int_tokens(lines[1 + row], width, f"第 {row + 1} 行") for row in range(height)]
        one_point_treasures = frozenset(
            Position(row, col)
            for row in range(height)
            for col, token in enumerate(lines[1 + row].split())
            if token.startswith("+") and grid[row][col] == 1
        )
        pos_a = _parse_birth_point(lines[height + 1], "A")
        pos_b = _parse_birth_point(lines[height + 2], "B")
        return MapModule._assemble(
            height, width, grid, pos_a, pos_b, source=str(path),
            one_point_treasures=one_point_treasures,
        )

    @staticmethod
    def _assemble(
        height: int,
        width: int,
        grid: list[list[int]],
        pos_a: Position,
        pos_b: Position,
        source: str = "",
        one_point_treasures: frozenset[Position] = frozenset(),
    ) -> tuple[TrueMap, Position, Position]:
        terrain: list[tuple[int, ...]] = []
        treasures: dict[Position, int] = {}
        traps: dict[Position, int] = {}
        for row in range(height):
            terrain_row: list[int] = []
            for col in range(width):
                value = grid[row][col]
                pos = Position(row, col)
                if pos in one_point_treasures:  # +1：宝藏，区别于墙 1
                    terrain_row.append(EMPTY)
                    treasures[pos] = 1
                elif value == WALL:  # 1：墙
                    terrain_row.append(WALL)
                elif value > WALL:  # >= 2：宝藏分值
                    terrain_row.append(EMPTY)
                    treasures[pos] = value
                elif value < EMPTY:  # < 0：陷阱扣分
                    terrain_row.append(EMPTY)
                    traps[pos] = value
                else:  # 0：空地
                    terrain_row.append(EMPTY)
            terrain.append(tuple(terrain_row))

        true_map = _build_true_map(height, width, terrain, treasures, traps)
        _check_birth_points(true_map, pos_a, pos_b, source)
        _check_connectivity(true_map, pos_a, source)
        return true_map, pos_a, pos_b

    # -------------------------------------------------------------- txt 写出

    @staticmethod
    def save_txt(path: pathlib.Path, true_map: TrueMap, pos_a: Position, pos_b: Position) -> None:
        """把**初始布局**写成 D3 文本；已被游玩（有宝藏被拾取或陷阱被触发）的地图抛 ``IllegalStateError``。"""
        current_map = require_true_map(true_map)
        if not isinstance(pos_a, Position) or not isinstance(pos_b, Position):
            raise TypeError("save_txt 的出生点必须是 Position")
        _validate_true_map_shape(current_map)

        treasures = _value_map(current_map.treasure_values)
        traps = _value_map(current_map.trap_values)
        remaining = frozenset(current_map.remaining_treasures)
        active = frozenset(current_map.active_traps)
        revealed = frozenset(current_map.revealed_traps)
        if not remaining <= set(treasures) or not (active | revealed) <= set(traps):
            raise MapFormatError("地图动态状态与静态资源层不一致，无法导出")

        if remaining != frozenset(treasures) or active != frozenset(traps) or revealed:
            raise IllegalStateError(
                "地图已被游玩（宝藏已拾取或陷阱已触发），只能导出初始布局："
                f"remaining={len(remaining)}/{len(treasures)}, "
                f"active_traps={len(active)}/{len(traps)}, revealed_traps={len(revealed)}"
            )

        _check_birth_points(current_map, pos_a, pos_b, str(path))

        lines = [f"{current_map.height} {current_map.width}"]
        for row in range(current_map.height):
            values: list[int | str] = []
            for col in range(current_map.width):
                pos = Position(row, col)
                if current_map.terrain[row][col] == WALL:
                    values.append(WALL)
                elif pos in treasures:
                    values.append("+1" if treasures[pos] == 1 else treasures[pos])
                elif pos in traps:
                    values.append(traps[pos])
                else:
                    values.append(EMPTY)
            lines.append(" ".join(str(value) for value in values))
        lines.append(f"A {pos_a.row} {pos_a.col}")
        lines.append(f"B {pos_b.row} {pos_b.col}")

        path = pathlib.Path(path)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")

    # ---------------------------------------------------------------- 随机生成

    @staticmethod
    def draw_treasure_value(rng: random.Random) -> int:
        """使用注入的 RNG 抽取宝藏分值：1–10 分各 9.9%，15/20 分各 0.5%。"""
        return rng.choices(TREASURE_VALUES, weights=TREASURE_WEIGHTS, k=1)[0]

    @staticmethod
    def generate(
        height: int,
        width: int,
        walls: int,
        treasures: int,
        traps: int,
        rng: random.Random,
        treasure_value: int | None = None,
        trap_value: int = -2,
        max_attempts: int = 1000,
    ) -> tuple[TrueMap, Position, Position]:
        """参数化随机生成：A 固定左上、B 固定右下，其余坐标随机分配墙/宝/陷阱。

        参数非法（数量为负、容量不足即 ``height*width < 2 + walls + treasures + traps``、
        分值不合规、``max_attempts <= 0``）抛 ``ValueError``；
        重试 ``max_attempts`` 次仍不满足"非墙格与 A 全连通且出生点不重合"抛 :class:`MapGenerationError`。
        只用传入的 ``rng``，同 seed 同参数结果完全可复现（含两个出生点）。
        默认每枚宝藏独立按概率抽取分值；显式传入 ``treasure_value`` 时使用固定分值。
        """
        MapModule._validate_generate_args(
            height, width, walls, treasures, traps, rng, treasure_value, trap_value, max_attempts
        )
        for _ in range(max_attempts):
            candidate = MapModule._try_layout(
                height, width, walls, treasures, traps, rng, treasure_value, trap_value
            )
            if candidate is not None:
                return candidate
        raise MapGenerationError(
            "随机地图生成失败："
            f"{max_attempts} 次尝试后仍无法满足“非墙格与出生点 A 全连通且出生点不重合”"
            f"（height={height}, width={width}, walls={walls}, treasures={treasures}, traps={traps}）"
        )

    @staticmethod
    def _validate_generate_args(
        height: int,
        width: int,
        walls: int,
        treasures: int,
        traps: int,
        rng: random.Random,
        treasure_value: int | None,
        trap_value: int,
        max_attempts: int,
    ) -> None:
        for name, value in (
            ("height", height),
            ("width", width),
            ("walls", walls),
            ("treasures", treasures),
            ("traps", traps),
            ("treasure_value", treasure_value),
            ("trap_value", trap_value),
            ("max_attempts", max_attempts),
        ):
            if name == "treasure_value" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须为整数，收到 {value!r}")
        if height <= 0 or width <= 0:
            raise ValueError(f"地图宽高必须为正：height={height}, width={width}")
        if walls < 0 or treasures < 0 or traps < 0:
            raise ValueError(
                f"墙/宝藏/陷阱数量不能为负：walls={walls}, treasures={treasures}, traps={traps}"
            )
        needed = 2 + walls + treasures + traps
        if height * width < needed:
            raise ValueError(
                f"地图容量不足：{height}×{width}={height * width} 格 < 2 个出生点 + {walls} 墙 + "
                f"{treasures} 宝藏 + {traps} 陷阱 = {needed} 格"
            )
        if treasure_value is not None and treasure_value < 1:
            raise ValueError(f"宝藏分值必须 >= 1，收到 {treasure_value}")
        if trap_value > -1:
            raise ValueError(f"陷阱扣分必须 <= -1，收到 {trap_value}")
        if max_attempts <= 0:
            raise ValueError(f"max_attempts 必须为正，收到 {max_attempts}")
        if not isinstance(rng, random.Random):
            raise TypeError(f"rng 必须是 random.Random 实例，收到 {type(rng).__name__}")

    @staticmethod
    def _try_layout(
        height: int,
        width: int,
        walls: int,
        treasures: int,
        traps: int,
        rng: random.Random,
        treasure_value: int | None,
        trap_value: int,
    ) -> tuple[TrueMap, Position, Position] | None:
        """单次随机尝试；非墙格与 A 不全连通时返回 ``None`` 触发重试。"""
        pos_a = Position(0, 0)
        pos_b = Position(height - 1, width - 1)
        cells = [
            Position(row, col)
            for row in range(height)
            for col in range(width)
            if Position(row, col) not in (pos_a, pos_b)
        ]
        rng.shuffle(cells)

        # 两个角落预留为空地，其余坐标按 墙 → 宝 → 陷阱 → 普通空地 切分。
        wall_cells = cells[:walls]
        treasure_cells = cells[walls : walls + treasures]
        trap_cells = cells[walls + treasures : walls + treasures + traps]

        wall_set = set(wall_cells)
        terrain = tuple(
            tuple(WALL if Position(row, col) in wall_set else EMPTY for col in range(width))
            for row in range(height)
        )
        true_map = _build_true_map(
            height,
            width,
            terrain,
            {
                pos: MapModule.draw_treasure_value(rng) if treasure_value is None else treasure_value
                for pos in treasure_cells
            },
            {pos: trap_value for pos in trap_cells},
        )
        reachable = PathModule.bfs_distances(true_map, pos_a)
        if len(reachable) != height * width - walls:
            return None
        return true_map, pos_a, pos_b
