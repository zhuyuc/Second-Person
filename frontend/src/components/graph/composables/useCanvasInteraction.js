// Canvas 交互控制器：惯性缩放/平移 + Grid Hash 命中检测 + PointerEvent 统一事件
import { reactive, ref } from 'vue'

// ---- Grid Hash 空间索引 ----
class SpatialGrid {
  constructor(cellSize = 60) {
    this.cellSize = cellSize
    this.grid = new Map()
  }
  _key(cx, cy) {
    return `${cx},${cy}`
  }
  _cell(x, y) {
    return [Math.floor(x / this.cellSize), Math.floor(y / this.cellSize)]
  }
  rebuild(nodes) {
    this.grid.clear()
    for (const n of nodes) {
      const [cx, cy] = this._cell(n.x, n.y)
      const k = this._key(cx, cy)
      if (!this.grid.has(k)) this.grid.set(k, [])
      this.grid.get(k).push(n)
    }
  }
  query(x, y, radius) {
    const results = []
    const r = Math.ceil(radius / this.cellSize) + 1
    const [cx0, cy0] = this._cell(x, y)
    for (let dx = -r; dx <= r; dx++) {
      for (let dy = -r; dy <= r; dy++) {
        const k = this._key(cx0 + dx, cy0 + dy)
        const bucket = this.grid.get(k)
        if (!bucket) continue
        for (const n of bucket) {
          const d = Math.sqrt((n.x - x) ** 2 + (n.y - y) ** 2)
          if (d <= (n.r || 12) + 4) results.push({ node: n, dist: d })
        }
      }
    }
    results.sort((a, b) => a.dist - b.dist)
    return results.length ? results[0].node : null
  }
}

export function useCanvasInteraction(canvasRef, opts = {}) {
  const {
    zoomMin = 0.05,
    zoomMax = 8,
    zoomStep = 1.08,
    inertiaDamping = 0.92,
    inertiaMinSpeed = 0.5,
    onNodeHover = null,
    onNodeClick = null,
    onNodeDragStart = null,
    onNodeDrag = null,
    onNodeDragEnd = null,
    onBgClick = null,
    onCameraChange = null,
    getNodes = () => [],
  } = opts

  const camera = reactive({ x: 0, y: 0, zoom: 1 })
  const targetZoom = ref(1)
  const hoveredNode = ref(null)
  const spatialGrid = new SpatialGrid(60)

  // 状态
  let isDraggingNode = false
  let isPanning = false
  let dragNode = null
  let dragOffset = { x: 0, y: 0 }
  let panStart = { x: 0, y: 0, cx: 0, cy: 0 }
  let moved = false

  // 惯性
  let inertiaVx = 0,
    inertiaVy = 0
  let inertiaRaf = 0
  const velocityBuffer = []

  // 双指缩放
  const pointers = new Map()
  let pinchStartDist = 0
  let pinchStartZoom = 1

  function rebuildGrid() {
    spatialGrid.rebuild(getNodes())
  }

  // 屏幕坐标 → 世界坐标
  function screenToWorld(sx, sy) {
    const el = canvasRef.value
    if (!el) return { x: 0, y: 0 }
    const rect = el.getBoundingClientRect()
    return {
      x: (sx - rect.left - el.clientWidth / 2) / camera.zoom - camera.x,
      y: (sy - rect.top - el.clientHeight / 2) / camera.zoom - camera.y,
    }
  }

  function hitTest(sx, sy) {
    const world = screenToWorld(sx, sy)
    return spatialGrid.query(world.x, world.y, 30 / camera.zoom)
  }

  // ---- Zoom ----
  function onWheel(e) {
    e.preventDefault()
    const factor = e.deltaY > 0 ? 1 / zoomStep : zoomStep
    const newZoom = Math.max(zoomMin, Math.min(zoomMax, camera.zoom * factor))
    const el = canvasRef.value
    if (el) {
      const rect = el.getBoundingClientRect()
      const sx = e.clientX - rect.left - el.clientWidth / 2
      const sy = e.clientY - rect.top - el.clientHeight / 2
      const wx = sx / camera.zoom - camera.x
      const wy = sy / camera.zoom - camera.y
      camera.zoom = newZoom
      camera.x = sx / camera.zoom - wx
      camera.y = sy / camera.zoom - wy
    } else {
      camera.zoom = newZoom
    }
    targetZoom.value = camera.zoom
    if (onCameraChange) onCameraChange()
  }

  // ---- Pointer events ----
  function onPointerDown(e) {
    const el = canvasRef.value
    if (!el) return
    el.setPointerCapture(e.pointerId)
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })

    // 双指缩放起始
    if (pointers.size === 2) {
      const pts = [...pointers.values()]
      pinchStartDist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)
      pinchStartZoom = camera.zoom
      return
    }

    cancelInertia()
    moved = false

    const node = hitTest(e.clientX, e.clientY)
    if (node) {
      isDraggingNode = true
      dragNode = node
      const world = screenToWorld(e.clientX, e.clientY)
      dragOffset = { x: node.x - world.x, y: node.y - world.y }
      if (onNodeDragStart) onNodeDragStart(node)
    } else {
      isPanning = true
      panStart = { x: e.clientX, y: e.clientY, cx: camera.x, cy: camera.y }
      velocityBuffer.length = 0
    }
  }

  function onPointerMove(e) {
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })

    // 双指缩放
    if (pointers.size === 2) {
      const pts = [...pointers.values()]
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)
      if (pinchStartDist > 0) {
        const newZoom = Math.max(
          zoomMin,
          Math.min(zoomMax, pinchStartZoom * (dist / pinchStartDist))
        )
        camera.zoom = newZoom
        targetZoom.value = newZoom
        if (onCameraChange) onCameraChange()
      }
      return
    }

    if (isDraggingNode && dragNode) {
      moved = true
      const world = screenToWorld(e.clientX, e.clientY)
      dragNode.x = world.x + dragOffset.x
      dragNode.y = world.y + dragOffset.y
      if (dragNode.fx !== null && dragNode.fx !== undefined) {
        dragNode.fx = dragNode.x
        dragNode.fy = dragNode.y
      }
      if (onNodeDrag) onNodeDrag(dragNode)
    } else if (isPanning) {
      const dx = e.clientX - panStart.x
      const dy = e.clientY - panStart.y
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true
      camera.x = panStart.cx + dx / camera.zoom
      camera.y = panStart.cy + dy / camera.zoom
      if (onCameraChange) onCameraChange()
      velocityBuffer.push({ x: e.clientX, y: e.clientY, t: performance.now() })
      if (velocityBuffer.length > 5) velocityBuffer.shift()
    } else {
      // hover 检测
      const node = hitTest(e.clientX, e.clientY)
      if (node !== hoveredNode.value) {
        hoveredNode.value = node
        if (onNodeHover) onNodeHover(node)
        const el = canvasRef.value
        if (el) el.style.cursor = node ? 'pointer' : 'grab'
      }
    }
  }

  function onPointerUp(e) {
    pointers.delete(e.pointerId)
    const el = canvasRef.value
    if (el) {
      try {
        el.releasePointerCapture(e.pointerId)
      } catch {
        /* 指针已释放时忽略 */
      }
    }

    if (pointers.size < 2) {
      pinchStartDist = 0
    }

    if (isDraggingNode) {
      isDraggingNode = false
      if (!moved && dragNode && onNodeClick) onNodeClick(dragNode)
      if (onNodeDragEnd) onNodeDragEnd(dragNode, moved)
      dragNode = null
      return
    }
    if (isPanning) {
      isPanning = false
      if (!moved && onBgClick) {
        onBgClick()
        return
      }
      // 启动惯性
      startInertia()
    }
  }

  function onPointerCancel(e) {
    pointers.delete(e.pointerId)
    isDraggingNode = false
    isPanning = false
    dragNode = null
  }

  // 惯性平移
  function startInertia() {
    if (velocityBuffer.length < 2) return
    const last = velocityBuffer[velocityBuffer.length - 1]
    const prev = velocityBuffer[Math.max(0, velocityBuffer.length - 3)]
    const dt = last.t - prev.t || 16
    inertiaVx = (((last.x - prev.x) / dt) * 16) / camera.zoom
    inertiaVy = (((last.y - prev.y) / dt) * 16) / camera.zoom
    if (Math.hypot(inertiaVx, inertiaVy) < inertiaMinSpeed) return
    function step() {
      camera.x += inertiaVx
      camera.y += inertiaVy
      inertiaVx *= inertiaDamping
      inertiaVy *= inertiaDamping
      if (onCameraChange) onCameraChange()
      if (Math.hypot(inertiaVx, inertiaVy) > inertiaMinSpeed) {
        inertiaRaf = requestAnimationFrame(step)
      }
    }
    inertiaRaf = requestAnimationFrame(step)
  }

  function cancelInertia() {
    if (inertiaRaf) {
      cancelAnimationFrame(inertiaRaf)
      inertiaRaf = 0
    }
    inertiaVx = 0
    inertiaVy = 0
  }

  // 双击复位
  function onDblClick(e) {
    e.preventDefault()
    const node = hitTest(e.clientX, e.clientY)
    if (node) return // 双击节点不处理
    fitAll()
  }

  // 适配所有节点到视口
  function fitAll(animate = true) {
    const nodes = getNodes()
    if (!nodes.length) return
    const el = canvasRef.value
    if (!el) return
    let x0 = Infinity,
      y0 = Infinity,
      x1 = -Infinity,
      y1 = -Infinity
    for (const n of nodes) {
      x0 = Math.min(x0, n.x - (n.r || 12))
      y0 = Math.min(y0, n.y - (n.r || 12))
      x1 = Math.max(x1, n.x + (n.r || 12))
      y1 = Math.max(y1, n.y + (n.r || 12))
    }
    const pad = 40
    const cw = el.clientWidth,
      ch = el.clientHeight
    const contentW = x1 - x0 + pad * 2
    const contentH = y1 - y0 + pad * 2
    const zoom = Math.max(0.1, Math.min(2, Math.min(cw / contentW, ch / contentH)))
    const cx = (x0 + x1) / 2,
      cy = (y0 + y1) / 2

    if (animate) {
      animateTo(-cx, -cy, zoom)
    } else {
      camera.x = -cx
      camera.y = -cy
      camera.zoom = zoom
      targetZoom.value = zoom
    }
  }

  // 飞行到节点
  function flyTo(node, zoom) {
    animateTo(-node.x, -node.y, zoom || Math.max(camera.zoom, 1.2))
  }

  let animRaf = 0
  function animateTo(tx, ty, tz, duration = 400) {
    if (animRaf) cancelAnimationFrame(animRaf)
    const sx = camera.x,
      sy = camera.y,
      sz = camera.zoom
    const start = performance.now()
    function step(now) {
      let t = Math.min(1, (now - start) / duration)
      t = 1 - (1 - t) * (1 - t) // ease-out quadratic
      camera.x = sx + (tx - sx) * t
      camera.y = sy + (ty - sy) * t
      camera.zoom = sz + (tz - sz) * t
      targetZoom.value = camera.zoom
      if (onCameraChange) onCameraChange()
      if (t < 1) animRaf = requestAnimationFrame(step)
      else animRaf = 0
    }
    animRaf = requestAnimationFrame(step)
  }

  // 绑定/解绑事件
  function bindEvents() {
    const el = canvasRef.value
    if (!el) return
    el.addEventListener('wheel', onWheel, { passive: false })
    el.addEventListener('pointerdown', onPointerDown)
    el.addEventListener('pointermove', onPointerMove)
    el.addEventListener('pointerup', onPointerUp)
    el.addEventListener('pointercancel', onPointerCancel)
    el.addEventListener('dblclick', onDblClick)
    el.style.touchAction = 'none'
    el.style.cursor = 'grab'
  }

  function unbindEvents() {
    const el = canvasRef.value
    if (!el) return
    el.removeEventListener('wheel', onWheel)
    el.removeEventListener('pointerdown', onPointerDown)
    el.removeEventListener('pointermove', onPointerMove)
    el.removeEventListener('pointerup', onPointerUp)
    el.removeEventListener('pointercancel', onPointerCancel)
    el.removeEventListener('dblclick', onDblClick)
    cancelInertia()
    if (animRaf) cancelAnimationFrame(animRaf)
  }

  return {
    camera,
    hoveredNode,
    rebuildGrid,
    screenToWorld,
    hitTest,
    fitAll,
    flyTo,
    bindEvents,
    unbindEvents,
  }
}
