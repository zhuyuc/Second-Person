// 设置页用量统计加载（SettingsView 抽取）
import { ref } from 'vue'
import { settingsApi } from '@/api/settings'

export function useSettingsUsage() {
  const usage = ref({})
  const distribution = ref({})
  const trend = ref([])
  const monthCost = ref(null)
  const loading = ref(false)

  async function load({ source, model, period } = {}) {
    loading.value = true
    try {
      const filters = { source: source || undefined, model: model || undefined }
      const [u, d, t, m] = await Promise.all([
        settingsApi.usageSummary(filters),
        settingsApi.usageDistribution(filters),
        settingsApi.usageTrend(period, filters),
        settingsApi.monthCost(),
      ])
      usage.value = u
      distribution.value = d
      trend.value = t
      monthCost.value = m
      return { usage: u, distribution: d, trend: t, monthCost: m, filters }
    } finally {
      loading.value = false
    }
  }

  return { usage, distribution, trend, monthCost, loading, load }
}
