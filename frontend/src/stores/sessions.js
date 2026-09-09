// 会话列表共享状态：全局侧栏(SessionSidebar)与 ChatView 共用。
import { defineStore } from 'pinia'
import { chatApi } from '@/api/chat'

const DEFAULT_SESSION_TITLE = '新对话'
const TITLE_REFRESH_DELAYS = [0, 800, 2000, 4500, 9000, 18000, 30000, 60000]
const SESSION_PAGE_SIZE = 50
const autoTitleRefreshes = new Map()

export const useSessions = defineStore('sessions', {
  // currentSid 持久化到 localStorage：刷新后恢复当前会话视图，
  // 配合 ChatView.tryReattach 续播进行中的生成（刷新不中断）。
  // 所有 currentSid 变更必须走 setCurrent（唯一写入口，含持久化）
  // pendingProjectId：M5.1 延迟建项目会话 —— 侧栏点「新建会话」不立即
  // 落库，仅记项目 id；首条消息 send 时才 create_session(project_id=?)
  state: () => ({
    list: [],
    listLoaded: false,
    listLoading: false,
    page: 0,
    nextCursor: null,
    hasMore: true,
    currentSid: localStorage.getItem('sp_current_sid') || null,
    pendingProjectId: null,
  }),
  actions: {
    async load({ reset = true } = {}) {
      if (this.listLoading || (!reset && !this.hasMore)) return
      this.listLoading = true
      const page = reset ? 1 : this.page + 1
      try {
        const d = await chatApi.sessions({
          // 空游标明确进入后端 keyset 模式，避免首次请求意外退回 OFFSET 兼容路径。
          cursor: reset ? '' : this.nextCursor,
          page,
          pageSize: SESSION_PAGE_SIZE,
        })
        const incoming = d.list || []
        if (reset) this.list = incoming
        else {
          const existing = new Set(this.list.map((session) => session.session_id))
          this.list.push(...incoming.filter((session) => !existing.has(session.session_id)))
        }
        this.page = page
        this.nextCursor = d.next_cursor || null
        this.hasMore = this.nextCursor ? true : this.list.length < (d.total || 0)
        this.listLoaded = true
      } finally {
        this.listLoading = false
      }
      // 刷新恢复：localStorage 里的会话若已删除/归档，不再继续请求，清掉回到新对话
      this.discardMissingCurrent()
    },
    loadMore() {
      return this.load({ reset: false })
    },
    discardMissingCurrent() {
      const sid = this.currentSid
      // 分页首屏未命中不代表会话已删除，只有所有页都读取完才清除恢复态。
      if (!sid || !this.listLoaded || this.hasMore) return
      if (!this.list.some((s) => s.session_id === sid)) {
        this.setCurrent(null)
      }
    },
    hasSession(sid) {
      return !!sid && this.list.some((s) => s.session_id === sid)
    },
    // 局部更新：pin/rename/archive 等操作后只改对应项，避免刷新整个侧栏。
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
            // 标题生成是单会话状态变化，定向读取摘要而不是轮询整个会话列表。
            const summary = await chatApi.sessionSummary(sid)
            const session = this.list.find((item) => item.session_id === sid)
            if (!session || summary.archived) {
              stop()
              return
            }
            this.applyPatch(sid, summary)
            if (summary.title && summary.title !== DEFAULT_SESSION_TITLE) {
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
