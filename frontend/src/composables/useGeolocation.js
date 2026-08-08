// 浏览器地理位置：Geolocation API 取经纬度 → 逆地理编码为城市名
// 免费无 Key；localhost 属安全上下文可用。位置缓存 sessionStorage，避免每轮重复弹窗/请求。
// 逆地理编码双源回退：BigDataCloud（国内可达）→ Nominatim → 裸坐标兑底
const CACHE_KEY = 'sp_geo_location'
const CACHE_COORD_KEY = 'sp_geo_coord'
// 位置变化小于此距离（km）视为未移动，直接复用缓存
const MOVE_THRESHOLD_KM = 1

function haversine(lat1, lon1, lat2, lon2) {
    const R = 6371
    const dLat = (lat2 - lat1) * Math.PI / 180
    const dLon = (lon2 - lon1) * Math.PI / 180
    const a = Math.sin(dLat / 2) ** 2 +
        Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a))
}

function getCoords() {
    return new Promise((resolve, reject) => {
        if (!navigator.geolocation) { reject(new Error('浏览器不支持定位')); return }
        navigator.geolocation.getCurrentPosition(
            pos => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
            err => reject(err),
            { timeout: 8000, maximumAge: 600000, enableHighAccuracy: false })
    })
}

async function _fetchJson(url, timeoutMs = 6000) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), timeoutMs)
    try {
        const resp = await fetch(url, {
            headers: { 'Accept': 'application/json' }, signal: ctrl.signal })
        return await resp.json()
    } finally { clearTimeout(timer) }
}

async function reverseGeocode(lat, lon) {
    // 主源：BigDataCloud（免费无 Key、CORS、国内可达）
    try {
        const g = await _fetchJson(
            `https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${lat}&longitude=${lon}&localityLanguage=zh`)
        const parts = [g.principalSubdivision, g.city, g.locality].filter(Boolean)
        const label = [...new Set(parts)].join('')
        if (label) return label
    } catch { /* 不可达时回退 Nominatim */ }
    // 备源：Nominatim（OpenStreetMap）
    try {
        const data = await _fetchJson(
            `https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lon}&zoom=12&accept-language=zh-CN`)
        const a = data.address || {}
        const parts = [a.state, a.city || a.county, a.district || a.suburb || a.town]
            .filter(Boolean)
        const label = [...new Set(parts)].join('') || data.display_name?.split(',')[0]
        if (label) return label
    } catch { /* 双源均失败 → 裸坐标兑底 */ }
    // 兑底：直接给坐标（大模型可从经纬度推断大致城市）
    return `坐标(${lat.toFixed(3)},${lon.toFixed(3)})`
}

// 获取当前位置（带缓存）。force=true 时忽略缓存重新定位。
export async function resolveLocation(force = false) {
    if (!force) {
        const cached = sessionStorage.getItem(CACHE_KEY)
        if (cached) return cached
    }
    const { lat, lon } = await getCoords()
    // 坐标未明显移动 → 复用旧标签，省一次逆地理编码请求
    const prevCoord = sessionStorage.getItem(CACHE_COORD_KEY)
    const prevLabel = sessionStorage.getItem(CACHE_KEY)
    if (!force && prevCoord && prevLabel) {
        const [plat, plon] = prevCoord.split(',').map(Number)
        if (haversine(lat, lon, plat, plon) < MOVE_THRESHOLD_KM) return prevLabel
    }
    const label = await reverseGeocode(lat, lon)
    if (label) {
        sessionStorage.setItem(CACHE_KEY, label)
        sessionStorage.setItem(CACHE_COORD_KEY, `${lat},${lon}`)
    }
    return label
}

// 读缓存（不触发定位），供发送消息时同步取用
export function cachedLocation() {
    return sessionStorage.getItem(CACHE_KEY) || null
}
