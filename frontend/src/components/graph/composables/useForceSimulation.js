// 前端 Barnes-Hut 力仿真（Canvas 图谱引擎）
// 三种模式：冷启动（后端坐标微调）、扰动（拖拽弹性回弹）、扩展（邻居节点扩散）
import { ref } from 'vue'

// ---- QuadTree (Barnes-Hut) ----
class QTNode {
  constructor(x, y, w, h) {
    this.x = x
    this.y = y
    this.w = w
    this.h = h
    this.cx = 0
    this.cy = 0
    this.mass = 0
    this.body = null
    this.children = null // [NW, NE, SW, SE]
  }
  contains(px, py) {
    return px >= this.x && px < this.x + this.w && py >= this.y && py < this.y + this.h
  }
  subdivide() {
    const hw = this.w / 2,
      hh = this.h / 2
    this.children = [
      new QTNode(this.x, this.y, hw, hh),
      new QTNode(this.x + hw, this.y, hw, hh),
      new QTNode(this.x, this.y + hh, hw, hh),
      new QTNode(this.x + hw, this.y + hh, hw, hh),
    ]
  }
  insert(node) {
    if (!this.contains(node.x, node.y)) return
    if (this.mass === 0 && !this.children) {
      this.body = node
      this.mass = 1
      this.cx = node.x
      this.cy = node.y
      return
    }
    if (!this.children) {
      this.subdivide()
      if (this.body) {
        const b = this.body
        this.body = null
        for (const c of this.children) c.insert(b)
      }
    }
    for (const c of this.children) c.insert(node)
    this.mass++
    this.cx = (this.cx * (this.mass - 1) + node.x) / this.mass
    this.cy = (this.cy * (this.mass - 1) + node.y) / this.mass
  }
}

function buildQuadTree(nodes) {
  if (!nodes.length) return null
  let x0 = Infinity,
    y0 = Infinity,
    x1 = -Infinity,
    y1 = -Infinity
  for (const n of nodes) {
    x0 = Math.min(x0, n.x)
    y0 = Math.min(y0, n.y)
    x1 = Math.max(x1, n.x)
    y1 = Math.max(y1, n.y)
  }
  const pad = 50
  const root = new QTNode(
    x0 - pad,
    y0 - pad,
    Math.max(1, x1 - x0 + pad * 2),
    Math.max(1, y1 - y0 + pad * 2)
  )
  for (const n of nodes) root.insert(n)
  return root
}

// Barnes-Hut 斥力遍历
function repulsiveForce(node, qtNode, theta, repulse) {
  if (!qtNode || qtNode.mass === 0) return { fx: 0, fy: 0 }
  const dx = node.x - qtNode.cx,
    dy = node.y - qtNode.cy
  const distSq = dx * dx + dy * dy + 1
  if (!qtNode.children && qtNode.mass === 1) {
    if (qtNode.body === node) return { fx: 0, fy: 0 }
    const f = repulse / distSq
    const dist = Math.sqrt(distSq)
    return { fx: (dx / dist) * f, fy: (dy / dist) * f }
  }
  const s = qtNode.w
  if ((s * s) / distSq < theta * theta) {
    const f = (repulse * qtNode.mass) / distSq
    const dist = Math.sqrt(distSq)
    return { fx: (dx / dist) * f, fy: (dy / dist) * f }
  }
  if (qtNode.children) {
    let fx = 0,
      fy = 0
    for (const c of qtNode.children) {
      const r = repulsiveForce(node, c, theta, repulse)
      fx += r.fx
      fy += r.fy
    }
    return { fx, fy }
  }
  return { fx: 0, fy: 0 }
}

const DEFAULTS = {
  repulse: 18000,
  springLen: 160,
  springK: 0.006,
  gravity: 0.015,
  damping: 0.85,
  theta: 0.9,
  alphaDecay: 0.02,
  maxVelocity: 40,
}

export function useForceSimulation(nodesRef, edgesRef, opts = {}) {
  const cfg = { ...DEFAULTS, ...opts }
  const alpha = ref(0)
  const running = ref(false)
  const velocities = new Map()

  // 邻接表缓存
  let adjMap = new Map()
  function rebuildAdj() {
    adjMap = new Map()
    for (const e of edgesRef.value) {
      if (!adjMap.has(e.source)) adjMap.set(e.source, [])
      if (!adjMap.has(e.target)) adjMap.set(e.target, [])
      adjMap.get(e.source).push(e)
      adjMap.get(e.target).push(e)
    }
  }

  function getVel(id) {
    if (!velocities.has(id)) velocities.set(id, { vx: 0, vy: 0 })
    return velocities.get(id)
  }

  function tick() {
    if (alpha.value < 0.001) {
      running.value = false
      return false
    }
    const nodes = nodesRef.value
    if (!nodes.length) return false

    // 中心
    let sumX = 0,
      sumY = 0
    for (const n of nodes) {
      sumX += n.x
      sumY += n.y
    }
    const centerX = sumX / nodes.length,
      centerY = sumY / nodes.length

    // 构建四叉树
    const qt = buildQuadTree(nodes)

    // node id -> node 映射
    const nodeMap = new Map()
    for (const n of nodes) nodeMap.set(n.entity_id, n)

    for (const n of nodes) {
      if (n.fx !== null && n.fx !== undefined) {
        n.x = n.fx
        n.y = n.fy
        continue
      }

      let fx = 0,
        fy = 0

      // Barnes-Hut 斥力
      const rep = repulsiveForce(n, qt, cfg.theta, cfg.repulse)
      fx += rep.fx
      fy += rep.fy

      // 弹簧力
      const adj = adjMap.get(n.entity_id)
      if (adj) {
        for (const e of adj) {
          const otherId = e.source === n.entity_id ? e.target : e.source
          const other = nodeMap.get(otherId)
          if (!other) continue
          const dx = other.x - n.x,
            dy = other.y - n.y
          const dist = Math.sqrt(dx * dx + dy * dy) || 1
          const displacement = dist - cfg.springLen
          const force = cfg.springK * displacement * (e.weight || 1)
          fx += (dx / dist) * force
          fy += (dy / dist) * force
        }
      }

      // 中心引力
      fx += (centerX - n.x) * cfg.gravity
      fy += (centerY - n.y) * cfg.gravity

      // 速度更新 + 阻尼
      const vel = getVel(n.entity_id)
      vel.vx = (vel.vx + fx * alpha.value) * cfg.damping
      vel.vy = (vel.vy + fy * alpha.value) * cfg.damping

      // 限速
      const speed = Math.sqrt(vel.vx * vel.vx + vel.vy * vel.vy)
      if (speed > cfg.maxVelocity) {
        vel.vx = (vel.vx / speed) * cfg.maxVelocity
        vel.vy = (vel.vy / speed) * cfg.maxVelocity
      }

      n.x += vel.vx
      n.y += vel.vy
    }

    alpha.value *= 1 - cfg.alphaDecay
    return true
  }

  // 冷启动：后端坐标微调
  function coldStart() {
    rebuildAdj()
    alpha.value = 0.25
    running.value = true
  }

  // 扰动：拖拽后弹性回弹
  function reheat(a = 0.5) {
    rebuildAdj()
    alpha.value = Math.max(alpha.value, a)
    running.value = true
  }

  // 扩展：邻居节点扩散
  function expand() {
    rebuildAdj()
    alpha.value = 0.7
    running.value = true
  }

  // 立即停止
  function freeze() {
    alpha.value = 0
    running.value = false
  }

  // 固定/释放节点
  function fixNode(id, x, y) {
    const n = nodesRef.value.find((n) => n.entity_id === id)
    if (n) {
      n.fx = x
      n.fy = y
      n.x = x
      n.y = y
    }
  }
  function releaseNode(id) {
    const n = nodesRef.value.find((n) => n.entity_id === id)
    if (n) {
      delete n.fx
      delete n.fy
    }
    const vel = velocities.get(id)
    if (vel) {
      vel.vx = 0
      vel.vy = 0
    }
  }

  function onDataChange() {
    rebuildAdj()
    // 清理已不存在节点的速度缓存
    const ids = new Set(nodesRef.value.map((n) => n.entity_id))
    for (const k of velocities.keys()) {
      if (!ids.has(k)) velocities.delete(k)
    }
  }

  return {
    alpha,
    running,
    tick,
    coldStart,
    reheat,
    expand,
    freeze,
    fixNode,
    releaseNode,
    onDataChange,
  }
}
