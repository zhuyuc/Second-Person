import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

function fontPreloadPlugin() {
    return {
        name: 'font-preload',
        transformIndexHtml(html, ctx) {
            const bundle = ctx.bundle
            if (!bundle) return html
            for (const name of Object.keys(bundle)) {
                if (name.endsWith('.woff2') && name.includes('tabler-icons-subset')) {
                    return html.replace(
                        '</head>',
                        `  <link rel="preload" as="font" type="font/woff2" href="/${name}" crossorigin>\n</head>`
                    )
                }
            }
            return html
        }
    }
}

export default defineConfig({
    plugins: [vue(), fontPreloadPlugin()],
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
