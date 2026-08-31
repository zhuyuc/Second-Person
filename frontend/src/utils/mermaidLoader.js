// Mermaid 懒加载入口：只在真正需要渲染图表时才加载 mermaid 库
// 避免 3MB+ 的 mermaid 进入首屏 bundle

let mermaidPromise = null

export async function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((m) => m.default || m)
  }
  return mermaidPromise
}

export async function applyMermaidTheme() {
  const mermaid = await loadMermaid()
  const { applyMermaidTheme: apply } = await import('./mermaidThemeImpl')
  return apply(mermaid)
}
