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
