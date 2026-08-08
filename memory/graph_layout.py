"""
知识图谱布局计算（对话零阻塞架构：增量布点 + 夜间全量精排）。

- 纯 Python 力导向布局（斥力 + 弹簧 + 中心重力），无 networkx / fa2 依赖，
  契合本地零依赖约束。注意：全量重算为 O(n²)×迭代轮数的 CPU 密集计算
  （实测 400 节点约 5 秒），**禁止在请求路径/事件循环上调用 compute_layout**，
  只允许夜间维护链经 asyncio.to_thread 在工作线程执行。
- 请求路径只调 place_missing 增量布点：新实体放共现邻居质心附近，孤立实体
  沿外环分布，老节点坐标不动，O(新增数) 毫秒级。
- 画布随节点数动态扩容（面积 ∝ 节点数，密度恒定），仅作初始化/重力参考，
  不做边界强制限制：节点坐标允许超出名义画布，由斥力/引力/中心重力自然
  收敛（强行夹紧会把外围节点压成贴边直线，视觉上极不自然）；前端 viewBox
  按节点包围盒自适应，无需与后端约定固定画布尺寸。
"""
from __future__ import annotations

import logging
import math
import random
import time
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.graph_layout")

BASE_W = 640         # 基准画布（≤60 节点），节点更多时按密度恒定原则扩容
BASE_H = 440
BASE_DENSITY_N = 60  # 基准画布容纳的节点数（密度基准）
ITER = 180
K = 170.0            # 理想边长
REPULSE = 24000.0    # 斥力系数
COLLIDE_PAD = 24     # 重叠消除的节点间留白（含标签空间）
COLLIDE_ITER = 60    # 重叠消除最大轮数
# 全量重算节点上限：超限只排 memory_count 最高的前 N 个，其余由增量布点补位，
# 避免未来数据膨胀后夜间链失控（纯 Python 耗时随节点数平方增长）
MAX_LAYOUT_NODES = 2000
# 批量写分块大小：控制单次持写锁时长，避免与事件循环线程的 db 写互斥长等
WRITE_CHUNK = 500


def _node_radius(memory_count: int, max_count: int) -> float:
    # 与前端 nodeRadius 同曲线：0.75 次幂比 √ 更拉开层级差异，范围 [5, 26]
    r = 5 + 21 * ((memory_count or 0) / max(1, max_count)) ** 0.75
    return min(26.0, r)


def _canvas(n: int) -> tuple[float, float, float]:
    """按节点数扩容画布：面积 ∝ 节点数，返回 (宽, 高, 扩容系数)。"""
    scale = max(1.0, math.sqrt(n / BASE_DENSITY_N))
    return BASE_W * scale, BASE_H * scale, scale


def place_missing(db) -> int:
    """增量布点（请求路径唯一入口）：只给缺坐标的新实体定位，老节点不动。

    有共现邻居的放邻居坐标质心附近加确定性随机偏移；孤立实体沿现有布局
    包围盒外环分布（画布尺寸动态，不能假设固定 640×440）；同时清理已删实体
    的孤儿坐标行。整体单事务执行，与 compute_layout 的原子替换事务互斥，
    不存在"读到精排中间态误判全表缺坐标"的竞态窗口。返回新布点数。
    """
    now = now_cst().isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM graph_layout WHERE entity_id NOT IN "
            "(SELECT entity_id FROM memory_entities)")
        missing = conn.execute(
            "SELECT e.entity_id FROM memory_entities e "
            "LEFT JOIN graph_layout g ON e.entity_id=g.entity_id "
            "WHERE g.entity_id IS NULL").fetchall()
        if not missing:
            return 0
        # 外环参考现有布局包围盒（无存量坐标时回退基准画布）
        b = conn.execute(
            "SELECT MIN(x) x0, MAX(x) x1, MIN(y) y0, MAX(y) y1, COUNT(*) c "
            "FROM graph_layout WHERE x IS NOT NULL").fetchone()
        if b["c"]:
            cx = (b["x0"] + b["x1"]) / 2
            cy = (b["y0"] + b["y1"]) / 2
            ring_r = max(b["x1"] - b["x0"], b["y1"] - b["y0"]) / 2 + 60
        else:
            cx, cy = BASE_W / 2, BASE_H / 2
            ring_r = min(BASE_W, BASE_H) / 2 - 40
        rows = []
        for ent in missing:
            eid = ent["entity_id"]
            rng = random.Random(eid)  # 按实体 ID 确定性随机，多次调用坐标稳定
            nbs = conn.execute(
                "SELECT g.x, g.y FROM memory_entity_links a "
                "JOIN memory_entity_links b "
                "ON a.memory_id=b.memory_id AND b.entity_id!=a.entity_id "
                "JOIN graph_layout g ON b.entity_id=g.entity_id "
                "WHERE a.entity_id=? AND g.x IS NOT NULL LIMIT 8",
                (eid,)).fetchall()
            if nbs:
                x = sum(r["x"] for r in nbs) / len(nbs) \
                    + (rng.random() - 0.5) * 60
                y = sum(r["y"] for r in nbs) / len(nbs) \
                    + (rng.random() - 0.5) * 60
            else:
                a = rng.random() * math.pi * 2
                x = cx + ring_r * math.cos(a)
                y = cy + ring_r * math.sin(a)
            rows.append((eid, round(x, 2), round(y, 2), now))
        conn.executemany(
            "INSERT OR REPLACE INTO graph_layout(entity_id,x,y,updated_at) "
            "VALUES(?,?,?,?)", rows)
    logger.info("图谱增量布点：%d 个新实体", len(rows))
    return len(rows)


def compute_layout(db) -> int:
    """全量力导向精排（仅夜间链，经 to_thread 执行），REPLACE 写入 graph_layout。

    超 MAX_LAYOUT_NODES 时只排 memory_count 最高的前 N 个并告警，剩余由
    place_missing 增量补位。返回参与排布的节点数。
    """
    t0 = time.perf_counter()
    total = db.query_one("SELECT COUNT(*) c FROM memory_entities")["c"]
    if total > MAX_LAYOUT_NODES:
        logger.warning("实体数 %d 超布局上限 %d，仅精排前 %d 个高频实体",
                       total, MAX_LAYOUT_NODES, MAX_LAYOUT_NODES)
    ents = db.query_all(
        "SELECT entity_id, memory_count FROM memory_entities "
        "ORDER BY memory_count DESC LIMIT ?", (MAX_LAYOUT_NODES,))
    if not ents:
        with db.transaction() as conn:
            conn.execute("DELETE FROM graph_layout")
        return 0
    edges = db.query_all(
        "SELECT a.entity_id src, b.entity_id tgt, COUNT(*) w "
        "FROM memory_entity_links a JOIN memory_entity_links b "
        "ON a.memory_id=b.memory_id AND a.entity_id < b.entity_id "
        "GROUP BY a.entity_id, b.entity_id")

    ids = [e["entity_id"] for e in ents]
    max_count = max((e["memory_count"] or 0) for e in ents) or 1
    n = len(ids)
    # 动态画布：面积随节点数线性扩容，保持节点密度恒定，避免数百节点
    # 挤在固定 640×440 里重叠成团；重力随扩容反比例削弱，否则大画布
    # 边缘重力过强会把节点重新压回中心
    gw, gh, scale = _canvas(n)
    gravity = 0.02 / scale
    cx, cy = gw / 2, gh / 2
    idx = {eid: i for i, eid in enumerate(ids)}

    # 环形初始化（确定性种子，保证多次重算布局稳定可复现）
    rng = random.Random(42)
    px, py, vx, vy, rad = [], [], [], [], []
    ring_r = 0 if n <= 1 else min(gw, gh) / 2 - 90
    for i, e in enumerate(ents):
        a = (i / max(1, n)) * math.pi * 2 - math.pi / 2
        jitter = (rng.random() - 0.5) * 2  # 打破完全对称，避免重叠力为 0
        px.append(cx + ring_r * math.cos(a) + jitter)
        py.append(cy + ring_r * math.sin(a) + jitter)
        vx.append(0.0)
        vy.append(0.0)
        rad.append(_node_radius(e["memory_count"] or 0, max_count))

    if n > 1:
        elist = [(idx[e["src"]], idx[e["tgt"]], e["w"] or 1)
                 for e in edges if e["src"] in idx and e["tgt"] in idx]
        for it in range(ITER):
            # 每轮主动让出 GIL：纯 Python 密集循环在工作线程中仍会与事件循环
            # 争抢 GIL 造成百毫秒级拖啡，短 sleep 给对话协程留出调度窗口
            time.sleep(0.002)
            cool = 1 - it / ITER
            # 节点间斥力
            for i in range(n):
                for j in range(i + 1, n):
                    dx = px[i] - px[j]
                    dy = py[i] - py[j]
                    d2 = dx * dx + dy * dy or 0.01
                    f = REPULSE / d2
                    d = math.sqrt(d2)
                    fx, fy = (dx / d) * f, (dy / d) * f
                    vx[i] += fx
                    vy[i] += fy
                    vx[j] -= fx
                    vy[j] -= fy
            # 边引力（弹簧）
            for s, t, w in elist:
                dx = px[t] - px[s]
                dy = py[t] - py[s]
                d = math.sqrt(dx * dx + dy * dy) or 0.01
                f = (d - K) / K * w
                fx, fy = (dx / d) * f * 8, (dy / d) * f * 8
                vx[s] += fx
                vy[s] += fy
                vx[t] -= fx
                vy[t] -= fy
            # 中心重力 + 阻尼（无硬边界：坐标允许出界，靠重力自然收敛，
            # 避免强制夹紧把外围节点压成贴边直线）
            for i in range(n):
                vx[i] += (cx - px[i]) * gravity
                vy[i] += (cy - py[i]) * gravity
                px[i] += max(-30, min(30, vx[i])) * cool
                py[i] += max(-30, min(30, vy[i])) * cool
                vx[i] *= 0.85
                vy[i] *= 0.85

        # 重叠消除：力导向只保证宏观结构，不保证节点不相交；按半径+留白
        # 逐对推开，直到无重叠或达轮数上限，保证每个节点周围有标签空间
        for _ in range(COLLIDE_ITER):
            time.sleep(0.002)   # 同主循环：让出 GIL 给事件循环协程
            moved = False
            for i in range(n):
                for j in range(i + 1, n):
                    dx = px[j] - px[i]
                    dy = py[j] - py[i]
                    d = math.sqrt(dx * dx + dy * dy) or 0.01
                    min_d = rad[i] + rad[j] + COLLIDE_PAD
                    if d < min_d:
                        push = (min_d - d) / 2
                        ux, uy = dx / d, dy / d
                        px[i] -= ux * push
                        py[i] -= uy * push
                        px[j] += ux * push
                        py[j] += uy * push
                        moved = True
            if not moved:
                break

    now = now_cst().isoformat(timespec="seconds")
    rows = [(ids[i], round(px[i], 2), round(py[i], 2), now) for i in range(n)]
    # 单事务原子替换：清表+写入对并发读者不可见中间态，避免精排期间
    # place_missing 误判全表缺坐标、用近似布点覆盖精排结果的竞态
    with db.transaction() as conn:
        conn.execute("DELETE FROM graph_layout")
        for i in range(0, len(rows), WRITE_CHUNK):
            conn.executemany(
                "INSERT INTO graph_layout(entity_id,x,y,updated_at) VALUES(?,?,?,?)",
                rows[i:i + WRITE_CHUNK])
    logger.info("知识图谱全量精排完成：%d 节点，耗时 %.2fs",
                n, time.perf_counter() - t0)
    return n
