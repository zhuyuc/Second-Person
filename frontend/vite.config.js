import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
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
    plugins: [
        vue(),
        Components({
            dirs: ['src/components'],
            extensions: ['vue'],
            deep: true,
            dts: 'src/components.d.ts',
            directoryAsNamespace: false,
            globalNamespaces: [],
            include: [/\.vue$/, /\.vue\?vue/],
            exclude: [/[\\/]node_modules[\\/]/, /[\\/]\.git[\\/]/, /[\\/]\.nuxt[\\/]/],
        }),
        fontPreloadPlugin(),
    ],
    resolve: {
        alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
    },
    build: {
        outDir: '../app/static',
        emptyOutDir: true,
        // 使用 esbuild（默认）作为 minifier，比 terser 快 20-40x，且 tree-shake 更激进
        minify: 'esbuild',
        // 略微放宽 chunk 阈值：diagram chunk 已知包含 mermaid 且已懒加载
        chunkSizeWarningLimit: 800,
        // CSS 也进入 minify（默认开启 esbuild）
        cssMinify: 'esbuild',
        rollupOptions: {
            output: {
                manualChunks(id) {
                    // mermaid 全家桶保持单一 chunk，避免子拆分产生循环引用
                    if (id.includes('node_modules/mermaid') || id.includes('node_modules/dagre') || id.includes('node_modules/cytoscape')) {
                        return 'diagram'
                    }
                    if (id.includes('node_modules/marked')) return 'marked'
                    if (id.includes('node_modules/katex')) return 'katex'
                    if (id.includes('node_modules/vue')) return 'vendor-vue'
                },
            },
        },
    },
    esbuild: {
        // 生产构建移除 console.log/debug/info（保留 warn/error 便于线上排查）
        drop: process.env.NODE_ENV === 'production' ? ['debugger'] : [],
        pure: process.env.NODE_ENV === 'production'
            ? ['console.log', 'console.debug', 'console.info']
            : [],
    },
    server: {
        proxy: { '/api': 'http://localhost:8000' }
    }
})
