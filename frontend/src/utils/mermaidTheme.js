// Mermaid 统一主题：CSS 变量驱动（getComputedStyle 天然跟随 prefers-color-scheme），
// ChatView 内联代码块与 MermaidChart 组件共用同一配置，杜绝两套主题互相覆盖。
// 主题变量映射定义在 style.css :root 的 --mc-* token（从 SP-UI v4 色板派生）。
//
// 注意：本模块不再静态 import mermaid，而是接收实例参数，由调用方负责加载。
// 请优先使用 @/utils/mermaidLoader 的 applyMermaidTheme()。

import { applyMermaidTheme as applyTheme } from './mermaidLoader'

export async function applyMermaidTheme() {
  return applyTheme()
}
