// 领域枚举中文化映射：展示用中文标签，提交后端仍用原始英文值
// 方案 B：后端 domain_labels 缓存（新领域由 LLM 自动翻译入库）优先，
// 静态种子映射作为加载前/失败时的兜底；未命中或本身为中文的原样显示
import { ref } from 'vue'
import { api } from '@/api/client'

const DOMAIN_LABELS = {
  ai: '人工智能',
  computing: '计算机',
  finance: '金融',
  investment: '投资',
  technology: '技术',
  frontend_architecture: '前端架构',
  storage_architecture: '存储架构',
  system_configuration: '系统配置',
  product_design: '产品设计',
  product_development: '产品开发',
  product_management: '产品管理',
  project_management: '项目管理',
  software_development: '软件开发',
  web_development: 'Web开发',
  software_engineering: '软件工程',
  data_science: '数据科学',
  machine_learning: '机器学习',
  security: '安全',
  business: '商业',
  marketing: '市场营销',
  design: '设计',
  education: '教育',
  health: '健康',
  lifestyle: '生活方式',
  career: '职业发展',
  psychology: '心理学',
  general: '通用',
}

// 后端下发的映射（响应式：加载完成后模板自动刷新）
const remoteLabels = ref({})
let loading = null

export function loadDomainLabels(force = false) {
  if (loading && !force) return loading
  loading = api.get('/memory/domain-labels')
    .then(m => { remoteLabels.value = m || {} })
    .catch(() => { loading = null /* 失败允许下次重试 */ })
  return loading
}

export function domainLabel(domain) {
  if (!domain) return domain
  const key = String(domain).toLowerCase().replace(/-/g, '_')
  return remoteLabels.value[domain] || remoteLabels.value[key]
    || DOMAIN_LABELS[key] || domain
}
