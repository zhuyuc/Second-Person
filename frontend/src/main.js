import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import ChatView from './views/ChatView.vue'
import MemoryView from './views/MemoryView.vue'
import SettingsView from './views/SettingsView.vue'
import '@tabler/icons-webfont/dist/tabler-icons.min.css'
import './style.css'

const router = createRouter({
    history: createWebHashHistory(),
    routes: [
        { path: '/', redirect: '/chat' },
        { path: '/chat', component: ChatView },
        { path: '/memory', component: MemoryView },
        { path: '/settings', component: SettingsView },
    ],
})

const app = createApp(App)
app.config.errorHandler = (err, vm, info) => {
    console.error('[Vue Error]', err, info)
}
app.use(createPinia()).use(router).mount('#app')
