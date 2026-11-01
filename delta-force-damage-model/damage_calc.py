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
import math
import os
import sys
from dataclasses import dataclass, replace

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GUNS_CSV = os.path.join(BASE_DIR, "guns.csv")
AMMO_CSV = os.path.join(BASE_DIR, "ammo.csv")
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
    弹速: float = None   # 弹丸初速 m/s (该行默认配置), 用于飞行时间
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
                弹速=_num(row, "弹速(m/s)", name, required=False),
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


def 飞行时间(gun, dist):
    """弹丸飞行时间(s) = 距离 / 初速。无初速数据时返回 0（不计弹速）。"""
    if not gun.弹速:
        return 0.0
    return float(dist) / float(gun.弹速)


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


def 序列结算(gun, ammo, parts, dist, 身甲=None, 头盔=None, 玩家生命=PLAYER_HP):
    """按命中顺序结算一组混合部位（可跨头/身两个护甲池各自独立损耗）。
    头→头盔池, 胸与受防护的腹/肩→身甲池, 手/腿→无甲; 破甲后该发的溢出部分按无甲 100% 结算。
    返回 (剩余生命, 剩余身甲, 剩余头盔)。"""
    甲值 = {"身": 身甲.护甲值 if 身甲 else 0.0, "头": 头盔.护甲值 if 头盔 else 0.0}
    hp = 玩家生命
    for p in parts:
        a = 部位对应护甲(p, 身甲, 头盔)
        if a is None:
            hp -= 单发结算(gun, ammo, p, dist)[0]
            continue
        肉伤, 甲伤, _ = 单发结算(gun, ammo, p, dist, a.等级, 甲值[a.部位])
        hp -= 肉伤
        甲值[a.部位] = max(0.0, 甲值[a.部位] - 甲伤)
    return hp, 甲值["身"], 甲值["头"]


def 防致死耐久(gun, ammo, parts, dist, 身甲等级, 头甲等级=None, 头甲值=0.0, 护腹=True, 护肩=False, cap=400.0, 迭代=50):
    """二分“身甲耐久”, 求恰好打光玩家生命的最大耐久 (恰好致死即临界)。
    返回 (耐久 | None=永不成立, 是否顶到 cap)。护腹/护肩按口径显式传入,
    不继承某个具体型号的标记; 头盔单独传入等级与耐久。"""

    def 剩余(D):
        身 = Armor(名称=f"{身甲等级}级身甲(临界)", 部位="身", 等级=身甲等级, 护甲值=D,
                  防护腹部=1 if 护腹 else 0, 防护肩部=1 if 护肩 else 0)
        头 = (Armor(名称=f"{头甲等级}级头盔(临界)", 部位="头", 等级=头甲等级, 护甲值=头甲值,
                    防护腹部=0, 防护肩部=0) if 头甲等级 else None)
        return 序列结算(gun, ammo, parts, dist, 身, 头)[0]

    if 剩余(0.0) > 0:
        return None, False
    if 剩余(cap) <= 0:
        return cap, True
    lo, hi = 0.0, cap
    for _ in range(迭代):
        mid = (lo + hi) / 2.0
        if 剩余(mid) <= 0:
            lo = mid
        else:
            hi = mid
    return lo, False


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


def 破甲血量(gun, ammo, dist, 甲级, 甲值, 部位="胸"):
    """连续命中该部位直至破甲，返回 (破甲发数, 破甲累计肉伤, 破甲后剩余血量, 明细)。"""
    if not 甲级 or not 甲值 or 甲值 <= 0:
        return 0, 0.0, PLAYER_HP, {"理论甲伤": 0.0, "穿透比例": 1.0}
    cur, 累计, n, 理论 = float(甲值), 0.0, 0, 0.0
    while cur > 1e-9 and n < 200:
        肉伤, 甲伤, d = 单发结算(gun, ammo, 部位, dist, 甲级, cur)
        理论 = d["理论甲伤"]
        n += 1
        cur = max(0.0, cur - 甲伤)
        累计 += 肉伤
    return n, 累计, max(0.0, PLAYER_HP - 累计), {
        "理论甲伤": 理论, "穿透比例": 穿透比例(ammo.穿透等级, 甲级)}


def _破甲致死发数(gun, ammo, dist, 甲级, 甲值, 部位):
    """破甲前若躯干连打已能致死，返回致死发数；否则返回 None。"""
    if not 甲级 or not 甲值 or 甲值 <= 0:
        return int(math.ceil(PLAYER_HP / max(1e-9, 单发结算(gun, ammo, 部位, dist)[0])))
    cur, 累计, n = float(甲值), 0.0, 0
    while 累计 < PLAYER_HP - 1e-9 and n < 200:
        肉伤, 甲伤, _ = 单发结算(gun, ammo, 部位, dist, 甲级, cur)
        n += 1
        cur = max(0.0, cur - 甲伤)
        累计 += 肉伤
    return n if 累计 >= PLAYER_HP - 1e-9 else None


def 破甲后组合(gun, ammo, dist, 甲级, 甲值, 上限=None, 部位="胸"):
    """先按该部位连打直至破甲，再在剩余血量上枚举 胸/腹/四肢 追加命中。
    返回最少总发数下的全部最小击杀组合；胸腹伤害相同时自动合并为“躯干”。"""
    b, 破甲肉, R, 明细 = 破甲血量(gun, ammo, dist, 甲级, 甲值, 部位)
    U胸 = 单发结算(gun, ammo, "胸", dist)[0]
    U腹 = 单发结算(gun, ammo, "腹", dist)[0]
    U肢 = min(单发结算(gun, ammo, "手", dist)[0], 单发结算(gun, ammo, "腿", dist)[0])
    合并 = abs(U胸 - U腹) < 1e-9
    结果 = {"破甲发数": b, "破甲肉伤": 破甲肉, "剩余血量": R, "合并胸腹": 合并,
            "U胸": U胸, "U腹": U腹, "U肢": U肢,
            "理论甲伤": 明细["理论甲伤"], "穿透比例": 明细["穿透比例"]}
    if R <= 1e-9:
        发数 = _破甲致死发数(gun, ammo, dist, 甲级, 甲值, 部位) or b
        结果.update({"追加最少发数": 0, "最少总发数": 发数, "最小组合": (), "已致死": True})
        return 结果
    cap = 上限
    if cap is None:
        cap = int(math.ceil(R / max(1e-9, min(U胸, U腹, U肢))) + 1)
    全部 = []
    for a in range(cap + 1):
        for bb in range(cap + 1 - a):
            for c in range(cap + 1 - a - bb):
                if a * U胸 + bb * U腹 + c * U肢 >= R - 1e-9:
                    全部.append((a + bb + c, a, bb, c))
    n = min(x[0] for x in 全部)
    最小 = sorted({(x[1], x[2], x[3]) for x in 全部 if x[0] == n})
    if 合并:
        最小 = sorted({(a + bb, 0, c) for a, bb, c in 最小})
    结果.update({"追加最少发数": n, "最少总发数": b + n, "最小组合": tuple(最小), "已致死": False})
    return 结果


def _组合文字(组合, 合并):
    a, bb, c = 组合
    段 = []
    if 合并:
        if a + bb:
            段.append(f"躯干{a + bb}")
    else:
        if a:
            段.append(f"胸{a}")
        if bb:
            段.append(f"腹{bb}")
    if c:
        段.append(f"四肢{c}")
    return "+".join(段) if 段 else "0"


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


def cmd_耐久(a):
    guns, ammos, _ = load_all()
    gun = _find(guns, a.枪, "枪械")
    ammo = _find(ammos, a.弹, "弹药")
    lo, hi = [float(x) for x in str(a.甲值范围).replace("～", "-").split("-")]
    if hi < lo:
        lo, hi = hi, lo
    点, d = [], lo
    while d <= hi + 1e-9:
        点.append(round(d, 6))
        d += a.步长
    结果 = [(D, 破甲后组合(gun, ammo, a.距离, a.甲, D, a.上限, a.部位)) for D in 点]
    首 = 结果[0][1]
    print(f"{gun.名称} + {ammo.名称}(穿透{ammo.穿透等级}) @{fmt(a.距离)}m，破甲攻击部位{a.部位}")
    print(f"甲{a.甲}: 每发理论甲伤{fmt(首['理论甲伤'])}，穿透比例{fmt(首['穿透比例'])}"
          f" | 单发无甲肉伤 胸{fmt(首['U胸'])} 腹{fmt(首['U腹'])} 四肢{fmt(首['U肢'])}"
          + ("（胸腹同伤，合并为躯干）" if 首["合并胸腹"] else ""))
    print("耐久区间 | 破甲发数 | 破甲累计肉伤 | 破甲后剩余 | 追加最少发数→最少总发数 | 该发数下的最小击杀组合")
    组 = []
    for D, r in 结果:
        键 = (r["破甲发数"], r["追加最少发数"], r["最小组合"], r["已致死"],
              r["最少总发数"] if r["已致死"] else 0)
        if 组 and 组[-1]["键"] == 键:
            组[-1]["止"] = D
            组[-1]["R止"] = r["剩余血量"]
        else:
            组.append({"键": 键, "起": D, "止": D, "样本": r,
                      "R起": r["剩余血量"], "R止": r["剩余血量"]})
    for 段 in 组:
        r = 段["样本"]
        起, 止 = 段["起"], 段["止"]
        区间 = fmt(起) if abs(止 - 起) < 1e-9 else f"{fmt(起)}–{fmt(止)}"
        if r["已致死"]:
            print(f" {区间} | {r['破甲发数']} | — | 破甲前已致死 | 全躯干"
                  f"{r['最少总发数']}发")
            continue
        R起, R止 = 段["R起"], 段["R止"]
        剩 = fmt(R起) if abs(R起 - R止) < 1e-9 else f"{fmt(min(R起, R止))}–{fmt(max(R起, R止))}"
        组合 = "、".join(_组合文字(c, r["合并胸腹"]) for c in r["最小组合"])
        print(f" {区间} | {r['破甲发数']} | {fmt(r['破甲肉伤'])} | {剩} "
              f"| {r['追加最少发数']}→{r['最少总发数']} | {组合}")


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

    p5 = sub.add_parser("耐久", help="按护甲耐久扫描破甲后血量与胸/腹/四肢击杀组合变化")
    p5.add_argument("--枪", required=True, help="枪械名称，见 guns.csv")
    p5.add_argument("--弹", required=True, help="弹药名称，见 ammo.csv")
    p5.add_argument("--甲", type=int, required=True, help="护甲等级1-6(决定穿透比例与甲伤)")
    p5.add_argument("--距离", type=float, default=10)
    p5.add_argument("--甲值范围", default="60-160", help="耐久扫描区间，如 60-160")
    p5.add_argument("--步长", type=float, default=1.0)
    p5.add_argument("--部位", choices=["胸", "腹"], default="胸", help="破甲攻击部位(默认胸)")
    p5.add_argument("--上限", type=int, default=None, help="追加发数上限，默认按剩余血量推算")

    a = p.parse_args()
    {"列表": cmd_列表, "单发": cmd_单发, "击杀": cmd_击杀, "矩阵": cmd_矩阵, "耐久": cmd_耐久}[a.cmd](a)


if __name__ == "__main__":
    main()
