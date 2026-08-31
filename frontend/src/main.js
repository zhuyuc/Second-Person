import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import { useToast } from './stores/toast'
import './assets/tabler-icons-subset.css'
import './style.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/chat', component: () => import('./views/ChatView.vue') },
    { path: '/memory', component: () => import('./views/MemoryView.vue') },
    { path: '/settings', component: () => import('./views/SettingsView.vue') },
  ],
})

const app = createApp(App)
const pinia = createPinia()
app.use(pinia).use(router)

// toast store 需 pinia 挂载后创建；用一次 lazy accessor 保证首访问不早于 mount
let _toast = null
function toast() {
  if (!_toast) {
    try {
      _toast = useToast()
    } catch {
      return null
    }
  }
  return _toast
}

function friendlyMessage(err) {
  if (!err) return '未知错误'
  const msg = err.message || String(err)
  // chunk 加载失败最常见：网络中断 / 部署更新导致老 hash 404
  if (/Loading chunk|Failed to fetch dynamically imported module|dynamically imported module/i.test(msg)) {
    return '模块加载失败，请刷新页面'
  }
  return msg
}

function notifyGlobalError(err, source) {
  const t = toast()
  if (!t) return
  try {
    t.push('error', `[${source}] ${friendlyMessage(err)}`)
  } catch {
    /* 忽略二次错误 */
  }
}

// 全局错误邻界：所有未捕获错误统一入口，避免白屏 / 静默失败
app.config.errorHandler = (err, _vm, info) => {
  console.error('[Vue Error]', err, info)
  notifyGlobalError(err, `Vue:${info}`)
}

// 浏览器级 Promise 拒绝 & 同步错误兜底
// 覆盖场景：SSE 断线后未捕获的 fetch reject、动态 import chunk 加载失败等
window.addEventListener('unhandledrejection', (event) => {
  console.error('[Unhandled Rejection]', event.reason)
  notifyGlobalError(event.reason, 'Unhandled')
})
window.addEventListener('error', (event) => {
  if (event.error) {
    console.error('[Window Error]', event.error)
    notifyGlobalError(event.error, 'Window')
  }
})

app.mount('#app')
