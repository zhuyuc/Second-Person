// 三态焦点状态机（v3.0 §11.1）：Idle / Hover / Pinned，SVG 与 Sigma 引擎共用。
// 实际焦点 = pinned > hover；拖动中忽略 hover。邻居集合 Set 预计算，O(1) 查询。
import { ref, computed } from 'vue'

export function useGraphFocus(edges) {
    const hoveredId = ref(null)
    const pinnedId = ref(null)
    const draggingId = ref(null)

    const focusedId = computed(() => {
        if (draggingId.value) return null
        return pinnedId.value || hoveredId.value
    })

    const focusedNeighbors = computed(() => {
        const id = focusedId.value
        const set = new Set()
        if (!id) return set
        set.add(id)
        for (const e of (edges.value || [])) {
            if (e.source === id) set.add(e.target)
            else if (e.target === id) set.add(e.source)
        }
        return set
    })

    // idle / focused / neighbor / dimmed
    function nodeState(entityId) {
        if (!focusedId.value) return 'idle'
        if (entityId === focusedId.value) return 'focused'
        if (focusedNeighbors.value.has(entityId)) return 'neighbor'
        return 'dimmed'
    }

    // idle / active / neighbor / dimmed
    function edgeState(edge) {
        if (!focusedId.value) return 'idle'
        const active = edge.source === focusedId.value || edge.target === focusedId.value
        if (active) return 'active'
        const bothIn = focusedNeighbors.value.has(edge.source) &&
            focusedNeighbors.value.has(edge.target)
        return bothIn ? 'neighbor' : 'dimmed'
    }

    function clearPinned() { pinnedId.value = null }

    return {
        hoveredId, pinnedId, draggingId,
        focusedId, focusedNeighbors, nodeState, edgeState, clearPinned,
    }
}
