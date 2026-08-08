// 会话列表共享状态：全局侧栏(SessionSidebar)与 ChatView 共用。
import { defineStore } from 'pinia'
import { api } from '@/api/client'

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
    },
})
