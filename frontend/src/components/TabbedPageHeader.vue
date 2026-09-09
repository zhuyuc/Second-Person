<script setup>
defineProps({
  title: { type: String, required: true },
  tabs: { type: Array, required: true },
  activeTab: { type: Number, required: true },
})
const emit = defineEmits(['select'])
</script>

<template>
  <h1>{{ title }}</h1>
  <div class="tabs-sticky">
    <div class="tabs">
      <button
        v-for="(label, index) in tabs"
        :key="label"
        type="button"
        class="tab"
        :class="{ active: activeTab === index }"
        @click="emit('select', index)"
      >
        {{ label }}
      </button>
    </div>
  </div>
</template>
