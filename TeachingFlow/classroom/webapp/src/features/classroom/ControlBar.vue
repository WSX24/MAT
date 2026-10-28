<script setup lang="ts">
import type { Rate } from './useClassroom'

/** 课堂控制条：暂停/继续、上一页/下一页、语速、结束课堂。行为与旧 React 版一致。 */
const props = defineProps<{
  paused: boolean
  rate: Rate
  slideIndex: number
  slideCount: number
  finishing: boolean
  disabled: boolean
}>()

const emit = defineEmits<{
  togglePause: []
  prev: []
  next: []
  rate: [value: Rate]
  finish: []
}>()

const RATES: Rate[] = [0.8, 1, 1.2]
</script>

<template>
  <div class="control-bar" role="toolbar" aria-label="课堂控制">
    <button
      class="btn-primary-sm"
      type="button"
      :disabled="props.disabled"
      :aria-label="props.paused ? '继续讲解（空格）' : '暂停讲解（空格）'"
      @click="emit('togglePause')"
    >
      <svg v-if="props.paused" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l10-6.5z" /></svg>
      <svg v-else viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="5" width="3.6" height="14" rx="1" /><rect x="13.4" y="5" width="3.6" height="14" rx="1" /></svg>
      {{ props.paused ? '继续' : '暂停' }}
    </button>

    <div class="control-group">
      <button class="icon-btn-sm" type="button" aria-label="上一页（←）" title="上一页（←）" :disabled="props.disabled || props.slideIndex <= 0" @click="emit('prev')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14.5 6-6 6 6 6" /></svg>
      </button>
      <span class="slide-counter" aria-live="polite">
        {{ props.slideCount ? `${props.slideIndex + 1} / ${props.slideCount}` : '—' }}
      </span>
      <button class="icon-btn-sm" type="button" aria-label="下一页（→）" title="下一页（→）" :disabled="props.disabled || props.slideCount === 0 || props.slideIndex >= props.slideCount - 1" @click="emit('next')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 6 6 6-6 6" /></svg>
      </button>
    </div>

    <div class="control-group" role="group" aria-label="语速">
      <span class="control-hint">语速</span>
      <div class="segmented">
        <button
          v-for="value in RATES"
          :key="value"
          class="segmented-item"
          type="button"
          :aria-pressed="props.rate === value"
          :aria-label="`语速 ${value} 倍`"
          @click="emit('rate', value)"
        >{{ value }}×</button>
      </div>
    </div>

    <span class="spacer" />
    <span class="control-hint">空格暂停/继续 · ←→ 翻页</span>
    <button class="btn-danger-sm" type="button" :disabled="props.finishing" aria-label="结束课堂并看小结" @click="emit('finish')">
      <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>
      {{ props.finishing ? '正在收尾…' : '结束课堂' }}
    </button>
  </div>
</template>
