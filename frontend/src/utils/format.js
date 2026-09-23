// 时间/大小格式化统一入口（三大视图单一来源，禁止各自重复实现）
// 入参兼容后端 "YYYY-MM-DD HH:MM:SS" 与 ISO 两种格式

function toDate(iso) {
  if (!iso) return null
  const d = new Date(String(iso).replace(' ', 'T'))
  return isNaN(d.getTime()) ? null : d
}

const pad = (n) => String(n).padStart(2, '0')

export function nowLocalIso() {
  const d = new Date()
  return (
    d.getFullYear() +
    '-' +
    pad(d.getMonth() + 1) +
    '-' +
    pad(d.getDate()) +
    'T' +
    pad(d.getHours()) +
    ':' +
    pad(d.getMinutes()) +
    ':' +
    pad(d.getSeconds())
  )
}

// 搜索面板紧凑时间：同日 HH:MM，否则 M/D
export function formatCompactTime(iso) {
  const d = toDate(iso)
  if (!d) return ''
  const now = new Date()
  const hm = pad(d.getHours()) + ':' + pad(d.getMinutes())
  if (d.toDateString() === now.toDateString()) return hm
  return d.getMonth() + 1 + '/' + d.getDate()
}

// 相对时间（对话消息列表用）：刚刚 / N 分钟前 / 今天 HH:MM / 昨天 HH:MM / M/D / Y/M/D
export function formatRelative(iso) {
  const d = toDate(iso)
  if (!d) return iso || ''
  const now = new Date()
  const diff = (now - d) / 1000
  const hm = pad(d.getHours()) + ':' + pad(d.getMinutes())
  if (diff < 60) return '刚刚'
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前'
  if (d.toDateString() === now.toDateString()) return '今天 ' + hm
  const y = new Date(now)
  y.setDate(now.getDate() - 1)
  if (d.toDateString() === y.toDateString()) return '昨天 ' + hm
  if (d.getFullYear() === now.getFullYear()) return d.getMonth() + 1 + '/' + d.getDate()
  return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate()
}

// 绝对时间（默认到分钟；seconds=true 到秒）
export function formatTime(iso, { seconds = false } = {}) {
  const d = toDate(iso)
  if (!d) return iso || '-'
  const base =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return seconds ? `${base}:${pad(d.getSeconds())}` : base
}

// 悬浮提示用完整时间（精确到秒）
export function formatTimeFull(iso) {
  return formatTime(iso, { seconds: true })
}

// 时长可读化：中文时/分/秒/毫秒，按需保留有效单位（如 1537ms → "1秒537毫秒"、65000ms → "1分5秒"）
export function fmtDuration(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return ''
  const n = Number(ms)
  if (n <= 0) return '<1毫秒'
  if (n < 1000) return `${Math.round(n)}毫秒`
  let rest = Math.floor(n)
  const h = Math.floor(rest / 3600000)
  rest %= 3600000
  const m = Math.floor(rest / 60000)
  rest %= 60000
  const s = Math.floor(rest / 1000)
  const msPart = rest % 1000
  let out = ''
  if (h) out += `${h}时`
  if (m) out += `${m}分`
  if (s || !out) out += `${s}秒`
  if (!h && !m && msPart) out += `${msPart}毫秒`
  return out
}

// 文件大小可读化
export function fmtSize(n) {
  if (n === null || n === undefined) return ''
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

export function friendlyError(msg, fallback = '操作失败，请重试') {
  if (!msg) return fallback
  const raw = String(msg).trim()
  const m = raw.toLowerCase()
  if (raw.includes('请求体过大') || (m.includes('content-length') && m.includes('2'))) {
    return '图片或内容过大（单次上限约 2MB），请压缩图片、少传几张后再试'
  }
  if (m.includes('ssl') || m.includes('wrong_version_number')) {
    return '无法安全连接外部服务，请检查网络或代理设置'
  }
  if (m.includes('timeout') || m.includes('timed out') || m.includes('time-out')) {
    return '等待服务响应超时，请稍后重试'
  }
  if (
    m.includes('connection') &&
    (m.includes('refused') || m.includes('reset') || m.includes('abort'))
  ) {
    return '无法连接服务，请确认程序已启动且网络正常'
  }
  if (m.includes('failed to fetch') || m.includes('networkerror') || m.includes('load failed')) {
    return '网络异常，请检查网络或确认本地服务正在运行'
  }
  if (m.includes('rate limit') || m.includes('too many requests') || /\b429\b/.test(m)) {
    return '请求过于频繁，请稍后再试'
  }
  if (
    m.includes('401') ||
    m.includes('unauthorized') ||
    m.includes('api key') ||
    m.includes('invalid_api_key') ||
    m.includes('authentication')
  ) {
    return '服务认证失败，请到「设置」检查 API 密钥是否正确'
  }
  if (m.includes('insufficient') && m.includes('quota')) {
    return 'API 额度不足，请充值或更换可用密钥'
  }
  if (m.includes('content_policy') || m.includes('content_filter')) {
    return '内容被安全策略拦截，请调整表述后再试'
  }
  if (
    m.includes('model_not_found') ||
    m.includes('model not available') ||
    (m.includes('model') && m.includes('does not exist'))
  ) {
    return '所选模型不可用，请到「设置」更换模型'
  }
  if (m.includes('404') || m.includes('not found')) {
    return '要找的内容不存在，可能已被删除'
  }
  if (m.includes('client error') || (m.includes('400') && m.includes('bad request'))) {
    return '请求参数有误，请检查输入后重试'
  }
  if (
    m.includes('server error') ||
    m.includes('internal server error') ||
    /\b50[0234]\b/.test(m)
  ) {
    return '服务暂时不可用，请稍后重试'
  }
  // httpx / aiohttp 一类 "For url ..." 技术尾巴，对用户无意义
  if (m.includes('for url')) return fallback
  // 纯技术堆栈 / 过长英文，收敛为可读短句；不把 Traceback 等原文塞进提示
  if (raw.length > 80 || /traceback|exception|stack|errno/i.test(raw)) {
    return fallback
  }
  return raw
}
