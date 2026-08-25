// 会话列表共享状态：全局侧栏(SessionSidebar)与 ChatView 共用。
import { defineStore } from 'pinia'
import { api } from '@/api/client'

export const DEFAULT_SESSION_TITLE = '新对话'
const TITLE_REFRESH_DELAYS = [0, 800, 2000, 4500, 9000, 18000, 30000, 60000]
const autoTitleRefreshes = new Map()

export const useSessions = defineStore('sessions', {
    // currentSid 持久化到 localStorage：刷新后恢复当前会话视图，
    // 配合 ChatView.tryReattach 续播进行中的生成（刷新不中断）。
    // 所有 currentSid 变更必须走 setCurrent（唯一写入口，含持久化）
    state: () => ({ list: [], currentSid: localStorage.getItem('sp_current_sid') || null }),
    actions: {
        async load() {
            // 侧栏需展示全量会话：显式传大 page_size，避免后端默认 20 条截断
            const d = await api.get('/chat/sessions?page_size=500')
            this.list = d.list
        },
        setCurrent(sid) {
            this.currentSid = sid
            if (sid) localStorage.setItem('sp_current_sid', sid)
            else localStorage.removeItem('sp_current_sid')
        },
        // 新会话已在后端持久化，但列表请求有延迟。先插入占位项，避免首条消息后侧栏留白。
        ensurePlaceholder(sid) {
            if (!sid) return
            const existing = this.list.find(s => s.session_id === sid)
            if (existing) {
                if (!existing.title) existing.title = DEFAULT_SESSION_TITLE
                return
            }
            this.list.unshift({
                session_id: sid,
                title: DEFAULT_SESSION_TITLE,
                title_source: 'auto',
                last_active: new Date().toISOString(),
                message_count: 0,
                pinned: false,
                channel: null,
                readonly: false,
                from_session: null,
                handoff_status: null,
                succeeded_by: null,
            })
        },
        // 标题由后台独立生成；调度与 ChatView 生命周期解耦，切换到其他页面后仍会刷新侧栏。
        scheduleTitleRefresh(sid) {
            if (!sid || autoTitleRefreshes.has(sid)) return
            const timers = new Set()
            const stop = () => {
                timers.forEach(timer => window.clearTimeout(timer))
                autoTitleRefreshes.delete(sid)
            }
            for (const delay of TITLE_REFRESH_DELAYS) {
                let timer
                timer = window.setTimeout(async () => {
                    timers.delete(timer)
                    try {
                        await this.load()
                        const session = this.list.find(s => s.session_id === sid)
                        if (!session || (session.title && session.title !== DEFAULT_SESSION_TITLE)) {
                            stop()
                            return
                        }
                    } catch { /* 下一次刷新继续尝试 */ }
                    if (!timers.size) stop()
                }, delay)
                timers.add(timer)
            }
            autoTitleRefreshes.set(sid, timers)
        },
        // 查询会话 handoff 状态（会话上下文管理方案 v2）
        async handoffStatus(sid) {
            if (!sid) return null
            try {
                const d = await api.get(`/chat/session/${sid}/handoff-status`)
                return d.status
            } catch { return null }
        },
    },
})
