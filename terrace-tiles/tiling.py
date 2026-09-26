# -*- coding: utf-8 -*-
"""
Движок раскладки плитки по произвольному контуру террасы.

Единицы: миллиметры. Система координат: ось X — вдоль террасы (вдоль фасада),
ось Y — поперёк. Линия стыка крытой и открытой части — y = junction_y.
Раскладка ведётся от стыка к краям: на линии стыка лежит ШОВ (шов симметричен
относительно линии), плитки уходят от неё в обе стороны.

Паттерны:
  grid        — прямая раскладка (шов в шов), angle 0 или 45
  bond        — вразбежку, offset 0.5 (на половину) или 1/3 (на треть, лесенкой)
  herringbone — ёлочка (для прямоугольной плитки с целым отношением сторон), angle 0 или 45
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from shapely.geometry import Polygon, MultiPolygon, box
from shapely.affinity import rotate
from shapely.ops import unary_union
from shapely.prepared import prep

KERF = 3.0          # ширина реза диска, мм — учитывается при пересчёте обрезков
FULL_RATIO = 0.999  # доля площади, начиная с которой плитка считается целой


@dataclass
class Tile:
    name: str
    w: float  # размер вдоль X в системе паттерна (для ёлочки — длинная сторона)
    h: float  # размер вдоль Y

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def short(self) -> float:
        return min(self.w, self.h)


@dataclass
class Piece:
    poly: Polygon
    kind: str            # 'full' | 'cut'
    zone: str
    orient: str          # 'H' | 'V' (ёлочка) или '' (сетка)
    local_dims: tuple    # (ширина, высота) куска в системе паттерна, мм
    ratio: float         # доля площади плитки
    nverts: int = 4


@dataclass
class LayoutResult:
    variant: dict
    tile: Tile
    pieces: list
    terrace: Polygon
    zones: dict
    origin: tuple
    angle: float
    stats: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# генераторы решёток (в локальной, не повёрнутой системе паттерна)
# ----------------------------------------------------------------------------

def _grid(tile: Tile, joint: float, bbox, origin, offset_frac=0.0, period=1):
    mw, mh = tile.w + joint, tile.h + joint
    ox, oy = origin
    minx, miny, maxx, maxy = bbox
    j0 = math.floor((miny - oy) / mh) - 1
    j1 = math.ceil((maxy - oy) / mh) + 1
    for j in range(j0, j1 + 1):
        shift = ((j % period) * offset_frac * mw) if period > 1 else 0.0
        y0 = oy + j * mh
        i0 = math.floor((minx - ox - shift) / mw) - 1
        i1 = math.ceil((maxx - ox - shift) / mw) + 1
        for i in range(i0, i1 + 1):
            x0 = ox + shift + i * mw
            yield 'H', box(x0, y0, x0 + tile.w, y0 + tile.h)


def _herringbone(tile: Tile, joint: float, bbox, origin):
    """Прямая ёлочка. tile.w — длинная сторона L, tile.h — короткая W, L/W = r (целое).
    Ячейка c = W + шов. Горизонтальная плитка занимает r ячеек по X, вертикальная — r по Y.
    Лестницы: H в ячейке (k+(r+1)s, k-(r-1)s), V в ячейке (k+(r+1)s+r, k-(r-1)s) — верх V
    на уровне верха H, V уходит вниз на r ячеек. Покрытие плоскости без зазоров проверено
    (классы вычетов x-y mod 2r)."""
    L, W = tile.w, tile.h
    r = int(round(L / W))
    if r < 2 or abs(L / W - r) > 1e-6:
        raise ValueError(f"Ёлочка требует целого отношения сторон плитки, а у {tile.name} L/W={L / W:.3f}")
    c = W + joint
    ox, oy = origin
    minx, miny, maxx, maxy = bbox
    Xmin, Xmax = math.floor((minx - ox) / c) - r - 1, math.ceil((maxx - ox) / c) + r + 1
    Ymin, Ymax = math.floor((miny - oy) / c) - r - 1, math.ceil((maxy - oy) / c) + r + 1
    s0 = math.floor((Xmin - Ymax) / (2 * r)) - 1
    s1 = math.ceil((Xmax - Ymin) / (2 * r)) + 1
    for s in range(s0, s1 + 1):
        k0 = Ymin + (r - 1) * s - 1
        k1 = Ymax + (r - 1) * s + 1
        for k in range(k0, k1 + 1):
            X, Y = k + (r + 1) * s, k - (r - 1) * s
            if Xmin <= X <= Xmax and Ymin <= Y <= Ymax:
                x0, y0 = ox + X * c, oy + Y * c
                yield 'H', box(x0, y0, x0 + L, y0 + W)
            Xv, Yv = X + r, Y
            if Xmin <= Xv <= Xmax and Ymin <= Yv <= Ymax:
                x0 = ox + Xv * c
                y1 = oy + Yv * c + W             # верх вертикальной плитки = верх горизонтальной
                yield 'V', box(x0, y1 - L, x0 + W, y1)


def herringbone_selftest(r=2):
    """Проверка: ячейки-прямоугольники (без швов) покрывают область ровно один раз."""
    t = Tile('t', r * 100.0, 100.0)
    region = box(0, 0, 3000, 3000)
    polys = [p for _, p in _herringbone(t, 0.0, region.bounds, (0, 0))]
    u = unary_union(polys)
    inter = u.intersection(region)
    total = sum(p.intersection(region).area for p in polys)
    return abs(inter.area - region.area) < 1e-6 and abs(total - region.area) < 1e-6


# ----------------------------------------------------------------------------
# раскладка
# ----------------------------------------------------------------------------

def build_terrace(zones_cfg: dict):
    zones = {}
    for name, z in zones_cfg.items():
        if 'rect' in z:
            x0, y0, x1, y1 = z['rect']
            zones[name] = box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        else:
            zones[name] = Polygon(z['polygon'])
    terrace = unary_union(list(zones.values()))
    if isinstance(terrace, MultiPolygon):
        raise ValueError("Зоны не образуют единый контур")
    return terrace, zones


def layout(terrace: Polygon, zones: dict, tile: Tile, joint: float, variant: dict,
           origin_xy: tuple, x_mode: str) -> LayoutResult:
    """origin_xy = (xc, junction_y): точка, через которую проходит линия стыка;
    x_mode: 'joint' — на xc лежит шов; 'tile' — на xc лежит центр плитки."""
    pattern = variant['pattern']
    angle = float(variant.get('angle', 0.0))
    xc, yj = origin_xy
    # локальная система: повернуть террасу на -angle вокруг (xc, yj)
    pivot = (xc, yj)
    local_terrace = rotate(terrace, -angle, origin=pivot) if angle else terrace
    bbox = local_terrace.bounds
    oy = yj + joint / 2.0                         # шов симметричен относительно линии стыка
    if pattern == 'herringbone':
        ox = xc + joint / 2.0 if x_mode == 'joint' else xc - (tile.h + joint) / 2.0
        gen = _herringbone(tile, joint, bbox, (ox, oy))
    else:
        ox = xc + joint / 2.0 if x_mode == 'joint' else xc - tile.w / 2.0
        if pattern == 'grid':
            gen = _grid(tile, joint, bbox, (ox, oy))
        elif pattern == 'bond':
            off = float(variant.get('offset', 0.5))
            period = 2 if abs(off - 0.5) < 1e-9 else (3 if abs(off - 1 / 3) < 1e-9 else int(round(1 / off)))
            gen = _grid(tile, joint, bbox, (ox, oy), offset_frac=off, period=period)
        else:
            raise ValueError(f"Неизвестный паттерн {pattern}")

    prepared = prep(local_terrace)
    local_zones = {n: (rotate(z, -angle, origin=pivot) if angle else z) for n, z in zones.items()}
    pieces = []
    for orient, tp in gen:
        if not prepared.intersects(tp):
            continue
        inter = tp.intersection(local_terrace)
        if inter.is_empty or inter.area < 1e-6:
            continue
        parts = list(inter.geoms) if hasattr(inter, 'geoms') else [inter]
        for part in parts:
            if part.geom_type != 'Polygon' or part.area < 1.0:
                continue
            ratio = part.area / tile.area
            kind = 'full' if ratio >= FULL_RATIO else 'cut'
            # зона — по центроиду
            cpt = part.representative_point()
            zone = next((n for n, z in local_zones.items() if z.contains(cpt)), '?')
            minx, miny, maxx, maxy = part.bounds      # в локальной системе паттерна — оси плитки
            gpoly = rotate(part, angle, origin=pivot) if angle else part
            pieces.append(Piece(gpoly, kind, zone, orient, (maxx - minx, maxy - miny), ratio,
                                len(part.exterior.coords) - 1))
    res = LayoutResult(variant, tile, pieces, terrace, zones, (ox, oy), angle)
    res.stats = compute_stats(res, joint)
    return res


# ----------------------------------------------------------------------------
# статистика и расход
# ----------------------------------------------------------------------------

def _ffd_bins(items: list, capacity: float) -> int:
    """First-fit-decreasing: сколько целых плиток нужно, чтобы нарезать полосы items."""
    bins = []
    for it in sorted(items, reverse=True):
        for b in range(len(bins)):
            if bins[b] + it <= capacity + 1e-6:
                bins[b] += it
                break
        else:
            bins.append(it)
    return len(bins)


def compute_stats(res: LayoutResult, joint: float) -> dict:
    tile = res.tile
    full = [p for p in res.pieces if p.kind == 'full']
    cut = [p for p in res.pieces if p.kind == 'cut']
    st = {
        'full': len(full), 'cut': len(cut), 'pieces': len(res.pieces),
        'area_m2': res.terrace.area / 1e6,
        'per_zone': {},
    }
    for zn in res.zones:
        st['per_zone'][zn] = {
            'full': sum(1 for p in full if p.zone == zn),
            'cut': sum(1 for p in cut if p.zone == zn),
            'area_m2': res.zones[zn].area / 1e6,
        }
    # мелкие подрезы
    min_dims = []
    for p in cut:
        mrr = p.poly.minimum_rotated_rectangle
        xs, ys = mrr.exterior.coords.xy
        e1 = math.hypot(xs[1] - xs[0], ys[1] - ys[0])
        e2 = math.hypot(xs[2] - xs[1], ys[2] - ys[1])
        min_dims.append(min(e1, e2))
    st['min_piece_mm'] = min(min_dims) if min_dims else None
    st['slivers_lt_100'] = sum(1 for d in min_dims if d < 100)
    st['slivers_lt_third'] = sum(1 for d in min_dims if d < tile.short / 3)
    # расход с учётом обрезков
    orth = res.variant['pattern'] in ('grid', 'bond') and abs(res.angle % 90) < 1e-9
    if orth:
        tw, th = tile.w, tile.h
        side, end, corner = [], [], []
        for p in cut:
            w, h = p.local_dims
            fw, fh = w / tw, h / th
            if fh >= FULL_RATIO and fw < FULL_RATIO:
                side.append(w + KERF)
            elif fw >= FULL_RATIO and fh < FULL_RATIO:
                end.append(h + KERF)
            else:
                corner.append(p)
        with_reuse = len(full) + _ffd_bins(side, tw) + _ffd_bins(end, th) + len(corner)
        method = 'полосы по краям нарезаются из одной плитки (FFD), угловые куски — по плитке на кусок'
    else:
        # диагональ / ёлочка: треугольные куски (≤ 1/2 плитки) паруются по площади,
        # остальные — по плитке на кусок
        tri = [p.ratio for p in cut if p.nverts == 3 and p.ratio <= 0.5 + 1e-6]
        other = len(cut) - len(tri)
        with_reuse = len(full) + _ffd_bins([t + 0.02 for t in tri], 1.0) + other
        method = 'треугольные куски (≤½ плитки) паруются из одной плитки, остальные — по плитке на кусок'
    st['tiles_no_reuse'] = len(full) + len(cut)
    st['tiles_with_reuse'] = with_reuse
    st['reuse_method'] = method
    st['tile_m2'] = tile.area / 1e6
    st['net_m2'] = st['area_m2']
    st['waste_pct'] = (with_reuse * tile.area / res.terrace.area - 1) * 100
    return st


# ----------------------------------------------------------------------------
# выбор старта по X: шов или центр плитки на оси симметрии
# ----------------------------------------------------------------------------

def best_layout(terrace, zones, tile, joint, variant, xc, junction_y):
    cands = []
    for mode in ('joint', 'tile'):
        r = layout(terrace, zones, tile, joint, variant, (xc, junction_y), mode)
        r.stats['x_mode'] = mode
        cands.append(r)
    # критерии: нет подрезов < 100 мм, минимальный кусок побольше, меньше плиток
    def key(r):
        s = r.stats
        return (s['slivers_lt_100'], s['slivers_lt_third'], -(s['min_piece_mm'] or 0), s['tiles_with_reuse'])
    cands.sort(key=key)
    best = cands[0]
    best.stats['alternatives'] = [
        {'x_mode': c.stats['x_mode'], 'tiles_with_reuse': c.stats['tiles_with_reuse'],
         'min_piece_mm': c.stats['min_piece_mm'], 'slivers_lt_100': c.stats['slivers_lt_100']}
        for c in cands]
    return best
