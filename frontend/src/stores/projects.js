// 项目工作区共享状态（侧栏 / ChatView / SettingsView 共用）
import { defineStore } from 'pinia'
import { projectsApi } from '@/api/projects'

export const useProjects = defineStore('projects', {
  state: () => ({
    list: [], // active 项目
    archivedList: [], // 归档项目
    // currentProjectId 与 sessions.currentSid 联动派生，不单独维护
  }),
  getters: {
    byId: (s) => (id) => s.list.find((p) => p.id === id) || s.archivedList.find((p) => p.id === id),
  },
  actions: {
    async load() {
      this.list = await projectsApi.list('active')
    },
    async loadAll() {
      const [a, b] = await Promise.all([projectsApi.list('active'), projectsApi.list('archived')])
      this.list = a
      this.archivedList = b
    },
    async rename(id, title) {
      const proj = await projectsApi.patch(id, { title })
      await this.load()
      return proj
    },
    async archive(id) {
      const r = await projectsApi.archive(id)
      await this.loadAll()
      return r
    },
  },
})
