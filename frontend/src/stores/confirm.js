// 系统内置确认对话框 store（替代原生 window.confirm）。
// 用法：const confirm = useConfirm(); if (!await confirm.ask({ message, danger })) return
import { defineStore } from 'pinia'

let pendingResolve = null

export const useConfirm = defineStore('confirm', {
    state: () => ({ item: null }),
    actions: {
        // opts 支持字符串或 { title, message, confirmText, cancelText, danger }
        ask(opts) {
            const o = typeof opts === 'string' ? { message: opts } : (opts || {})
            this.item = {
                title: o.title || '确认操作',
                message: o.message || '',
                confirmText: o.confirmText || '确定',
                cancelText: o.cancelText || '取消',
                danger: !!o.danger,
            }
            return new Promise((resolve) => { pendingResolve = resolve })
        },
        _settle(val) {
            this.item = null
            if (pendingResolve) { pendingResolve(val); pendingResolve = null }
        },
        confirm() { this._settle(true) },
        cancel() { this._settle(false) },
    },
})
