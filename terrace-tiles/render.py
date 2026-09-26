# -*- coding: utf-8 -*-
"""Отрисовка раскладки: PNG/PDF (matplotlib) и DXF (ezdxf)."""
from __future__ import annotations

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon
from matplotlib.backends.backend_pdf import PdfPages
import ezdxf

from tiling import LayoutResult

ZONE_COLORS = {'covered': '#f3ead9', 'open': '#dfe9f3'}
PATTERN_RU = {'grid': 'прямая (шов в шов)', 'bond': 'вразбежку', 'herringbone': 'ёлочка'}


def _min_dim(poly):
    import math
    mrr = poly.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    return min(math.hypot(xs[1] - xs[0], ys[1] - ys[0]), math.hypot(xs[2] - xs[1], ys[2] - ys[1]))


def variant_title(res: LayoutResult, joint: float) -> str:
    v, t = res.variant, res.tile
    p = PATTERN_RU[v['pattern']]
    if v['pattern'] == 'bond':
        off = v.get('offset', 0.5)
        p += ' на ½' if abs(off - 0.5) < 1e-9 else ' на ⅓'
    if float(v.get('angle', 0)):
        p += f", {int(v['angle'])}°"
    return f"{v['id']}: плитка {t.name} мм, {p}, шов {joint:g} мм"


def draw_variant(res: LayoutResult, joint: float, project: str, notes: list[str], png_path: str,
                 pdf: PdfPages | None = None):
    st = res.stats
    minx, miny, maxx, maxy = res.terrace.bounds
    fig = plt.figure(figsize=(16.54, 11.69))  # A3 альбом
    ax = fig.add_axes([0.04, 0.10, 0.70, 0.80])
    ax.set_aspect('equal')
    # зоны
    for name, z in res.zones.items():
        ax.add_patch(MplPolygon(list(z.exterior.coords), closed=True, facecolor=ZONE_COLORS.get(name, '#eee'),
                                edgecolor='none', zorder=0))
    # плитки
    for p in res.pieces:
        if p.kind == 'full':
            fc, ec, lw, hatch = 'white', '#666', 0.4, None
        else:
            fc, ec, lw, hatch = ('#f4b183' if _min_dim(p.poly) < 100 else '#ffe699'), '#8a6d00', 0.6, '///'
        ax.add_patch(MplPolygon(list(p.poly.exterior.coords), closed=True, facecolor=fc, edgecolor=ec,
                                linewidth=lw, hatch=hatch, zorder=2))
    # контур и стык
    ax.add_patch(MplPolygon(list(res.terrace.exterior.coords), closed=True, fill=False, edgecolor='black',
                            linewidth=2.2, zorder=5))
    for name, z in res.zones.items():
        ax.add_patch(MplPolygon(list(z.exterior.coords), closed=True, fill=False, edgecolor='#1f4e79',
                                linewidth=1.2, linestyle=(0, (6, 4)), zorder=4))
    ox, oy = res.origin
    ax.axhline(oy - joint / 2, color='#c00000', lw=1.4, ls='--', zorder=6)
    ax.text(minx, oy - joint / 2 + 40, 'линия стыка крытой и открытой части — старт раскладки (шов на оси)',
            color='#c00000', fontsize=9, va='bottom', ha='left', zorder=7)
    # габариты
    pad = 250
    ax.annotate('', xy=(minx, maxy + pad), xytext=(maxx, maxy + pad), arrowprops=dict(arrowstyle='<->', lw=1))
    ax.text((minx + maxx) / 2, maxy + pad + 40, f'{(maxx - minx) / 1000:.2f} м', ha='center', va='bottom', fontsize=10)
    ax.annotate('', xy=(maxx + pad, miny), xytext=(maxx + pad, maxy), arrowprops=dict(arrowstyle='<->', lw=1))
    ax.text(maxx + pad + 40, (miny + maxy) / 2, f'{(maxy - miny) / 1000:.2f} м', ha='left', va='center', fontsize=10, rotation=90)
    for name, z in res.zones.items():
        zx0, zy0, zx1, zy1 = z.bounds
        ax.text(zx0 + 60, zy1 - 60, f"{name_ru(name)}: {(zx1 - zx0) / 1000:.2f} × {(zy1 - zy0) / 1000:.2f} м",
                fontsize=9, va='top', ha='left', color='#1f4e79', zorder=8,
                bbox=dict(facecolor='white', alpha=0.8, edgecolor='none', pad=2))
    ax.set_xlim(minx - 600, maxx + 700)
    ax.set_ylim(miny - 400, maxy + 700)
    ax.axis('off')
    fig.suptitle(project, fontsize=13, x=0.04, ha='left', y=0.965)
    fig.text(0.04, 0.925, variant_title(res, joint), fontsize=12, weight='bold')
    # блок статистики
    lines = [
        f"Площадь: {st['area_m2']:.2f} м²",
        f"Целых плиток: {st['full']}",
        f"Резаных кусков: {st['cut']}",
        f"Плиток без учёта обрезков: {st['tiles_no_reuse']}",
        f"Плиток с учётом обрезков: {st['tiles_with_reuse']}",
        f"  ({st['tiles_with_reuse'] * st['tile_m2']:.2f} м², отход {st['waste_pct']:.1f} %)",
        f"Мин. кусок: {st['min_piece_mm']:.0f} мм" if st['min_piece_mm'] else "Мин. кусок: —",
        f"Подрезов < 100 мм: {st['slivers_lt_100']}",
        f"Подрезов < ⅓ плитки: {st['slivers_lt_third']}",
        f"Старт по X: {'шов' if st.get('x_mode') == 'joint' else 'центр плитки'} на оси",
        "",
        "По зонам:",
    ]
    for zn, zs in st['per_zone'].items():
        lines.append(f"  {name_ru(zn)}: {zs['area_m2']:.2f} м², целых {zs['full']}, резаных {zs['cut']}")
    lines += ["", "Условные обозначения:", "  белое — целая плитка", "  штрих жёлтый — резаная", "  штрих оранжевый — кусок < 100 мм"]
    fig.text(0.76, 0.86, "\n".join(lines), fontsize=10, va='top', ha='left', family='DejaVu Sans',
             bbox=dict(facecolor='#fafafa', edgecolor='#999', pad=8))
    if notes:
        fig.text(0.04, 0.03, "Примечания: " + " | ".join(notes), fontsize=8.5, color='#555', wrap=True)
    fig.savefig(png_path, dpi=110)
    if pdf is not None:
        pdf.savefig(fig)
    plt.close(fig)


def name_ru(zone: str) -> str:
    return {'covered': 'Крытая часть', 'open': 'Открытая часть'}.get(zone, zone)


def write_dxf(res: LayoutResult, joint: float, path: str):
    doc = ezdxf.new('R2010', setup=True)
    doc.header['$INSUNITS'] = 4  # мм
    msp = doc.modelspace()
    layers = {'KONTUR': 7, 'ZONY': 5, 'PLITKA_CEL': 8, 'PLITKA_REZ': 2, 'PLITKA_MELK': 1, 'STYK': 1, 'TEKST': 3}
    for name, color in layers.items():
        doc.layers.add(name, color=color)
    msp.add_lwpolyline(list(res.terrace.exterior.coords), close=True, dxfattribs={'layer': 'KONTUR', 'const_width': 0})
    for name, z in res.zones.items():
        msp.add_lwpolyline(list(z.exterior.coords), close=True, dxfattribs={'layer': 'ZONY'})
    for p in res.pieces:
        if p.kind == 'full':
            lay = 'PLITKA_CEL'
        else:
            lay = 'PLITKA_MELK' if _min_dim(p.poly) < 100 else 'PLITKA_REZ'
        msp.add_lwpolyline(list(p.poly.exterior.coords), close=True, dxfattribs={'layer': lay})
    ox, oy = res.origin
    minx, miny, maxx, maxy = res.terrace.bounds
    y = oy - joint / 2
    msp.add_line((minx - 300, y), (maxx + 300, y), dxfattribs={'layer': 'STYK', 'linetype': 'DASHED'})
    st = res.stats
    txt = (f"{variant_title(res, joint)} | целых {st['full']}, резаных {st['cut']}, "
           f"плиток с учётом обрезков {st['tiles_with_reuse']}")
    msp.add_text(txt, dxfattribs={'layer': 'TEKST', 'height': 80}).set_placement((minx, maxy + 500))
    doc.saveas(path)
