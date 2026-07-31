import { defineStore } from 'pinia'

let seq = 0
const DURATION = { success: 3000, warning: 5000, error: 8000, info: 4000 }
const MAX_VISIBLE = 3

export const useToast = defineStore('toast', {
    state: () => ({ items: [], _queue: [] }),
    actions: {
        push(type, message, traceId) {
            const id = ++seq
            const item = { id, type, message, traceId }
            // 超过 3 条：新条目入队列，不丢弃
            if (this.items.length >= MAX_VISIBLE) {
                this._queue.push(item)
                return
            }
            this.items.push(item)
            const d = DURATION[type] || 4000
            // error 需手动关闭，不自动消失
            if (type !== 'error') setTimeout(() => this.remove(id), d)
        },
        remove(id) {
            this.items = this.items.filter((i) => i.id !== id)
            // 队列补位
            if (this._queue.length) {
                const next = this._queue.shift()
                this.items.push(next)
                const d = DURATION[next.type] || 4000
                if (next.type !== 'error') setTimeout(() => this.remove(next.id), d)
            }
        },
    },
})
