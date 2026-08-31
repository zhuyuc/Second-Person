// 会话列表共享状态：全局侧栏(SessionSidebar)与 ChatView 共用。
import { defineStore } from 'pinia'
import { chatApi } from '@/api/chat'

const DEFAULT_SESSION_TITLE = '新对话'
const TITLE_REFRESH_DELAYS = [0, 800, 2000, 4500, 9000, 18000, 30000, 60000]
const autoTitleRefreshes = new Map()

export const useSessions = defineStore('sessions', {
  // currentSid 持久化到 localStorage：刷新后恢复当前会话视图，
  // 配合 ChatView.tryReattach 续播进行中的生成（刷新不中断）。
  // 所有 currentSid 变更必须走 setCurrent（唯一写入口，含持久化）
  // pendingProjectId：M5.1 延迟建项目会话 —— 侧栏点「新建会话」不立即
  // 落库，仅记项目 id；首条消息 send 时才 create_session(project_id=?)
  state: () => ({
    list: [],
    currentSid: localStorage.getItem('sp_current_sid') || null,
    pendingProjectId: null,
  }),
  actions: {
    async load() {
      // 侧栏需展示全量会话：显式传大 page_size，避免后端默认 20 条截断
      const d = await chatApi.sessions()
      this.list = d.list
    },
    // 局部更新：pin/rename/archive 等操作后只改对应项，避免全量 load()
    // 重新拉 500 条 + 触发 500 个 v-for 节点 diff
    applyPatch(sid, patch) {
      const s = this.list.find((x) => x.session_id === sid)
      if (s) Object.assign(s, patch)
    },
    removeLocal(sid) {
      const i = this.list.findIndex((x) => x.session_id === sid)
      if (i >= 0) this.list.splice(i, 1)
    },
    setCurrent(sid) {
      this.currentSid = sid
      if (sid) localStorage.setItem('sp_current_sid', sid)
      else localStorage.removeItem('sp_current_sid')
      // 切到已有会话 → 清空 pendingProjectId（否则会污染下一次新建）
      if (sid) this.pendingProjectId = null
    },
    setPendingProject(pid) {
      this.pendingProjectId = pid || null
    },
    // 新会话已在后端持久化，但列表请求有延迟。先插入占位项，避免首条消息后侧栏留白。
    // M5.1：projectId 可选 —— 项目下新建会话时传入，占位就挂到工作区段
    ensurePlaceholder(sid, projectId = null) {
      if (!sid) return
      const existing = this.list.find((s) => s.session_id === sid)
      if (existing) {
        if (!existing.title) existing.title = DEFAULT_SESSION_TITLE
        if (projectId && !existing.project_id) existing.project_id = projectId
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
        project_id: projectId,
        archived: false,
        archived_source: null,
        sandbox_mode: null,
      })
    },
    // 标题由后台独立生成；调度与 ChatView 生命周期解耦，切换到其他页面后仍会刷新侧栏。
    scheduleTitleRefresh(sid) {
      if (!sid || autoTitleRefreshes.has(sid)) return
      const timers = new Set()
      const stop = () => {
        timers.forEach((timer) => window.clearTimeout(timer))
        autoTitleRefreshes.delete(sid)
      }
      for (const delay of TITLE_REFRESH_DELAYS) {
        const timer = window.setTimeout(async () => {
          timers.delete(timer)
          try {
            await this.load()
            const session = this.list.find((s) => s.session_id === sid)
            if (!session || (session.title && session.title !== DEFAULT_SESSION_TITLE)) {
              stop()
              return
            }
          } catch {
            /* 下一次刷新继续尝试 */
          }
          if (!timers.size) stop()
        }, delay)
        timers.add(timer)
      }
      autoTitleRefreshes.set(sid, timers)
    },
  },
})
