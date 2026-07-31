// SVG 引擎节点拖动 + 点击识别（v3.0 §6.4 / §11.2）：会话内保留，不写回后端。
// 拖动中置 draggingId（禁用 hover 焦点）；松手时若未移动则视为点击，回调 onClick。
export function useDraggableNode(nodePos, focus, getSvgPoint, onClick) {
    let offset = { x: 0, y: 0 }
    let start = { x: 0, y: 0 }
    let curNode = null
    let moved = false

    function onMouseDown(evt, node) {
        evt.stopPropagation()
        curNode = node
        moved = false
        focus.draggingId.value = node.entity_id
        const p = getSvgPoint(evt)
        start = { x: p.x, y: p.y }
        offset = { x: node.x - p.x, y: node.y - p.y }
        window.addEventListener('mousemove', onMouseMove)
        window.addEventListener('mouseup', onMouseUp, { once: true })
    }

    function onMouseMove(evt) {
        if (!curNode) return
        const p = getSvgPoint(evt)
        if (Math.abs(p.x - start.x) > 3 || Math.abs(p.y - start.y) > 3) moved = true
        const node = nodePos.value.find(n => n.entity_id === curNode.entity_id)
        if (node) { node.x = p.x + offset.x; node.y = p.y + offset.y }
    }

    function onMouseUp() {
        window.removeEventListener('mousemove', onMouseMove)
        const node = curNode
        curNode = null
        focus.draggingId.value = null
        if (!moved && node && onClick) onClick(node)  // 未移动 = 点击
    }

    return { onMouseDown }
}
