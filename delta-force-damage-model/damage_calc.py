#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三角洲行动 伤害数值计算程序

数据文件（与本脚本同目录，UTF-8-SIG 编码，可用 Excel 直接编辑）:
    guns.csv   枪械: 基础伤害/护甲伤害/射速/各部位倍率/距离衰减
    ammo.csv   子弹: 穿透等级/基础伤害比例/甲伤系数/各部位倍率/对各级护甲的甲伤倍率
    armor.csv  护甲: 名称/部位(身|头)/等级/护甲值

核心公式
--------
单发生命值伤害 = 枪械部位倍率 × 子弹部位倍率 × 枪械基础伤害 × 距离系数
               × 子弹基础伤害比例 × 护甲穿透比例
单发护甲值伤害 = 枪械护甲伤害 × 距离系数 × 甲伤倍率 × 子弹甲伤系数
               （甲伤与受击部位无关，不乘部位倍率；子弹基础伤害比例只参与肉伤，
                 不参与甲伤，甲伤只受甲伤系数影响——游戏内实测修正）

护甲穿透比例: 甲级>弹级→0 ; 甲级=弹级→0.5 ; 甲级+1=弹级→0.75 ; 其余→1
甲伤倍率: 子弹穿透等级对目标护甲等级的甲伤衰减水平（ammo.csv 中逐级给出）

破甲溢出规则（拆解文）: 单发理论甲伤超过当前护甲值时, 实际甲伤=当前护甲值,
溢出占比部分按无护甲（穿透比例=100%）结算生命值伤害:
    肉伤 = 无甲肉伤 × [占比×穿透比例 + (1-占比)] ,  占比 = 实际甲伤/理论甲伤

用法
----
  python damage_calc.py 列表 枪械|弹药|护甲
  python damage_calc.py 单发 --枪 AS Val --弹 5级弹 --甲 5 --部位 胸 --距离 20
  python damage_calc.py 击杀 --枪 AS Val --弹 5级弹 --甲 5 --甲值 105 --明细
  python damage_calc.py 矩阵 --距离 20 --部位 胸 > ttk矩阵.csv
"""
import argparse
import csv
import os
import sys
from dataclasses import dataclass, replace

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GUNS_CSV = os.path.join(BASE_DIR, "guns.csv")
AMMO_CSV = os.path.join(BASE_DIR, "ammo1.csv")
ARMOR_CSV = os.path.join(BASE_DIR, "armor.csv")

PARTS = ["头", "胸", "腹", "手", "腿", "肩"]
PLAYER_HP = 100.0


# ---------------------------------------------------------------- 数据模型
@dataclass
class Gun:
    名称: str
    类型: str
    使用子弹: str        # 口径, 如 5.56x45
    基础伤害: float
    护甲伤害: float
    射速: float          # 可为 None
    部位倍率: dict       # {部位: 系数}
    部位覆盖: set        # 弹药绝对覆盖的部位(忽略枪械倍率)
    部位相对: dict       # ×开头的部位: 相对系数, 乘在枪械倍率上
    距离衰减: list       # [(起始距离, 系数), ...] 升序
    备注: str = ""


@dataclass
class Ammo:
    名称: str
    口径: str
    穿透等级: int
    基础伤害比例: float
    甲伤系数: float
    部位倍率: dict       # {部位: 系数}
    部位覆盖: set        # =开头的部位: 倍率为绝对值, 直接覆盖枪械倍率
    部位相对: dict       # ×开头的部位: 相对系数, 乘在枪械倍率上
    甲伤倍率: dict       # {护甲等级1-6: 系数}
    备注: str = ""


@dataclass
class Armor:
    名称: str
    部位: str            # 身 / 头
    等级: int
    护甲值: float
    防护腹部: int        # 1=防护
    防护肩部: int        # 1=防护
    备注: str = ""


# ---------------------------------------------------------------- 读取
def _norm(s):
    return str(s).replace(" ", "").replace("\u3000", "").lower()


def _cell(row, key):
    return (row.get(key) or "").strip()


def _num(row, key, name, required=True, default=None):
    v = _cell(row, key)
    if v == "":
        if required:
            raise ValueError(f"[{name}] 缺少数值字段: {key}")
        return default
    return float(v)


def _part_mults(row, name):
    """解析头/胸/腹/手/腿/肩倍率列。
    =开头 → 绝对覆盖(忽略枪械倍率)；×或*开头 → 相对系数(乘在枪械倍率上)。
    返回 (倍率dict, 绝对覆盖集合, 相对系数dict)"""
    out, 覆盖, 相对 = {}, set(), {}
    for p in PARTS:
        v = _cell(row, p + "倍率")
        if v.startswith("="):
            覆盖.add(p)
            v = v[1:]
        elif v.startswith(("×", "*")):
            相对[p] = float(v[1:])
            out[p] = 相对[p]
            continue
        if v == "":
            raise ValueError(f"[{name}] 缺少字段: {p}倍率")
        out[p] = float(v)
    return out, 覆盖, 相对


def parse_falloff(text, name):
    """'0:1;35:0.9;50:0.8' -> [(0,1.0),(35,0.9),(50,0.8)]"""
    segs = []
    for piece in (text or "").split(";"):
        piece = piece.strip()
        if not piece:
            continue
        d, _, f = piece.partition(":")
        segs.append((float(d), float(f)))
    if not segs:
        raise ValueError(f"[{name}] 距离衰减格式错误, 应为 0:1;35:0.9;50:0.8")
    segs.sort(key=lambda x: x[0])
    return segs


def load_guns(path=GUNS_CSV):
    guns = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = _cell(row, "名称")
            if not name:
                continue
            pm, 覆盖, 相对 = _part_mults(row, name)
            guns[_norm(name)] = Gun(
                名称=name,
                类型=_cell(row, "类型"),
                使用子弹=_cell(row, "使用子弹"),
                基础伤害=_num(row, "基础伤害", name, required=False),
                护甲伤害=_num(row, "护甲伤害", name, required=False),
                射速=_num(row, "射速", name, required=False),
                部位倍率=pm,
                部位覆盖=覆盖,
                部位相对=相对,
                距离衰减=parse_falloff(_cell(row, "距离衰减"), name),
                备注=_cell(row, "备注"),
            )
    return guns


def load_ammos(path=AMMO_CSV):
    ammos = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = _cell(row, "名称")
            if not name:
                continue
            pm, 覆盖, 相对 = _part_mults(row, name)
            甲伤倍率 = {}
            for lv in range(1, 7):
                甲伤倍率[lv] = _num(row, f"甲伤倍率_{lv}甲", name)
            ammos[_norm(name)] = Ammo(
                名称=name,
                口径=_cell(row, "口径"),
                穿透等级=int(_num(row, "穿透等级", name)),
                基础伤害比例=_num(row, "基础伤害比例", name, default=1.0),
                甲伤系数=_num(row, "甲伤系数", name, default=1.0),
                部位倍率=pm,
                部位覆盖=覆盖,
                部位相对=相对,
                甲伤倍率=甲伤倍率,
                备注=_cell(row, "备注"),
            )
    return ammos


def load_armors(path=ARMOR_CSV):
    armors = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = _cell(row, "名称")
            if not name:
                continue
            armors[_norm(name)] = Armor(
                名称=name,
                部位=_cell(row, "部位"),
                等级=int(_num(row, "等级", name)),
                护甲值=_num(row, "护甲值", name),
                防护腹部=int(_num(row, "防护腹部", name, required=False, default=0)),
                防护肩部=int(_num(row, "防护肩部", name, required=False, default=0)),
                备注=_cell(row, "备注"),
            )
    return armors


def load_all():
    return load_guns(), load_ammos(), load_armors()


def _find(table, key, kind):
    k = _norm(key)
    obj = table.get(k)
    if obj is None:
        hits = [o for n, o in table.items() if n.startswith(k)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise SystemExit(f"{kind}[{key}] 匹配到多条，请写全名: " + "、".join(o.名称 for o in hits))
        raise SystemExit(f"未找到{kind}[{key}]，可用: " + "、".join(o.名称 for o in table.values()))
    return obj


def 选甲行(armors, 部位, 等级, 甲值=None):
    for o in armors.values():
        if o.部位 == 部位 and o.等级 == 等级:
            if 甲值 is not None:
                return replace(o, 护甲值=甲值, 名称=f"{o.名称}(自定义甲值)")
            return o
    raise SystemExit(f"armor.csv 中未找到 {等级}级{部位}甲")


# ---------------------------------------------------------------- 核心计算
def 距离系数(gun, dist):
    factor = gun.距离衰减[0][1]
    for d, f in gun.距离衰减:
        if dist >= d:
            factor = f
        else:
            break
    return factor


def 穿透比例(弹级, 甲级):
    diff = 弹级 - 甲级
    if diff < 0:
        return 0.0     # 甲级 > 弹级
    if diff == 0:
        return 0.5
    if diff == 1:
        return 0.75
    return 1.0         # 甲级+1 < 弹级


def 单发结算(gun, ammo, part, dist, 甲级=None, 当前甲值=0.0):
    """返回 (生命值伤害, 实际护甲伤害, 明细dict)"""
    if gun.基础伤害 is None:
        raise SystemExit(f"枪械[{gun.名称}]缺少基础伤害, 请先补全 guns.csv")
    枪倍 = gun.部位倍率[part]
    弹倍 = ammo.部位倍率[part]
    距系 = 距离系数(gun, dist)
    覆盖 = part in ammo.部位覆盖
    相对 = ammo.部位相对.get(part)
    if 覆盖:
        部位倍率 = 弹倍
    elif 相对 is not None:
        部位倍率 = 枪倍 * 相对
    else:
        部位倍率 = 枪倍 * 弹倍
    无甲肉伤 = gun.基础伤害 * 部位倍率 * 距系 * ammo.基础伤害比例
    明细 = {
        "枪械部位倍率": 枪倍, "子弹部位倍率": 弹倍, "部位覆盖": 覆盖, "部位相对": 相对, "距离系数": 距系,
        "基础伤害比例": ammo.基础伤害比例, "护甲穿透比例": 1.0,
        "甲伤倍率": None, "理论甲伤": None, "实际甲伤": None,
    }
    if 甲级 is None or 当前甲值 <= 0:
        return 无甲肉伤, 0.0, 明细
    if gun.护甲伤害 is None:
        raise SystemExit(f"枪械[{gun.名称}]缺少护甲伤害, 请先补全 guns.csv")
    穿比 = 穿透比例(ammo.穿透等级, 甲级)
    理论甲伤 = (gun.护甲伤害 * 距系
                * ammo.甲伤倍率[甲级] * ammo.甲伤系数)
    实际甲伤 = min(当前甲值, 理论甲伤)
    占比 = 实际甲伤 / 理论甲伤
    肉伤 = 无甲肉伤 * (占比 * 穿比 + (1 - 占比))
    明细.update({"护甲穿透比例": 穿比, "甲伤倍率": ammo.甲伤倍率[甲级],
                 "理论甲伤": 理论甲伤, "实际甲伤": 实际甲伤})
    return 肉伤, 实际甲伤, 明细


# 特殊射速曲线: 枪械名 -> (前段射速, 前段间隔数, 后段射速)
# 爆发SVCH 爆发枪机: 第1→2、2→3、3→4 发均按 700rpm, 第4→5 发起严格 600rpm
特殊射速 = {
    "爆发svch": (700.0, 3, 600.0),
}

# 连发(分段式)射速: 枪械名 -> (组内发数, 组内间隔秒)
# 组周期由标称射速决定 = 组内发数 × 60/标称射速; 组内间隔为实测值;
# 组末发→下一组首发 = 组周期 − (组内发数−1) × 组内间隔
连发射速 = {
    "mk4": (3, 0.0500),          # 三连发: 组周期 3×60/793 ≈ 227.0ms, 组间 127.0ms
    "击剑手mk4": (3, 0.0500),     # 三连发: 同上(同射速793)
    "m16a4": (3, 0.0750),        # 三连发: 组内间隔暂定待实测, 组周期 3×60/672 ≈ 267.9ms
    "刺客asval": (4, 0.0707),     # 四连发: 组周期 4×60/680 ≈ 352.9ms(≈353ms), 组间 140.9ms
}


def 射击用时(gun, 发数):
    """打出 发数 发(共 发数-1 个射击间隔)的累计用时(秒)。
    等间隔默认 60/射速; 特殊射速(爆发枪机)与连发(分组)曲线优先。"""
    if 发数 <= 1:
        return 0.0
    key = _norm(gun.名称)
    prof = 特殊射速.get(key)
    if prof:
        快, 快段数, 慢 = prof
        快段 = min(发数 - 1, 快段数)
        return 快段 * 60.0 / 快 + (发数 - 1 - 快段) * 60.0 / 慢
    burst = 连发射速.get(key)
    if burst:
        组发数, 组内间隔 = burst
        组周期 = 组发数 * 60.0 / gun.射速
        整组, 余 = divmod(发数 - 1, 组发数)
        return 整组 * 组周期 + 余 * 组内间隔
    return (发数 - 1) * 60.0 / gun.射速


def 部位对应护甲(part, 身甲=None, 头盔=None):
    """返回命中部位实际生效的护甲。腹/肩需身甲声明防护，手/腿无护甲。"""
    if part == "头":
        return 头盔
    if part == "胸":
        return 身甲
    if part == "腹" and 身甲 is not None and 身甲.防护腹部:
        return 身甲
    if part == "肩" and 身甲 is not None and 身甲.防护肩部:
        return 身甲
    return None          # 腹/肩未防护 或 手/腿 无护甲


def 击杀模拟(gun, ammo, part, dist, 护甲=None, 玩家生命=PLAYER_HP, 上限=600):
    """护甲为击中部位对应的护甲(Armor或None)。返回 (枪数, 用时s|None, 剩余生命, 剩余甲值, 逐枪记录)"""
    甲值 = 护甲.护甲值 if 护甲 else 0.0
    甲级 = 护甲.等级 if 护甲 else None
    hp, shots, 记录 = 玩家生命, 0, []
    while hp > 1e-9 and shots < 上限:
        shots += 1
        肉伤, 甲伤, _ = 单发结算(gun, ammo, part, dist, 甲级, 甲值)
        前 = 甲值
        甲值 = max(0.0, 甲值 - 甲伤)
        hp -= 肉伤
        记录.append((shots, 肉伤, 甲伤, 前, 甲值, hp))
    用时 = 射击用时(gun, shots) if (gun.射速 and shots > 1) else (0.0 if shots == 1 else None)
    return shots, 用时, hp, 甲值, 记录


def fmt(x):
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


# ---------------------------------------------------------------- 命令
def _枪械概要(g):
    射速 = fmt(g.射速) if g.射速 else "待补"
    return f"{g.名称}({g.类型}) 基伤{fmt(g.基础伤害) if g.基础伤害 else '待补'} 甲伤{fmt(g.护甲伤害) if g.护甲伤害 else '待补'} 射速{射速}"


def cmd_列表(a):
    if a.类别 == "枪械":
        guns, _, _ = load_all()
        for g in guns.values():
            衰减 = " → ".join(f"{fmt(d)}m:{fmt(f)}" for d, f in g.距离衰减)
            倍率 = " ".join(f"{p}{fmt(g.部位倍率[p])}" for p in PARTS)
            print(f"{_枪械概要(g)}\n    部位倍率: {倍率} | 距离衰减: {衰减}"
                  + (f"\n    备注: {g.备注}" if g.备注 else ""))
    elif a.类别 == "弹药":
        _, ammos, _ = load_all()
        for m in ammos.values():
            倍率 = " ".join(f"{p}{fmt(m.部位倍率[p])}" for p in PARTS if m.部位倍率[p] != 1)
            甲伤 = "/".join(fmt(m.甲伤倍率[lv]) for lv in range(1, 7))
            print(f"{m.名称} 口径{m.口径 or '?'} 穿透{m.穿透等级}级 基伤比例{fmt(m.基础伤害比例)} 甲伤系数{fmt(m.甲伤系数)}"
                  f"\n    甲伤倍率(1-6甲): {甲伤}"
                  + (f" | 特殊部位倍率: {倍率}" if 倍率 else "")
                  + (f"\n    备注: {m.备注}" if m.备注 else ""))
    else:
        _, _, armors = load_all()
        for r in armors.values():
            防护 = ""
            if r.部位 == "身":
                ps = ["胸"] + (["腹"] if r.防护腹部 else []) + (["肩"] if r.防护肩部 else [])
                防护 = f" 防护部位:{'+'.join(ps)} |"
            print(f"{r.名称} [{r.部位}甲 {r.等级}级] 护甲值{fmt(r.护甲值)}{防护}"
                  + (f" {r.备注}" if r.备注 else ""))


def cmd_单发(a):
    guns, ammos, armors = load_all()
    gun = _find(guns, a.枪, "枪械")
    ammo = _find(ammos, a.弹, "弹药")
    护甲 = None
    if a.甲 > 0:
        行 = 选甲行(armors, a.甲位, a.甲, a.甲值)
        护甲 = 部位对应护甲(a.部位, 行, 行)
        if 护甲 is None:
            print(f"# 提示: {行.名称}不防护部位[{a.部位}]，按无甲结算", file=sys.stderr)
    肉伤, 甲伤, d = 单发结算(gun, ammo, a.部位, a.距离,
                          护甲.等级 if 护甲 else None,
                          护甲.护甲值 if 护甲 else 0)
    print("── 单发伤害明细 " + "─" * 40)
    print(f"枪械  {_枪械概要(gun)}")
    print(f"弹药  {ammo.名称} 穿透{ammo.穿透等级}级 基伤比例{fmt(d['基础伤害比例'])} 甲伤系数{fmt(ammo.甲伤系数)}")
    print(f"部位  {a.部位} | 距离 {fmt(a.距离)}m → 距离系数{fmt(d['距离系数'])}")
    if d["部位覆盖"]:
        print(f"部位倍率 = 弹药绝对覆盖 {fmt(d['子弹部位倍率'])}（忽略枪械部位倍率）")
    elif d["部位相对"] is not None:
        print(f"部位倍率 = 枪{fmt(d['枪械部位倍率'])} × 弹相对系数{fmt(d['部位相对'])} = {fmt(d['枪械部位倍率'] * d['部位相对'])}")
    else:
        print(f"部位倍率 = 枪{fmt(d['枪械部位倍率'])} × 弹{fmt(d['子弹部位倍率'])} = {fmt(d['枪械部位倍率'] * d['子弹部位倍率'])}")
    if 护甲:
        print(f"护甲  {护甲.名称} [{护甲.等级}级 护甲值{fmt(护甲.护甲值)}]")
        print(f"穿透比例(弹{ammo.穿透等级} vs 甲{护甲.等级}) = {fmt(d['护甲穿透比例'])}")
        print(f"甲伤倍率({ammo.穿透等级}级弹对{护甲.等级}级甲) = {fmt(d['甲伤倍率'])}")
    else:
        print("护甲  无（该部位无护甲，穿透比例=1）")
    print("─" * 54)
    print(f"单发生命值伤害 = {fmt(肉伤)}")
    if 护甲:
        print(f"单发护甲值伤害(理论) = {fmt(d['理论甲伤'])}  (若超出当前护甲值则实际=护甲值, 溢出按100%肉伤结算)")


def cmd_击杀(a):
    guns, ammos, armors = load_all()
    gun = _find(guns, a.枪, "枪械")
    ammo = _find(ammos, a.弹, "弹药")
    护甲 = None
    if a.甲 > 0:
        行 = 选甲行(armors, "头" if a.部位 == "头" else "身", a.甲, a.甲值)
        护甲 = 部位对应护甲(a.部位, 行, 行)
        if 护甲 is None:
            print(f"# 提示: {行.名称}不防护部位[{a.部位}]，按无甲结算", file=sys.stderr)
    shots, 用时, hp, 甲剩, 记录 = 击杀模拟(gun, ammo, a.部位, a.距离, 护甲, a.生命)
    print(f"{gun.名称} + {ammo.名称}(穿透{ammo.穿透等级}) vs "
          + (f"{护甲.名称}[{护甲.等级}级/{fmt(护甲.护甲值)}]" if 护甲 else "无甲")
          + f" @{fmt(a.距离)}m 部位{a.部位}")
    print(f"击杀枪数: {shots} | 用时: {(fmt(用时) + 's') if 用时 is not None else '-'}"
          f" | 剩余生命: {fmt(hp)} | 剩余护甲: {fmt(甲剩)}")
    if a.明细:
        print("── 逐枪记录 ──")
        for i, 肉伤, 甲伤, 甲前, 甲后, hp后 in 记录:
            print(f"  #{i:<3} 肉伤{fmt(肉伤):>7}  甲伤{fmt(甲伤):>7}"
                  f"  甲值{fmt(甲前):>6}→{fmt(甲后):<6}  生命剩余{fmt(hp后)}")


def cmd_矩阵(a):
    guns, ammos, armors = load_all()
    w = csv.writer(sys.stdout)
    w.writerow(["枪械", "子弹", "子弹穿透等级", "护甲等级", "护甲值", "部位", "距离",
                "穿透比例", "单发肉伤(满甲)", "单发甲伤", "击杀枪数", "击杀时间s"])
    甲缓存 = {}
    for lv in range(1, 7):
        try:
            甲缓存[lv] = 选甲行(armors, a.甲位, lv)
        except SystemExit:
            pass
    for g in guns.values():
        if g.基础伤害 is None:
            print(f"# 跳过 {g.名称}: 缺少基础伤害", file=sys.stderr)
            continue
        for m in ammos.values():
            for lv, 护甲 in 甲缓存.items():
                肉伤, 甲伤, d = 单发结算(g, m, a.部位, a.距离, lv, 护甲.护甲值)
                shots, 用时, _, _, _ = 击杀模拟(g, m, a.部位, a.距离, 护甲)
                w.writerow([g.名称, m.名称, m.穿透等级, lv, fmt(护甲.护甲值), a.部位, fmt(a.距离),
                            fmt(d["护甲穿透比例"]), fmt(肉伤), fmt(甲伤), shots,
                            fmt(用时) if 用时 is not None else ""])


def _add_target_args(p):
    p.add_argument("--枪", required=True, help="枪械名称，见 guns.csv")
    p.add_argument("--弹", required=True, help="弹药名称，见 ammo.csv")
    p.add_argument("--甲", type=int, default=0, help="护甲等级1-6，0=无甲(默认0)")
    p.add_argument("--甲位", choices=["身", "头"], default="身", help="护甲部位(默认身)")
    p.add_argument("--甲值", type=float, default=None, help="自定义护甲值(耐久)，默认取 armor.csv 同等级第一行")
    p.add_argument("--部位", choices=PARTS, default="胸")
    p.add_argument("--距离", type=float, default=20)


def main():
    p = argparse.ArgumentParser(prog="damage_calc", description="三角洲行动 伤害数值计算程序")
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("列表", help="查看数据表内容")
    p1.add_argument("类别", choices=["枪械", "弹药", "护甲"])

    p2 = sub.add_parser("单发", help="计算单发生命值/护甲值伤害（含系数明细）")
    _add_target_args(p2)

    p3 = sub.add_parser("击杀", help="模拟击杀所需枪数与TTK（含破甲溢出规则）")
    _add_target_args(p3)
    p3.add_argument("--明细", action="store_true", help="打印逐枪记录")
    p3.add_argument("--生命", type=float, default=100)

    p4 = sub.add_parser("矩阵", help="批量输出 枪械×子弹×护甲等级 击杀表(CSV)")
    p4.add_argument("--距离", type=float, default=20)
    p4.add_argument("--部位", choices=PARTS, default="胸")
    p4.add_argument("--甲位", choices=["身", "头"], default="身")

    a = p.parse_args()
    {"列表": cmd_列表, "单发": cmd_单发, "击杀": cmd_击杀, "矩阵": cmd_矩阵}[a.cmd](a)


if __name__ == "__main__":
    main()
