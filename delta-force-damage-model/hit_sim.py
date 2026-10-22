#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""散布-部位-击杀 Monte Carlo 模拟 (hit_sim)

在 damage_calc.py 的伤害公式之上加入弹着散布与部位判定, 输出 BTK/TTK 概率分布.

散布模型 (几何按参考图 475×608 像素实测: 椭圆中心 (239,270.5), 190px = 1 单位):
    共 4 条同心椭圆(最外 = 100% 散布半径, 向内约 82% / 58% / 40%),
    最内椭圆 50%, 外 3 个环带均分其余 50%(各 ≈16.7%), 区域内按面积均匀落点.
    参考图为 3D 透视投影, 各环圆心逐环略上移, 故逐环记录 (cx, cy, a, b).
部位判定 (归一化坐标, 原点=椭圆中心即胸腹交界, y向下为正, 先矩形后胶囊):
    头/胸/腹为矩形(胸腹取参考图蓝框), 左右臂与左右腿为胶囊(线段+半宽, 贴合斜向肢体),
    判定区外其余=空枪.
后继弹两种模型:
    A 相关模型: 锚点=最近一次"重锚"落点, 后续弹以锚点为圆心按正圆散布
                (半径=主散布水平半轴×FOLLOW_SCALE, 圆心不偏移), 4 段同心圆的概率按
                FOLLOW_WEIGHTS 由外向内递增; 若连续 重锚空枪数 发空枪, 说明锚点已明显
                偏离人体, 则下一发重新按首发判定(回到主散布 RINGS)并成为新锚点,
                避免整梭跟着偏离点一路打空;
    B 独立模型: 每发与第一发独立同分布.
结算规则:
    胸->身甲; 腹->身甲(若该甲防护腹部); 头/手/腿->无甲; 空枪->无结算但占射击间隔.
    BTK = 击杀所需发射数(含空枪); TTK = 逐发累加间隔(默认 60/射速, 特殊射速/连发分组
    曲线见 damage_calc.射击用时); 超过上限记未击杀.

用法
----
  python hit_sim.py --枪 K416 --弹 M995 --甲 5 --距离 20 --模型 both
  python hit_sim.py --枪 AKM --弹 "7.62x39 AP" --甲 4 --距离 30 --模型 A --次数 50000
"""
import argparse
import csv
import json
import math
import os
import random
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import damage_calc as dc

# ------------------------------------------------------------------ 散布/几何参数
# 全部数值由参考图(475×608)像素实测换算: (像素 x - 239)/190, (像素 y - 270.5)/190
RINGS = [  # (cx, cy, a, b) 外→内: 圆心偏移 + 水平/垂直半轴
    (0.000,  0.000, 1.000, 1.166),   # 100% 散布半径
    (0.000, -0.103, 0.816, 0.953),   # ~82%
    (0.000, -0.221, 0.584, 0.650),   # ~58%
    (0.000, -0.271, 0.395, 0.479),   # ~40%
]
RING_PROB = [0.5 / 3, 0.5 / 3, 0.5 / 3, 0.5]   # 各区域概率(外→内): 最内 50%, 外三环均分 50%

RECTS = [  # (部位, x0, x1, y0, y1) 按列表顺序判定; 头为头盔外廓, 胸/腹取参考图蓝框
    ("头", -0.290, 0.258, -1.203, -0.671),
    ("胸", -0.332, 0.321, -0.666, -0.261),
    ("腹", -0.337, 0.316, -0.229, 0.476),
]
BANDS = [  # (部位, x1, y1, x2, y2, 半宽) 胶囊判定(线段+半宽), 在 RECTS 之后判定
    ("手", -0.479, -0.476, -0.858, 0.524, 0.130),   # 左臂(自肩斜向下外)
    ("手",  0.479, -0.476,  0.858, 0.524, 0.130),   # 右臂
    ("腿", -0.218,  0.476, -0.218, 1.776, 0.171),   # 左腿(腹下缘至脚底)
    ("腿",  0.279,  0.476,  0.279, 1.776, 0.171),   # 右腿
]

FOLLOW_SCALE = 0.35                         # 模型A: 后继散布半径=主散布水平半轴×0.35 (原 0.5 再收 30%)
FOLLOW_WEIGHTS = [0.1, 0.2, 0.3, 0.4]       # 模型A: 同心圆环概率(外→内递增, 即离锚点越近越密)
FOLLOW_RINGS = [  # (0, 0, r, r) 正圆环系: 主散布椭圆含参考图3D透视拉伸, 不适用于后继独立密度图
    (0.0, 0.0, a * FOLLOW_SCALE, a * FOLLOW_SCALE) for _, _, a, _ in RINGS
]
重锚空枪数 = 2                              # 模型A: 连续这么多发空枪后, 下一发按首发重取(回主散布)并成为新锚点

PLAYER_HP = 100.0
SHOT_CAP = 120                              # 单次模拟发射上限(超过记未击杀)
目标甲配对 = [                              # 批量模式的护甲目标: (身甲等级, 身甲耐久, 头盔名称)
    # 4级甲只搭配4级头: DICH训练头盔(35) / GT1战术头盔(48)
    (4, 90.0,  "DICH训练头盔"), (4, 90.0,  "GT1战术头盔"),
    (4, 100.0, "DICH训练头盔"), (4, 100.0, "GT1战术头盔"),
    (4, 110.0, "DICH训练头盔"), (4, 110.0, "GT1战术头盔"),
    # 5级甲搭配5级头(DICH-1=40 / H09防暴=45 / GN重型=50)或35耐久4级头
    (5, 95.0,  "DICH-1战术头盔"), (5, 95.0,  "H09防暴头盔"), (5, 95.0,  "GN重型头盔"), (5, 95.0,  "DICH训练头盔"),
    (5, 105.0, "DICH-1战术头盔"), (5, 105.0, "H09防暴头盔"), (5, 105.0, "GN重型头盔"), (5, 105.0, "DICH训练头盔"),
    (5, 125.0, "DICH-1战术头盔"), (5, 125.0, "H09防暴头盔"), (5, 125.0, "GN重型头盔"), (5, 125.0, "DICH训练头盔"),
    (None, None, None),   # 无甲目标: 无身甲无头盔(基线), 置于末尾
]


# ---------------------------------------------------------------- 落点与判定
def 落点(rng, 环=None, rings=None, 权重=None):
    """采样一个落点(归一化坐标)。rings=None 用主散布 RINGS(椭圆), 后继散布改传 FOLLOW_RINGS(正圆)。
    环=None 时按 权重(默认 RING_PROB) 抽取区域; 区域内按面积均匀, 落到内侧环内则重取(拒绝采样)。"""
    if rings is None:
        rings = RINGS
    w = 权重 if 权重 is not None else RING_PROB
    if 环 is None:
        环 = rng.choices(range(len(rings)), weights=w)[0]
    cx, cy, a, b = rings[环]
    while True:
        u = math.sqrt(rng.random())                      # 面积均匀: 半径取 sqrt
        th = rng.uniform(0.0, 2.0 * math.pi)
        x = cx + a * u * math.cos(th)
        y = cy + b * u * math.sin(th)
        if 环 + 1 >= len(rings):
            return x, y
        nx, ny, na, nb = rings[环 + 1]
        if ((x - nx) / na) ** 2 + ((y - ny) / nb) ** 2 > 1.0:
            return x, y


def _胶囊内(x, y, x1, y1, x2, y2, r):
    """点到线段的距离是否在半宽 r 内(胶囊判定)。"""
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0.0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
    return (x - (x1 + t * dx)) ** 2 + (y - (y1 + t * dy)) ** 2 <= r * r


def 部位判定(x, y):
    for p, x0, x1, y0, y1 in RECTS:
        if x0 <= x <= x1 and y0 <= y <= y1:
            return p
    for p, x1, y1, x2, y2, r in BANDS:
        if _胶囊内(x, y, x1, y1, x2, y2, r):
            return p
    return "空枪"


def 次发(rng, 模型, 锚, 连空):
    """产出下一发的 (x, y, 部位, 新锚, 新连空, 是否重锚), 三处调用点共用同一规则。
    模型B 各发独立同分布; 模型A 以锚点为圆心取后继散布, 连续 重锚空枪数 发空枪后
    该发回到主散布(即"按首发判定")并成为新锚点(重锚=1)。"""
    if 模型 == "B" or 锚 is None or 连空 >= 重锚空枪数:
        x, y = 落点(rng)
        新锚 = (x, y)
        重锚 = 锚 is not None
    else:
        dx, dy = 落点(rng, rings=FOLLOW_RINGS, 权重=FOLLOW_WEIGHTS)
        x, y = 锚[0] + dx, 锚[1] + dy
        新锚 = 锚
        重锚 = False
    part = 部位判定(x, y)
    return x, y, part, 新锚, (连空 + 1 if part == "空枪" else 0), 重锚


def 部位概率(n=400000):
    """大样本估计各部位命中概率(含空枪), 用于展示散布-几何层。"""
    rng = random.Random(7)
    cnt = {}
    for _ in range(n):
        p = 部位判定(*落点(rng))
        cnt[p] = cnt.get(p, 0) + 1
    return {k: v / n for k, v in cnt.items()}


def 对应护甲(part, 身甲, 头甲=None):
    """命中部位对应的护甲对象: 头→头盔(独立耐久), 胸/受防护的腹肩→身甲, 手腿→无甲。"""
    return dc.部位对应护甲(part, 身甲, 头甲)


def 甲键(part, 身甲, 头甲):
    """命中部位属于哪个护甲池: 2=头盔, 1=身甲, 0=无甲。头与身两池互不相干。"""
    a = dc.部位对应护甲(part, 身甲, 头甲)
    if a is None:
        return 0
    return 2 if a is 头甲 else 1


# ---------------------------------------------------------------- 单次模拟
def 模拟一次(gun, ammo, 身甲, 头甲, dist, rng, 模型):
    """返回 (发射数, 击杀用时s 或 None=未击杀)。身甲与头盔各自独立损耗。"""
    hp, 发射 = PLAYER_HP, 0
    甲值 = [0.0, 身甲.护甲值 if 身甲 else 0.0, 头甲.护甲值 if 头甲 else 0.0]
    甲级 = [None, 身甲.等级 if 身甲 else None, 头甲.等级 if 头甲 else None]
    锚, 连空 = None, 0
    while hp > 1e-9 and 发射 < SHOT_CAP:
        发射 += 1
        x, y, part, 锚, 连空, _ = 次发(rng, 模型, 锚, 连空)
        if part != "空枪":
            k = 甲键(part, 身甲, 头甲)
            肉伤, 甲伤, _ = dc.单发结算(gun, ammo, part, dist, 甲级[k], 甲值[k])
            if k:
                甲值[k] = max(0.0, 甲值[k] - 甲伤)
            hp -= 肉伤
    用时 = dc.射击用时(gun, 发射) if hp <= 1e-9 else None
    return 发射, 用时


def 模拟批量(gun, ammo, 身甲, 头甲, dist, 模型, n, seed):
    rng = random.Random(seed)
    btk_hist = {}
    ttk_list, 未杀 = [], 0
    for _ in range(n):
        发射, 用时 = 模拟一次(gun, ammo, 身甲, 头甲, dist, rng, 模型)
        btk_hist[发射] = btk_hist.get(发射, 0) + 1
        if 用时 is None:
            未杀 += 1
        else:
            ttk_list.append(用时)
    btk = sorted(btk_hist.items())
    ttk_list.sort()
    return {
        "hist": [(k, v / n) for k, v in btk],
        "btk均值": sum(k * v for k, v in btk) / n,
        "ttk均值": sum(ttk_list) / len(ttk_list) if ttk_list else None,
        "ttk50": ttk_list[len(ttk_list) // 2] if ttk_list else None,
        "ttk90": ttk_list[int(len(ttk_list) * 0.9)] if ttk_list else None,
        "未杀率": 未杀 / n,
    }


# ---------------------------------------------------------------- 展示
def 模拟一次记录(gun, ammo, 身甲, 头甲, dist, rng, 模型):
    """同 模拟一次, 但逐发记录落点/部位/伤害, 用于回放。"""
    hp, 发射 = PLAYER_HP, 0
    甲值 = [0.0, 身甲.护甲值 if 身甲 else 0.0, 头甲.护甲值 if 头甲 else 0.0]
    甲级 = [None, 身甲.等级 if 身甲 else None, 头甲.等级 if 头甲 else None]
    锚, 连空 = None, 0
    recs = []
    while hp > 1e-9 and 发射 < SHOT_CAP:
        发射 += 1
        用锚 = 锚
        x, y, part, 锚, 连空, 重锚 = 次发(rng, 模型, 锚, 连空)
        ax, ay = (x, y) if (用锚 is None or 重锚) else 用锚
        肉伤 = 甲伤 = 0.0
        if part != "空枪":
            k = 甲键(part, 身甲, 头甲)
            肉伤, 甲伤, _ = dc.单发结算(gun, ammo, part, dist, 甲级[k], 甲值[k])
            if k:
                甲值[k] = max(0.0, 甲值[k] - 甲伤)
            hp -= 肉伤
        recs.append({"n": 发射, "x": round(x, 4), "y": round(y, 4), "part": part,
                     "flesh": round(肉伤, 2), "armor": round(甲伤, 2),
                     "hp": round(max(hp, 0.0), 2), "dur": round(甲值[1], 2), "durh": round(甲值[2], 2),
                     "ax": round(ax, 4), "ay": round(ay, 4), "re": 1 if 重锚 else 0,
                     "t": round(dc.射击用时(gun, 发射) * 1000)})
        if hp <= 1e-9:
            break
    return recs


_REPLAY_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>击杀回放 - __TITLE__</title>
<style>
 body{background:#12151c;color:#dfe4ee;font-family:"Microsoft YaHei",sans-serif;margin:0;display:flex;gap:18px;padding:18px}
 .left{flex:0 0 560px}.right{flex:1;min-width:320px}
 h2{font-size:16px;margin:4px 0 10px;color:#9fb3d1;font-weight:normal}
 svg{background:#171b24;border:1px solid #2a3242;border-radius:8px}
 .panel{background:#171b24;border:1px solid #2a3242;border-radius:8px;padding:12px 14px;margin-bottom:12px}
 button{background:#2a3242;color:#dfe4ee;border:0;border-radius:6px;padding:7px 16px;margin-right:8px;cursor:pointer;font-size:14px}
 button:hover{background:#3b465c} button.on{background:#3d6db5}
 .bars{margin:8px 0}
 .barwrap{background:#242b39;border-radius:4px;height:16px;position:relative;margin:6px 0}
 .bar{height:100%;border-radius:4px;transition:width .15s}
 #hpbar{background:#d9534f}#durbar{background:#4a90d9}#hdurbar{background:#2e9e7a}
 .barlabel{position:absolute;left:6px;top:0;font-size:12px;line-height:16px;color:#fff;text-shadow:0 0 3px #000}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th{color:#8fa3c0;text-align:left;padding:3px 6px;border-bottom:1px solid #2a3242}
 td{padding:3px 6px;border-bottom:1px solid #1e2430}
 tr.cur{background:#24304a}
 #status{font-size:15px;margin-top:6px;min-height:22px}
 .kill{color:#7dd87d;font-weight:bold}
 .legend{font-size:12px;color:#8fa3c0;line-height:1.7}
 .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
</style></head><body>
<div class="left">
<h2>__TITLE__ &nbsp;|&nbsp; 模型__MODEL__ __MODELDESC__</h2>
<svg id="svg" viewBox="-1.15 -1.26 2.30 3.02" width="560" height="735"></svg>
<div class="panel legend">
 散布椭圆系: 4 条同心椭圆(外→内 100% / 82% / 58% / 40% 半径), 最内圈50%、外三环各≈16.7% &nbsp;|&nbsp;
 <span class="sw" style="background:#e74c3c"></span>头
 <span class="sw" style="background:#e67e22"></span>胸
 <span class="sw" style="background:#f1c40f"></span>腹
 <span class="sw" style="background:#3498db"></span>手
 <span class="sw" style="background:#9b59b6"></span>腿
 <span class="sw" style="background:#6b7280"></span>空枪
</div>
</div>
<div class="right">
<div class="panel">
 <button id="play" class="on">▶ 播放</button>
 <button id="step">单步</button>
 <button id="reset">重置</button>
 <span style="margin-left:8px">速度</span>
 <button class="spd" data-i="0">慢</button><button class="spd on" data-i="1">中</button><button class="spd" data-i="2">快</button>
</div>
<div class="panel bars">
 生命值 <div class="barwrap"><div id="hpbar" class="bar" style="width:100%"></div><div class="barlabel" id="hplabel">100 / 100</div></div>
 <div id="durbox">身甲耐久 <div class="barwrap"><div id="durbar" class="bar" style="width:100%"></div><div class="barlabel" id="durlabel">__DUR0__ / __DUR0__</div></div></div>
 <div id="hdurbox">头盔耐久 <div class="barwrap"><div id="hdurbar" class="bar" style="width:100%"></div><div class="barlabel" id="hdurlabel">__DURH0__ / __DURH0__</div></div></div>
 <div id="status"></div>
</div>
<div class="panel">
 <h2 id="ftitle">后继落点密度 (模型__MODEL__)</h2>
<!-- 与左侧主图同比例尺: 主图 viewBox 宽 2.30 单位 × 560px = 243.5 px/单位, 小图 1.16 单位 × 243.5 ≈ 282.4px -->
 <svg id="fsvg" viewBox="-0.58 -0.58 1.16 1.16" width="282.43" height="282.43" style="margin:0 auto"></svg>
 <div class="legend" id="flegend"></div>
</div>
<div class="panel" style="max-height:520px;overflow:auto">
<table id="log"><thead><tr><th>发</th><th>t(ms)</th><th>部位</th><th>肉伤</th><th>甲伤</th><th>剩余血</th><th>剩余身甲</th><th>剩余头甲</th></tr></thead><tbody></tbody></table>
</div>
</div>
<script>
const D = __PAYLOAD__;
const COLORS = {"头":"#e74c3c","胸":"#e67e22","腹":"#f1c40f","手":"#3498db","腿":"#9b59b6","空枪":"#6b7280"};
const svg = document.getElementById("svg"), NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, text){ const e = document.createElementNS(NS, tag);
 for(const k in attrs) e.setAttribute(k, attrs[k]); if(text!=null) e.textContent = text; svg.appendChild(e); return e; }
// 静态层: 空枪底 + 简笔人体与部位判定区(同一套几何, 保证与判定一致) + 红圈
el("ellipse",{cx:0,cy:0,rx:D.rings[0][2]*1.02,ry:D.rings[0][3]*1.02,fill:"#1a1e28"});
D.rects.forEach(r=>{ el("rect",{x:r[1],y:r[3],width:r[2]-r[1],height:r[4]-r[3],fill:"#2a3140",stroke:"#3a4356","stroke-width":0.010}); });
D.bands.forEach(b=>{ el("line",{x1:b[1],y1:b[2],x2:b[3],y2:b[4],stroke:"#2a3140","stroke-width":2*b[5],"stroke-linecap":"round"}); });
D.rects.forEach(r=>{ el("rect",{x:r[1],y:r[3],width:r[2]-r[1],height:r[4]-r[3],fill:COLORS[r[0]],opacity:0.16,stroke:COLORS[r[0]],"stroke-width":0.006}); });
D.bands.forEach(b=>{ el("line",{x1:b[1],y1:b[2],x2:b[3],y2:b[4],stroke:COLORS[b[0]],opacity:0.22,"stroke-width":2*b[5],"stroke-linecap":"round"}); });
D.rings.forEach((g,i)=>{ el("ellipse",{cx:g[0],cy:g[1],rx:g[2],ry:g[3],fill:"none",stroke:"#e05252","stroke-width":i==0?0.012:0.007,opacity:i==0?0.95:0.65}); });
D.rects.forEach(r=>{ el("text",{x:(r[1]+r[2])/2,y:(r[3]+r[4])/2+0.03,"font-size":0.07,fill:COLORS[r[0]],"text-anchor":"middle",opacity:0.85}, r[0]); });
[["手",-0.67,0.02],["腿",-0.22,0.77]].forEach(t=>{ el("text",{x:t[1],y:t[2],"font-size":0.07,fill:COLORS[t[0]],"text-anchor":"middle",opacity:0.85}, t[0]); });
if(D.model==="A"){ el("line",{x1:-0.04,y1:0,x2:0.04,y2:0,stroke:"#e05252","stroke-width":0.006}); el("line",{x1:0,y1:-0.04,x2:0,y2:0.04,stroke:"#e05252","stroke-width":0.006}); }
// 后继落点密度小图(固定中心=锚点), 同心圆按 FOLLOW_WEIGHTS 深浅着色
const fx = document.getElementById("fsvg"), flegend = document.getElementById("flegend");
function fel(tag, attrs, text){ const e = document.createElementNS(NS, tag);
 for(const k in attrs) e.setAttribute(k, attrs[k]); if(text!=null) e.textContent = text; fx.appendChild(e); return e; }
(function(){
 const F = D.follow;
 if(D.model !== "A" || !F){ document.getElementById("ftitle").textContent = "后继落点密度";
   flegend.innerHTML = '<span style="color:#8fa3c0">模型B 各发独立同分布, 无后继锚定散布</span>'; return; }
 const wmax = Math.max.apply(null, F.weights), op = w => 0.10 + 0.50 * w / wmax;
 fel("ellipse", {cx:0, cy:0, rx:F.rings[0][2]*1.06, ry:F.rings[0][3]*1.06, fill:"#1a1e28"});
 F.rings.forEach((g,i) => { fel("ellipse", {cx:g[0], cy:g[1], rx:g[2], ry:g[3], fill:"#e08a3c", "fill-opacity":op(F.weights[i]).toFixed(3)}); });
 F.rings.forEach(g => { fel("ellipse", {cx:g[0], cy:g[1], rx:g[2], ry:g[3], fill:"none", stroke:"#e08a3c", "stroke-width":0.012, opacity:0.8}); });
 fel("circle", {cx:0, cy:0, r:0.022, fill:"#e05252", stroke:"#fff", "stroke-width":0.006});
 flegend.innerHTML = '<span class="sw" style="background:#e05252"></span>锚点(最近重锚) &nbsp; 半径 = 主散布水平半径 × ' + F.scale
   + '<br>同心圆(外→内) 概率 ' + F.weights.map(w => '<span class="sw" style="background:#e08a3c;opacity:' + op(w).toFixed(3) + '"></span>' + (w*100).toFixed(0) + '%').join(" ")
   + '<br>连续 ' + F.reanchor + ' 发空枪后, 下一发回到主散布重新取锚点 (红虚线圈 = 该重锚发)';
})();
const layer = el("g",{});
// 回放状态
let i = 0, timer = null, speed = [420,160,60][1];
const hpbar = document.getElementById("hpbar"), durbar = document.getElementById("durbar"), hdurbar = document.getElementById("hdurbar");
if(!D.durh0){ document.getElementById("hdurbox").style.display = "none"; }
const tbody = document.querySelector("#log tbody"), status = document.getElementById("status");
function pel(tag, attrs, text){ const e = document.createElementNS(NS, tag);
 for(const k in attrs) e.setAttribute(k, attrs[k]); if(text!=null) e.textContent = text; layer.appendChild(e); return e; }
function render(s){
 if(D.model==="A" && s.n>1 && !s.re && Math.abs(s.ax-s.x)+Math.abs(s.ay-s.y)>0.02){
   pel("line",{x1:s.ax,y1:s.ay,x2:s.x,y2:s.y,stroke:"#e05252","stroke-width":0.003,opacity:0.45,"stroke-dasharray":"0.02 0.015"}); }
 if(s.re){ pel("circle",{cx:s.x,cy:s.y,r:0.034,fill:"none",stroke:"#e05252","stroke-width":0.005,"stroke-dasharray":"0.014 0.012"}); }
 if(s.part === "空枪"){
   pel("text",{x:s.x,y:s.y+0.02,"font-size":0.045,fill:COLORS["空枪"],"text-anchor":"middle"}, "×");
 } else {
   pel("circle",{cx:s.x,cy:s.y,r:0.018,fill:COLORS[s.part],stroke:"#fff","stroke-width":0.004});
   pel("text",{x:s.x+0.03,y:s.y+0.015,"font-size":0.04,fill:"#dfe4ee"}, s.n);
 }
 hpbar.style.width = (s.hp/100*100)+"%"; hpbar.parentNode.querySelector(".barlabel").textContent = s.hp+" / 100";
 durbar.style.width = (D.dur0? s.dur/D.dur0*100:0)+"%"; durbar.parentNode.querySelector(".barlabel").textContent = s.dur+" / "+D.dur0;
 if(D.durh0){ hdurbar.style.width = (s.durh/D.durh0*100)+"%"; hdurbar.parentNode.querySelector(".barlabel").textContent = s.durh+" / "+D.durh0; }
 const tr = document.createElement("tr");
 tr.innerHTML = `<td>${s.n}</td><td>${s.t}</td><td style="color:${COLORS[s.part]}">${s.part}</td><td>${s.flesh}</td><td>${s.armor}</td><td>${s.hp}</td><td>${s.dur}</td><td>${s.durh}</td>`;
 tbody.appendChild(tr); tr.scrollIntoView({block:"nearest"});
 status.innerHTML = `第 <b>${s.n}</b> 发 [${s.t}ms] 命中 <b style="color:${COLORS[s.part]}">${s.part}</b>` +
   (s.part==="空枪" ? "，未造成伤害" : `，肉伤 ${s.flesh} / 甲伤 ${s.armor}`);
}
function finish(){
 const last = D.shots[D.shots.length-1];
 status.innerHTML = `<span class="kill">击杀!</span> BTK = ${last.n} 发, TTK = ${last.t} ms`;
 clearInterval(timer); timer = null; playBtn.textContent = "▶ 播放";
}
function step(){ if(i >= D.shots.length){ finish(); return; } render(D.shots[i]); i++; }
const playBtn = document.getElementById("play");
playBtn.onclick = ()=>{ if(timer){ clearInterval(timer); timer=null; playBtn.textContent="▶ 播放"; }
 else { playBtn.textContent = "⏸ 暂停"; if(i>=D.shots.length) reset(); timer = setInterval(()=>{ step(); if(i>=D.shots.length){ finish(); } }, speed); } };
document.getElementById("step").onclick = ()=>{ if(timer) playBtn.onclick(); step(); };
function reset(){ i=0; if(timer){clearInterval(timer); timer=null;} playBtn.textContent="▶ 播放"; svg.querySelectorAll("g g, circle.shot, text.shot").forEach(e=>e.remove());
 while(layer.firstChild) layer.firstChild.remove(); tbody.innerHTML=""; status.innerHTML="";
 hpbar.style.width="100%"; hpbar.parentNode.querySelector(".barlabel").textContent="100 / 100";
 durbar.style.width="100%"; durbar.parentNode.querySelector(".barlabel").textContent=D.dur0+" / "+D.dur0;
 if(D.durh0){ hdurbar.style.width="100%"; hdurbar.parentNode.querySelector(".barlabel").textContent=D.durh0+" / "+D.durh0; } }
document.getElementById("reset").onclick = reset;
document.querySelectorAll(".spd").forEach(b=>{ b.onclick = ()=>{ document.querySelectorAll(".spd").forEach(x=>x.classList.remove("on"));
 b.classList.add("on"); speed = [420,160,60][b.dataset.i]; if(timer){ clearInterval(timer); timer = setInterval(()=>{ step(); if(i>=D.shots.length) finish(); }, speed); } }; });
</script></body></html>"""


def 导出回放HTML(gun, ammo, 身甲, 头甲, dist, 模型, seed, 路径):
    rng = random.Random(seed)
    recs = 模拟一次记录(gun, ammo, 身甲, 头甲, dist, rng, 模型)
    last = recs[-1]
    身值 = 身甲.护甲值 if 身甲 else 0.0
    头值 = 头甲.护甲值 if 头甲 else 0.0
    payload = {
        "model": 模型,
        "dur0": round(身值, 2),
        "durh0": round(头值, 2),
        "shots": recs,
        "rects": [[p, x0, x1, y0, y1] for p, x0, x1, y0, y1 in RECTS],
        "bands": [[p, x1, y1, x2, y2, r] for p, x1, y1, x2, y2, r in BANDS],
        "rings": [list(g) for g in RINGS],
        "follow": {"scale": FOLLOW_SCALE, "weights": FOLLOW_WEIGHTS,
                   "reanchor": 重锚空枪数,
                   "rings": [list(g) for g in FOLLOW_RINGS]},
    }
    desc = (f"(锚定最近重锚落点, 后继弹向周围概率递减, 连空{重锚空枪数}发回主散布重取)"
            if 模型 == "A" else "(各发独立同分布)")
    头盔 = f" + 头盔[{头甲.名称}]{头值:.0f}耐久" if 头甲 else " (无头盔)"
    title = (f"{gun.名称} × {ammo.名称} vs {身甲.等级}级身甲[{身甲.名称}]{身值:.0f}耐久{头盔}"
             f" @{dist:.0f}m — {len(recs)}发击杀 TTK {last['t']}ms")
    html = (_REPLAY_TEMPLATE
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
            .replace("__TITLE__", title)
            .replace("__MODEL__", 模型)
            .replace("__MODELDESC__", desc)
            .replace("__DUR0__", f"{身值:.0f}")
            .replace("__DURH0__", f"{头值:.0f}"))
    with open(路径, "w", encoding="utf-8") as f:
        f.write(html)
    return 路径, recs


def 打印分布(标题, res):
    print(f"\n--- {标题} ---")
    print("BTK分布(发射数:概率):")
    acc = 0.0
    for k, p in res["hist"]:
        acc += p
        if k <= 15 or acc >= 0.995:
            bar = "#" * int(p * 100)
            print(f"  {k:>3}发 {p*100:5.2f}%  {bar}")
    t50 = f'{res["ttk50"]*1000:.0f}ms' if res["ttk50"] is not None else "-"
    t90 = f'{res["ttk90"]*1000:.0f}ms' if res["ttk90"] is not None else "-"
    tm = f'{res["ttk均值"]*1000:.0f}ms' if res["ttk均值"] is not None else "-"
    print(f"  期望BTK={res['btk均值']:.2f}  TTK均值={tm}  "
          f"P50={t50}  P90={t90}  未击杀率={res['未杀率']*100:.2f}%")


# ---------------------------------------------------------------- 批量概率分布
_部位序 = ["空枪", "头", "胸", "腹", "手", "腿"]
_部位码 = {p: i for i, p in enumerate(_部位序)}


def 预采样部位(rng, 次数, 模型):
    """预采样 次数×SHOT_CAP 的部位码序列(bytearray, 每 run 连续 SHOT_CAP 字节)。
    落点几何只由散布模型决定, 与枪弹无关, 因此同一模型下所有组合可复用同一序列。"""
    seq = bytearray(次数 * SHOT_CAP)
    for r in range(次数):
        base = r * SHOT_CAP
        锚, 连空 = None, 0
        for k in range(SHOT_CAP):
            _, _, part, 锚, 连空, _ = 次发(rng, 模型, 锚, 连空)
            seq[base + k] = _部位码[part]
    return seq


def 池参数(gun, ammo, 护甲, dist, 无甲):
    """单个护甲池(身甲或头盔)的破甲参数: 每发甲伤/b/未破[]/破发[]。
    甲伤不乘部位倍率/基伤比例, 故整场战斗每发甲伤恒定, 破甲临界命中数固定。"""
    甲级 = 护甲.等级
    穿比 = dc.穿透比例(ammo.穿透等级, 甲级)
    每发甲伤 = dc.单发结算(gun, ammo, "胸", dist, 甲级, 护甲.护甲值)[2]["理论甲伤"]
    if not 每发甲伤:
        return {"每发甲伤": 0.0, "b": SHOT_CAP + 1,
                "未破": [v * 穿比 for v in 无甲], "破发": [v * 穿比 for v in 无甲]}
    b = math.ceil(护甲.护甲值 / 每发甲伤)
    占比 = min(1.0, (护甲.护甲值 - (b - 1) * 每发甲伤) / 每发甲伤)
    未破 = [v * 穿比 for v in 无甲]
    破发 = [v * (占比 * 穿比 + 1.0 - 占比) for v in 无甲]   # 破甲那发: 溢出部分按无甲100%结算
    return {"每发甲伤": 每发甲伤, "b": b, "未破": 未破, "破发": 破发}


def 伤害表(gun, ammo, 身甲, 头甲, dist):
    """返回 (无甲[6], 池{1:身甲池, 2:头盔池})。身甲与头盔各自独立破甲, 互不相干。"""
    无甲 = [0.0] * 6
    for i, p in enumerate(_部位序):
        if p != "空枪":
            无甲[i] = dc.单发结算(gun, ammo, p, dist)[0]      # 甲级=None → 无甲肉伤
    池 = {}
    for k, 护甲 in ((1, 身甲), (2, 头甲)):
        if 护甲 is None:
            continue
        池[k] = 池参数(gun, ammo, 护甲, dist, 无甲)
    return 无甲, 池


_组合类别 = ("头", "胸", "腹", "四肢")     # 手/腿部位倍率恒等(89 枪全同), 合并为四肢展示


def 击杀组合(无甲, 池, 身甲, 头甲, 上限=10):
    """枚举 ≤上限 次命中即可击杀的最小击杀组合(忽略空枪与命中次序)。

    组合按 头/胸/腹/四肢 计数(手与腿倍率恒等故合并); 同一组合取"存在一种破甲次序
    可击杀"(每护甲池按最优分配: 肉伤最高的命中置于破甲发/破甲后满伤位)。
    返回长度=上限 的字符串列表(BTK1..BTK上限), 该档无可击杀组合时为 '无'。"""
    代表 = {"头": "头", "胸": "胸", "腹": "腹", "四肢": "手"}
    池别, 完, 未, 破 = [], [], [], []
    for p in _组合类别:
        cd = _部位码[代表[p]]
        k = 甲键(代表[p], 身甲, 头甲)
        池别.append(k)
        完.append(无甲[cd])
        if k:
            未.append(池[k]["未破"][cd])
            破.append(池[k]["破发"][cd])
        else:
            未.append(无甲[cd])
            破.append(无甲[cd])
    b池 = {k: (池[k]["b"] if k in 池 else 0) for k in (1, 2)}

    def 肉伤(n):
        """组合 n=(头,胸,腹,四肢) 的最优次序总肉伤。"""
        s = 0.0
        for k in (0, 1, 2):
            池内 = [i for i in range(4) if 池别[i] == k and n[i]]
            if not 池内:
                continue
            if k == 0:                       # 无甲池: 不受破甲影响
                s += sum(完[i] * n[i] for i in 池内)
                continue
            命中 = []
            for i in 池内:
                命中 += [(完[i], 未[i], 破[i])] * n[i]
            m = len(命中)
            if m < b池[k]:                   # 未破甲, 全程穿透比例结算
                s += sum(t[1] for t in 命中)
            else:                            # 破甲发 + 破甲后满伤
                命中.sort(key=lambda t: -t[0])
                满n = m - b池[k]
                s += sum(命中[i][0] for i in range(满n))
                s += 命中[满n][2]
                s += sum(命中[i][1] for i in range(满n + 1, m))
        return s

    杀 = [set() for _ in range(上限 + 2)]
    结果 = []
    for k in range(1, 上限 + 1):
        本 = []
        for a in range(k + 1):
            for b_ in range(k - a + 1):
                for c in range(k - a - b_ + 1):
                    n = (a, b_, c, k - a - b_ - c)
                    if 肉伤(n) < PLAYER_HP - 1e-6:
                        continue
                    杀[k].add(n)
                    if k > 1 and any(        # 最小性: 任一 (k-1) 子组合可击杀则本组合非最小
                            n[i] and tuple(n[:i] + (n[i] - 1,) + n[i + 1:]) in 杀[k - 1]
                            for i in range(4)):
                        continue
                    本.append(n)
        本.sort()
        结果.append("，".join(
            "".join(f"{cnt}{名}" for 名, cnt in zip(_组合类别, n) if cnt) for n in 本) or "无")
    return 结果


def 统计分布(seq, 次数, 部位池, 池, 无甲, gun):
    """遍历预采样序列统计 BTK 直方图与 TTK 分位。hist 末位为未击杀(超 SHOT_CAP 发)。
    部位池[c]=该部位命中所属护甲池(2=头盔/1=身甲/0=无甲), 身头两池各自累计命中数独立破甲。"""
    hist = [0] * (SHOT_CAP + 2)
    ttk = []
    b切 = [0, 池[1]["b"] if 1 in 池 else SHOT_CAP + 1, 池[2]["b"] if 2 in 池 else SHOT_CAP + 1]
    未破 = [None, 池[1]["未破"] if 1 in 池 else None, 池[2]["未破"] if 2 in 池 else None]
    破发 = [None, 池[1]["破发"] if 1 in 池 else None, 池[2]["破发"] if 2 in 池 else None]
    for r in range(次数):
        base, hp, 甲中, n = r * SHOT_CAP, PLAYER_HP, [0, 0, 0], 0
        while n < SHOT_CAP:
            c = seq[base + n]
            n += 1
            if c:
                k = 部位池[c]
                if k:
                    甲中[k] += 1
                    if 甲中[k] < b切[k]:
                        hp -= 未破[k][c]
                    elif 甲中[k] == b切[k]:
                        hp -= 破发[k][c]
                    else:
                        hp -= 无甲[c]
                else:
                    hp -= 无甲[c]
                if hp <= 1e-9:
                    break
        if hp <= 1e-9:
            hist[n] += 1
            ttk.append(dc.射击用时(gun, n))
        else:
            hist[SHOT_CAP + 1] += 1
    ttk.sort()
    return {
        "hist": hist, "次数": 次数,
        "期望BTK": sum(k * v for k, v in enumerate(hist[:SHOT_CAP + 1])) / 次数,
        "ttk均值": sum(ttk) / len(ttk) if ttk else None,
        "ttk50": ttk[len(ttk) // 2] if ttk else None,
        "ttk90": ttk[int(len(ttk) * 0.9)] if ttk else None,
        "未杀率": hist[SHOT_CAP + 1] / 次数,
    }


def 弹药候选(ammos, 口径):
    """同口径、穿透等级 4/5 的弹(与 ttk_btk.py 口径一致)。"""
    return sorted((a for a in ammos.values() if a.口径 == 口径 and a.穿透等级 in (4, 5)),
                  key=lambda a: (a.穿透等级, a.名称))


def 批量概率(guns, ammos, armors, 距离列表, 模型, 次数, seed, 枪过滤=None):
    """全枪械 × 同口径4/5级弹 × 目标甲配对(身甲×头盔 18组 + 无甲 1组) × 各距离 的 BTK/TTK 概率分布。
    落点几何与枪弹/距离均无关, 故部位序列全局只预采样一次复用。身甲与头盔两池独立结算。"""
    序列 = 预采样部位(random.Random(seed + (0 if 模型 == "A" else 7777)), 次数, 模型)
    rows, 无弹枪, 无射速 = [], [], []
    for gun in guns.values():
        if 枪过滤 and not dc._norm(gun.名称).startswith(dc._norm(枪过滤)):
            continue
        if gun.基础伤害 is None:
            print(f"# 跳过 {gun.名称}: 缺少基础伤害", file=sys.stderr)
            continue
        if gun.射速 is None:
            无射速.append(gun.名称)
            continue
        cands = 弹药候选(ammos, gun.使用子弹)
        if not cands:
            无弹枪.append(gun.名称)
            continue
        for ammo in cands:
            for 身级, 身值, 头名 in 目标甲配对:
                if 身级 is None:                 # 无甲目标: 无身甲无头盔
                    身甲 = 头甲 = None
                else:
                    身甲 = dc.选甲行(armors, "身", 身级, 身值)
                    头甲 = dc._find(armors, 头名, "护甲")
                部位池 = [甲键(p, 身甲, 头甲) for p in _部位序]
                for 距离 in 距离列表:
                    无甲, 池 = 伤害表(gun, ammo, 身甲, 头甲, 距离)
                    击杀 = 击杀组合(无甲, 池, 身甲, 头甲)
                    res = 统计分布(序列, 次数, 部位池, 池, 无甲, gun)
                    身池, 头池 = 池.get(1), 池.get(2)
                    全胸枪, 全胸用时, _, _, _ = dc.击杀模拟(gun, ammo, "胸", 距离, 身甲)
                    rows.append({
                        "枪械": gun.名称, "类型": gun.类型, "口径": gun.使用子弹,
                        "弹药": ammo.名称, "弹级": ammo.穿透等级,
                        "甲级": 身级, "甲值": 身值,
                        "头盔": 头甲.名称 if 头甲 else None,
                        "头级": 头甲.等级 if 头甲 else None,
                        "头耐久": 头甲.护甲值 if 头甲 else None,
                        "距离": 距离, "模型": 模型,
                        "护腹": "是" if (身甲 and 身甲.防护腹部) else "否",
                        "身每发甲伤": round(身池["每发甲伤"], 2) if 身池 else 0.0,
                        "身破甲发数": 身池["b"] if 身池 else 0,
                        "头每发甲伤": round(头池["每发甲伤"], 2) if 头池 else 0.0,
                        "头破甲发数": 头池["b"] if 头池 else 0,
                        "全胸BTK": 全胸枪,
                        "全胸TTKms": None if 全胸用时 is None else round(全胸用时 * 1000, 1),
                        "击杀落点": 击杀,
                        **res,
                    })
    return rows, 无弹枪, 无射速


中文级 = "零一二三四五六"      # 目标甲/目标头列的等级中文数字


def 输出概率长表(rows, 路径):
    """每组合一行, 含完整 BTK 直方图列(百分比, 超列上限的合并为 BTK>n%, 未击杀单列)。
    列序: 识别列 → 甲/头(等级+耐久, 如 四甲100/四头35) → 距离 → 甲伤/破甲发数
    → 期望BTK/TTK均值 → BTK直方图 → 未击杀%/TTK_P50/P90 → 模型/次数(末列)。"""
    if not rows:
        return 路径, 0
    maxk = 1
    for r in rows:
        for k in range(SHOT_CAP, 0, -1):
            if r["hist"][k]:
                maxk = max(maxk, k)
                break
    ncol = max(1, min(maxk, 30))
    head = ["枪械", "类型", "口径", "弹药", "弹级", "目标甲", "目标头",
            "距离", "身每发甲伤", "身破甲发数", "头每发甲伤", "头破甲发数",
            "期望BTK", "TTK均值ms", "全胸BTK", "全胸TTKms"]
    head += [f"BTK{k}击杀落点" for k in range(1, 11)]   # 忽略空枪与次序的最小击杀组合
    head += [f"BTK{k}%" for k in range(1, ncol + 1)]
    if maxk > ncol:
        head.append(f"BTK>{ncol}%")
    head += ["未击杀%", "TTK_P50ms", "TTK_P90ms", "模型", "次数"]

    def f(v, nd=3):
        return "" if v is None else round(v, nd)

    临时 = 路径 + ".tmp"      # 先写临时文件; 目标被占用时在名称末尾追加数字另存
    with open(临时, "w", newline="", encoding="utf-8-sig") as f_:
        w = csv.writer(f_)
        w.writerow(head)
        for r in rows:
            n = r["次数"]
            row = [r["枪械"], r["类型"], r["口径"], r["弹药"], r["弹级"],
                   "无甲" if r["甲级"] is None else f'{中文级[r["甲级"]]}甲{r["甲值"]:.0f}',
                   "无头" if r["头级"] is None else f'{中文级[r["头级"]]}头{r["头耐久"]:.0f}',
                   r["距离"],
                   r["身每发甲伤"], r["身破甲发数"], r["头每发甲伤"], r["头破甲发数"],
                   f(r["期望BTK"], 3),
                   f(None if r["ttk均值"] is None else r["ttk均值"] * 1000, 1),
                   r["全胸BTK"], r["全胸TTKms"]]
            row += r["击杀落点"]
            row += [f(r["hist"][k] / n * 100) for k in range(1, ncol + 1)]
            if maxk > ncol:
                row.append(f(sum(r["hist"][ncol + 1:SHOT_CAP + 1]) / n * 100))
            row += [f(r["未杀率"] * 100),
                    f(None if r["ttk50"] is None else r["ttk50"] * 1000, 1),
                    f(None if r["ttk90"] is None else r["ttk90"] * 1000, 1),
                    r["模型"], n]
            w.writerow(row)
    try:
        os.replace(临时, 路径)
        return 路径, ncol
    except PermissionError:
        pass
    base, ext = os.path.splitext(路径)      # 目标被占用: 名称末尾追加数字另存, 不覆盖已有文件
    for i in range(1, 1000):
        新路径 = f"{base}{i}{ext}"
        if os.path.exists(新路径):
            continue
        try:
            os.replace(临时, 新路径)
        except PermissionError:
            continue
        print(f"# 目标文件被占用, 结果已另存为: {新路径}", file=sys.stderr)
        return 新路径, ncol
    print(f"# 目标文件被占用且备用名不可用, 结果暂存于: {临时}", file=sys.stderr)
    return 临时, ncol


def 解析距离(文本):
    """'10,20,30' → [10.0, 20.0, 30.0]; 兼容中文逗号; 空值回退 [20.0]。"""
    vals = []
    for piece in str(文本).replace("，", ",").split(","):
        piece = piece.strip()
        if piece:
            vals.append(float(piece))
    return vals or [20.0]


def main():
    ap = argparse.ArgumentParser(description="散布-部位-击杀 Monte Carlo 模拟")
    ap.add_argument("--枪", default=None, help="单枪模式必填(名称前缀匹配)")
    ap.add_argument("--弹", default=None, help="单枪模式必填(名称前缀匹配)")
    ap.add_argument("--甲", type=int, default=5, help="身甲等级, 默认5")
    ap.add_argument("--甲值", type=float, default=None, help="身甲耐久, 默认按armor.csv")
    ap.add_argument("--头甲", default=None,
                    help="头盔名称(前缀匹配), 如 DICH训练头盔/GT1战术头盔; 默认无头盔(头部按无甲结算)")
    ap.add_argument("--距离", default="20",
                    help="交战距离(m); 批量模式可用逗号分隔多档, 如 10,20,30,40,50,80")
    ap.add_argument("--部位", default=None, help="[调试用] 固定部位跑确定性击杀")
    ap.add_argument("--模型", choices=["A", "B", "both"], default="both")
    ap.add_argument("--次数", type=int, default=None, help="Monte Carlo 次数, 单组合默认10万/批量默认2万")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--回放", nargs="?", const="hit_replay.html", default=None,
                    metavar="文件.html", help="模拟一次击杀并导出逐发落点回放HTML")
    ap.add_argument("--批量", action="store_true",
                    help="全枪械×同口径4/5级弹×目标甲配对(4级甲配4级头35/48耐久、5级甲配5级头40/45/50或4级头35，另含无甲目标) 输出BTK/TTK概率分布长表")
    ap.add_argument("--输出", default="ttk_btk_概率长表.csv", help="批量模式输出CSV文件名")
    args = ap.parse_args()
    距离 = 解析距离(args.距离)[0]      # 单枪/回放模式只取首档

    guns, ammos, armors = dc.load_all()

    if args.批量:
        # 用户确认口径: 模型A(锚定首发) + long表含完整直方图 + armor.csv 通用身甲(含其护腹标记)
        模型 = args.模型 if args.模型 in ("A", "B") else "A"
        次数 = args.次数 if args.次数 else 20000
        距离列表 = 解析距离(args.距离)
        rows, 无弹枪, 无射速 = 批量概率(guns, ammos, armors, 距离列表, 模型, 次数, args.seed, args.枪)
        路径, ncol = 输出概率长表(rows, os.path.join(BASE_DIR, args.输出))
        ds = ",".join(f"{d:g}" for d in 距离列表)
        print(f"批量概率分布: 模型{模型} 次数{次数} 距离{ds}m (共{len(距离列表)}档)", file=sys.stderr)
        print(f"共 {len(set(r['枪械'] for r in rows))} 把枪 / {len(rows)} 数据行 "
              f"(直方图列 BTK1..BTK{ncol}) → {路径}", file=sys.stderr)
        if 无弹枪:
            print(f"# 无4/5级弹未计入: {'、'.join(无弹枪)}", file=sys.stderr)
        if 无射速:
            print(f"# 缺射速未计入: {'、'.join(无射速)}", file=sys.stderr)
        return

    if args.枪 is None or args.弹 is None:
        raise SystemExit("单枪模式需同时指定 --枪 与 --弹 (批量模式请加 --批量)")
    次数 = args.次数 if args.次数 else 100000
    gun = dc._find(guns, args.枪, "枪械")
    ammo = dc._find(ammos, args.弹, "弹药")
    if gun.射速 is None:
        raise SystemExit(f"枪械[{gun.名称}]缺少射速, 无法模拟 TTK")
    身甲 = dc.选甲行(armors, "身", args.甲, args.甲值)
    甲值 = 身甲.护甲值
    头甲 = dc._find(armors, args.头甲, "护甲") if args.头甲 else None

    print(f"枪械 {gun.名称} 基伤{gun.基础伤害} 甲伤{gun.护甲伤害} 射速{gun.射速:.0f}")
    头盔 = (f"头盔[{头甲.名称}] {头甲.等级}级耐久{头甲.护甲值:.0f}" if 头甲 else "无头盔")
    print(f"弹药 {ammo.名称} {ammo.穿透等级}级  目标 {args.甲}级身甲[{身甲.名称}]耐久{甲值}"
          f" 护腹={'是' if 身甲.防护腹部 else '否'} + {头盔}  @ {距离:g}m")

    print("\n部位命中概率 (由散布几何决定, 大样本估计):")
    for p, prob in sorted(部位概率().items(), key=lambda kv: -kv[1]):
        print(f"  {p}: {prob*100:.2f}%")

    if args.回放:
        路径, recs = 导出回放HTML(gun, ammo, 身甲, 头甲, 距离,
                                 args.模型 if args.模型 != "both" else "A",
                                 args.seed, os.path.join(BASE_DIR, args.回放))
        print(f"\n回放已导出: {路径} (共{len(recs)}发击杀), 浏览器打开后点击播放逐发查看")
        return

    models = ["A", "B"] if args.模型 == "both" else [args.模型]
    for m in models:
        res = 模拟批量(gun, ammo, 身甲, 头甲, 距离, m, 次数, args.seed)
        打印分布(f"模型{'A(相关:锚定最近落点, 连空2发回主散布)' if m == 'A' else 'B(独立同分布)'}", res)


if __name__ == "__main__":
    main()
