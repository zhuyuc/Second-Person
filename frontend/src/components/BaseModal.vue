<script setup>
// SP-UI v4 统一弹窗组件（新增功能一律用它，详见 docs/UI_UX_SPEC.md）
// 统一能力：遮罩点击关闭 + 右上角 X + ESC 关闭 + 焦点捕获/归还 + role=dialog + 尺寸档位
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'

const props = defineProps({
    title: { type: String, default: '' },
    // 尺寸档位：'' (默认 500) | 'sm'(400) | 'md'(560) | 'lg'(720) | 'xl'(960/92vw)
    size: { type: String, default: '' },
    // 叠加在其他弹窗之上时用第二层级
    stacked: { type: Boolean, default: false },
    // 关闭方式开关（个别流程需禁止遮罩/ESC 关闭时置 false）
    closeOnOverlay: { type: Boolean, default: true },
    closeOnEsc: { type: Boolean, default: true },
    // 右上角 X 开关（线性强制流程如首次引导可隐藏，此时必须同时关闭遮罩/ESC）
    showClose: { type: Boolean, default: true },
})
const emit = defineEmits(['close'])

const panel = ref(null)
let lastActive = null

function close() { emit('close') }
function onOverlay() { if (props.closeOnOverlay) close() }
function onKeydown(e) {
    if (e.key === 'Escape' && props.closeOnEsc) { e.stopPropagation(); close() }
}

onMounted(() => {
    lastActive = document.activeElement
    document.addEventListener('keydown', onKeydown)
    nextTick(() => { if (panel.value) panel.value.focus() })
})
onBeforeUnmount(() => {
    document.removeEventListener('keydown', onKeydown)
    // 归还焦点到打开弹窗前的元素
    if (lastActive && typeof lastActive.focus === 'function') lastActive.focus()
})
</script>

<template>
    <div class="overlay" :style="{ zIndex: stacked ? 'var(--z-modal-2)' : 'var(--z-modal)' }" @click.self="onOverlay">
        <div ref="panel" class="modal" :class="size ? 'modal-' + size : ''" role="dialog" aria-modal="true"
            tabindex="-1" :aria-label="title || undefined">
            <div v-if="title || $slots.header" class="row" style="margin-bottom:14px;align-items:center">
                <span class="mt" style="margin:0">
                    <slot name="header">{{ title }}</slot>
                </span>
                <i v-if="showClose" class="ti ti-x btn-ghost"
                    style="cursor:pointer;font-size:var(--icon-sm);color:var(--muted);padding:4px;border-radius:8px;border:none;background:none"
                    title="关闭（Esc）" @click="close"></i>
            </div>
            <slot />
            <div v-if="$slots.footer" class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
                <slot name="footer" />
            </div>
        </div>
    </div>
</template>
