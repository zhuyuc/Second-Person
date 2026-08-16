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
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
        'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds())
}

export function dateKey(iso) {
    const d = toDate(iso)
    if (!d) return (iso || '').slice(0, 10)
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
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
    const y = new Date(now); y.setDate(now.getDate() - 1)
    if (d.toDateString() === y.toDateString()) return '昨天 ' + hm
    if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '/' + d.getDate()
    return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate()
}

// 绝对时间（默认到分钟；seconds=true 到秒）
export function formatTime(iso, { seconds = false } = {}) {
    const d = toDate(iso)
    if (!d) return iso || '-'
    const base = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
        `${pad(d.getHours())}:${pad(d.getMinutes())}`
    return seconds ? `${base}:${pad(d.getSeconds())}` : base
}

// 悬浮提示用完整时间（精确到秒）
export function formatTimeFull(iso) {
    return formatTime(iso, { seconds: true })
}

// 文件大小可读化
export function fmtSize(n) {
    if (n == null) return ''
    if (n < 1024) return n + ' B'
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
    return (n / 1024 / 1024).toFixed(1) + ' MB'
}

export function friendlyError(msg, fallback = '操作失败，请重试') {
    if (!msg) return fallback
    const m = msg.toLowerCase()
    if (m.includes('ssl') || m.includes('wrong_version_number'))
        return '服务连接失败，请检查网络或代理设置'
    if (m.includes('timeout') || m.includes('timed out'))
        return '服务响应超时，请稍后重试'
    if (m.includes('connection') && (m.includes('refused') || m.includes('reset') || m.includes('abort')))
        return '无法连接服务，请检查网络'
    if (m.includes('rate limit') || m.includes('429'))
        return '请求过于频繁，请稍后再试'
    if (m.includes('401') || m.includes('unauthorized') || m.includes('api key') || m.includes('invalid_api_key'))
        return 'API 认证失败，请检查密钥配置'
    if (m.includes('insufficient') && m.includes('quota'))
        return 'API 额度不足'
    if (m.includes('content_policy') || m.includes('content_filter'))
        return '内容被安全策略过滤，请调整输入后重试'
    if (m.includes('model_not_found') || m.includes('model not available') || m.includes('does not exist'))
        return '所选模型不可用，请检查模型配置'
    if (m.includes('client error') || (m.includes('400') && m.includes('bad request')))
        return '请求参数有误，请重试'
    if (m.includes('server error') || m.includes('500') || m.includes('502') || m.includes('503'))
        return '服务暂时不可用，请稍后重试'
    if (m.includes('404') || m.includes('not found'))
        return '请求的资源不存在'
    if (m.includes('for url')) return fallback.replace('操作', '请求')
    if (msg.length > 60) return fallback.replace('操作', '请求') + '（' + msg.substring(0, 40) + '…）'
    return msg
}
