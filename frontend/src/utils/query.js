// URL 查询参数统一构造：避免手写拼接遗漏 encodeURIComponent
export function withQuery(path, params = {}) {
    const qs = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
        if (value === undefined || value === null || value === '') continue
        qs.set(key, String(value))
    }
    const query = qs.toString()
    return query ? `${path}?${query}` : path
}
