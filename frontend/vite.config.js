import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 构建产物输出到 app/static，供 FastAPI StaticFiles 挂载
export default defineConfig({
    plugins: [vue()],
    resolve: {
        alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
    },
    build: {
        outDir: '../app/static',
        emptyOutDir: true
    },
    server: {
        proxy: { '/api': 'http://localhost:8000' }
    }
})
