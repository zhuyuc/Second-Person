// 项目工作区共享状态（侧栏 / ChatView / SettingsView 共用）
import { defineStore } from 'pinia'
import { projectsApi } from '@/api/projects'

export const useProjects = defineStore('projects', {
  state: () => ({
    list: [],           // active 项目
    archivedList: [],   // 归档项目
    // currentProjectId 与 sessions.currentSid 联动派生，不单独维护
  }),
  getters: {
    activeCount: (s) => s.list.length,
    byId: (s) => (id) => s.list.find(p => p.id === id)
      || s.archivedList.find(p => p.id === id),
  },
  actions: {
    async load() {
      this.list = await projectsApi.list('active')
    },
    async loadArchived() {
      this.archivedList = await projectsApi.list('archived')
    },
    async loadAll() {
      const [a, b] = await Promise.all([
        projectsApi.list('active'),
        projectsApi.list('archived'),
      ])
      this.list = a
      this.archivedList = b
    },
    async create(payload) {
      const proj = await projectsApi.create(payload)
      await this.load()
      return proj
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
    async unarchive(id) {
      const r = await projectsApi.unarchive(id)
      await this.loadAll()
      return r
    },
    async purge(id) {
      const r = await projectsApi.purge(id)
      await this.loadAll()
      return r
    },
    async relocate(id, new_path) {
      const proj = await projectsApi.relocate(id, new_path)
      await this.load()
      return proj
    },
  },
})
