<script setup lang="ts">
import { computed, ref } from 'vue'
import type { BuilderAskTool } from '@/api/types'

/**
 * 模型提问卡（工具 ask_user_question 的落地）。
 *
 * 它存在的意义：模型问的是"想学到什么程度 / 有什么基础 / 学多久"，这些问题
 * **必须让用户用点选回答**，而不是逼用户打一段话 —— 打字的成本会让很多人
 * 直接说"随便"，那这次提问就白问了。
 *
 * 两种提交路径：
 *   · 只有一个单选问题 → 点选项即提交（最快）；
 *   · 其余情况 → 先选，再点「提交回答」，拼成一句话发给模型。
 */
const props = defineProps<{
  tool: BuilderAskTool
  disabled?: boolean
}>()

const emit = defineEmits<{ (e: 'answer', text: string): void }>()

const draft = ref('')
/** 单选：questionId -> label；多选：questionId -> label[] */
const picked = ref<Record<string, string | string[]>>({})

const answered = computed(() => props.tool.answered)
const singleQuick = computed(() => props.tool.questions.length === 1 && !props.tool.questions[0]?.multiple)

function isPicked(questionId: string, label: string): boolean {
  const value = picked.value[questionId]
  return Array.isArray(value) ? value.includes(label) : value === label
}

function choose(questionId: string, label: string, multiple: boolean) {
  if (props.disabled || answered.value) return
  if (multiple) {
    const current = Array.isArray(picked.value[questionId])
      ? [...(picked.value[questionId] as string[])]
      : []
    const index = current.indexOf(label)
    if (index >= 0) current.splice(index, 1)
    else current.push(label)
    picked.value[questionId] = current
    return
  }
  picked.value[questionId] = label
  if (singleQuick.value) submit()
}

function compose(): string {
  const parts: string[] = []
  for (const question of props.tool.questions) {
    const value = picked.value[question.id]
    const text = Array.isArray(value) ? value.join('、') : (value || '')
    if (!text) continue
    parts.push(`${question.header || question.question}：${text}`)
  }
  return parts.join('；')
}

function submit() {
  if (props.disabled || answered.value) return
  const composed = compose()
  const text = [composed, draft.value.trim()].filter(Boolean).join('；')
  if (!text) return
  emit('answer', text)
}

function onEnter(event: KeyboardEvent) {
  if (event.shiftKey) return
  event.preventDefault()
  submit()
}
</script>

<template>
  <div class="ask-card" :class="{ 'ask-card--done': answered }">
    <div v-for="question in tool.questions" :key="question.id" class="ask-question">
      <p v-if="question.header" class="ask-header">{{ question.header }}</p>
      <p class="ask-text">{{ question.question }}</p>
      <div v-if="question.options.length" class="ask-options">
        <button
          v-for="option in question.options"
          :key="option.label"
          class="ask-option"
          type="button"
          :aria-pressed="isPicked(question.id, option.label)"
          :disabled="disabled || answered"
          @click="choose(question.id, option.label, !!question.multiple)"
        >
          <strong>{{ option.label }}</strong>
          <small v-if="option.description">{{ option.description }}</small>
        </button>
      </div>
    </div>

    <div v-if="answered" class="ask-answer">
      <span class="ask-answer-label">你的回答</span>
      <span>{{ tool.answer }}</span>
    </div>

    <form v-else class="ask-free" @submit.prevent="submit()">
      <input
        v-model="draft"
        type="text"
        :disabled="disabled"
        placeholder="也可以直接写自己的想法，例如「我不想学证明题」"
        aria-label="自由回答"
        @keydown.enter="onEnter"
      >
      <button class="ask-submit" type="submit" :disabled="disabled || (!draft.trim() && !compose())">
        提交回答
      </button>
    </form>
  </div>
</template>

<style scoped>
.ask-card { margin-top: 10px; padding: 13px 14px; border: 1px solid color-mix(in oklch, var(--accent) 20%, var(--border)); border-radius: 12px; background: color-mix(in oklch, var(--accent) 3%, var(--surface)); }
.ask-card--done { border-color: var(--border); background: var(--surface); }
.ask-question + .ask-question { margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--border-soft); }
.ask-header { margin: 0 0 3px; color: var(--accent); font-family: var(--font-mono); font-size: 9px; letter-spacing: .05em; }
.ask-text { margin: 0 0 8px; color: var(--fg); font-size: 12.5px; line-height: 1.6; }
.ask-options { display: flex; flex-wrap: wrap; gap: 7px; }
.ask-option { display: flex; max-width: 100%; flex-direction: column; gap: 1px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); color: var(--fg); cursor: pointer; font: inherit; text-align: left; transition: border-color 130ms ease, background 130ms ease, color 130ms ease; }
.ask-option strong { font-size: 11.5px; font-weight: 600; }
.ask-option small { color: var(--meta); font-size: 9.5px; line-height: 1.45; }
.ask-option:hover:not(:disabled) { border-color: color-mix(in oklch, var(--accent) 45%, var(--border)); }
.ask-option[aria-pressed="true"] { border-color: color-mix(in oklch, var(--accent) 60%, var(--border)); background: color-mix(in oklch, var(--accent) 8%, var(--bg)); color: var(--accent); }
.ask-option:disabled { cursor: not-allowed; opacity: .55; }

.ask-free { display: flex; gap: 7px; margin-top: 12px; }
.ask-free input { min-width: 0; flex: 1; padding: 7px 9px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); color: var(--fg); font: inherit; font-size: 11.5px; }
.ask-free input:focus { border-color: var(--accent); outline: 0; }
.ask-submit { flex: none; padding: 7px 11px; border: 1px solid color-mix(in oklch, var(--accent) 45%, var(--border)); border-radius: 9px; background: var(--accent); color: #fff; cursor: pointer; font: inherit; font-size: 11px; font-weight: 600; }
.ask-submit:disabled { cursor: not-allowed; opacity: .45; }

.ask-answer { display: flex; gap: 8px; margin-top: 11px; padding-top: 10px; border-top: 1px solid var(--border-soft); color: var(--muted); font-size: 11.5px; line-height: 1.6; }
.ask-answer-label { flex: none; color: var(--meta); font-family: var(--font-mono); font-size: 9px; }
</style>
