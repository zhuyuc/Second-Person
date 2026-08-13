// 系统内置确认对话框 store（替代原生 window.confirm）。
// 用法：const confirm = useConfirm(); if (!await confirm.ask({ message, danger })) return
// 带复选框：ask({ message, checkbox: '勾选项文案' }) → 确认时 resolve { ok: true, checked }
import { defineStore } from 'pinia'

let pendingResolve = null

export const useConfirm = defineStore('confirm', {
    state: () => ({ item: null, checked: false }),
    actions: {
        // opts 支持字符串或 { title, message, confirmText, cancelText, danger, checkbox }
        ask(opts) {
            if (pendingResolve) { pendingResolve(false); pendingResolve = null }
            const o = typeof opts === 'string' ? { message: opts } : (opts || {})
            this.checked = false
            this.item = {
                title: o.title || '确认操作',
                message: o.message || '',
                confirmText: o.confirmText || '确定',
                cancelText: o.cancelText || '取消',
                danger: !!o.danger,
                checkbox: o.checkbox || '',
            }
            return new Promise((resolve) => { pendingResolve = resolve })
        },
        _settle(val) {
            this.item = null
            if (pendingResolve) { pendingResolve(val); pendingResolve = null }
        },
        confirm() {
            // 无复选框时仍 resolve true，与既有调用方布尔判断完全兼容
            const withBox = this.item && this.item.checkbox
            this._settle(withBox ? { ok: true, checked: this.checked } : true)
        },
        cancel() { this._settle(false) },
    },
})
