<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { TranscriptEntry } from './useClassroom'

/** 字幕与提问：逐句字幕、实时一句、快捷提问、自由提问。 */
const props = defineProps<{
  entries: TranscriptEntry[]
  caption: string
  connected: boolean
}>()

const emit = defineEmits<{ ask: [text: string] }>()

const draft = ref('')
const list = ref<HTMLDivElement | null>(null)

const QUICK_ASKS = ['这一页的重点是什么？', '能再举个例子吗？', '刚才那句没听懂', '这和上一节有什么关系？']

function scrollToBottom() {
  void nextTick(() => {
    if (list.value) list.value.scrollTop = list.value.scrollHeight
  })
}

watch(
  () => [props.entries.length, props.caption],
  scrollToBottom,
  { immediate: true },
)

function submit() {
  const text = draft.value.trim()
  if (!text) return
  emit('ask', text)
  draft.value = ''
  scrollToBottom()
}
</script>

<template>
  <div class="sidebar-head">
    <span>字幕与提问</span>
    <span class="sidebar-head-count">{{ props.entries.length }} 条</span>
  </div>

  <div ref="list" class="transcript" role="log" aria-live="polite" aria-label="课堂字幕">
    <div v-if="!props.entries.length && !props.caption" class="line">
      <span class="line-text">课堂开始后，老师的讲解会逐句出现在这里。</span>
    </div>

    <div
      v-for="entry in props.entries"
      :key="entry.id"
      class="line"
      :class="{ 'line--me': entry.who === 'me' }"
    >
      <span class="line-who">{{ entry.who === 'me' ? '你' : entry.who === 'system' ? '提示' : '老师' }}</span>
      <span class="line-text">{{ entry.text }}</span>
    </div>

    <div v-if="props.caption" class="line line--live" aria-live="off">
      <span class="line-who">正在讲…</span>
      <span class="line-text">{{ props.caption }}</span>
    </div>
  </div>

  <div class="ask-box">
    <div class="quick-asks" aria-label="快捷提问">
      <button
        v-for="text in QUICK_ASKS"
        :key="text"
        class="quick-ask"
        type="button"
        :disabled="!props.connected"
        @click="emit('ask', text)"
      >{{ text }}</button>
    </div>
    <div class="ask-row">
      <label class="visually-hidden" for="ask-input">输入你的问题</label>
      <textarea
        id="ask-input"
        v-model="draft"
        rows="2"
        placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"
        @keydown.enter.exact.prevent="submit"
      />
      <button class="ask-send" type="button" aria-label="发送问题" title="发送问题" :disabled="!draft.trim()" @click="submit">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12 20 4l-5 16-3-7-8-1Z" /></svg>
      </button>
    </div>
  </div>
</template>
