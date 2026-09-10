// 侧边会话 ChatView 懒加载入口。单独抽出便于单测注入慢 loader，复现刷新后首次挂载竞态。
export function loadAsideChatView() {
  return import('@/views/ChatView.vue')
}
