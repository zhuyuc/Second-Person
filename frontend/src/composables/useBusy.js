// SP-UI v4 统一异步态（详见 docs/UI_UX_SPEC.md 2.1）
// 用法：const { busy, run } = useBusy()
//   <button :disabled="busy('save')" @click="run('save', saveEdit)">
//     <i v-if="busy('save')" class="ti ti-loader-2"></i> 保存</button>
// run() 会在同一 key 进行中时忽略重复触发，杜绝双提交；结束后自动清除。
import { reactive } from 'vue'

export function useBusy() {
    const state = reactive({ keys: {} })
    const busy = (k) => !!state.keys[k]
    const anyBusy = () => Object.values(state.keys).some(Boolean)
    async function run(k, fn) {
        if (state.keys[k]) return
        state.keys[k] = true
        try { return await fn() } finally { state.keys[k] = false }
    }
    return { busy, run, anyBusy }
}
