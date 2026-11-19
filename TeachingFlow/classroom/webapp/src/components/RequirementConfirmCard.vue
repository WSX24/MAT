<script setup lang="ts">
import { computed, ref } from 'vue'
import type { BuilderConfirmTool } from '@/api/types'

/**
 * 需求确认卡（工具 confirm_course_requirements 的落地）。
 *
 * 这是整条流程的**闸门**：确认之前不排课。所以卡片必须把"模型到底理解成了什么"
 * 摊开给人看（主题 / 程度 / 基础 / 周期 / 重点），而不是只写一句"这样对吗？"——
 * 用户点"符合"时要知道自己在同意什么。
 *
 * 两个出口：
 *   符合 → 开始生成课程大纲
 *   不符合 → 展开一个输入框收一句具体意见，然后回到对话继续细化
 */
const props = defineProps<{
  tool: BuilderConfirmTool
  disabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'accept'): void
  (e: 'reject', feedback: string): void
}>()

const rejecting = ref(false)
const feedback = ref('')

const decided = computed(() => props.tool.decision)
const req = computed(() => props.tool.requirements)

function accept() {
  if (props.disabled || decided.value) return
  emit('accept')
}

function reject() {
  if (props.disabled || decided.value) return
  if (!rejecting.value) {
    rejecting.value = true
    return
  }
  emit('reject', feedback.value.trim() || '这份不太对，我们再细化一下')
}
</script>

<template>
  <div class="req-card" :class="decided ? 'req-card--decided' : ''">
    <div class="req-grid">
      <div class="req-cell">
        <span class="req-key">课程</span>
        <span class="req-value">{{ req.course_title }}</span>
      </div>
      <div class="req-cell">
        <span class="req-key">学科 / 学段</span>
        <span class="req-value">{{ req.subject }} · {{ req.stage }}</span>
      </div>
      <div class="req-cell">
        <span class="req-key">起点</span>
        <span class="req-value">{{ req.student_level }}<template v-if="req.prerequisite">（{{ req.prerequisite }}）</template></span>
      </div>
      <div class="req-cell">
        <span class="req-key">周期</span>
        <span class="req-value">{{ req.weeks }} 周 × 每周 {{ req.hours_per_week }} 课时</span>
      </div>
      <div class="req-cell req-cell--wide">
        <span class="req-key">学习目标</span>
        <span class="req-value">{{ req.goal }}</span>
      </div>
      <div v-if="req.key_topics.length" class="req-cell req-cell--wide">
        <span class="req-key">重点内容</span>
        <span class="req-topics">
          <span v-for="topic in req.key_topics" :key="topic" class="req-topic">{{ topic }}</span>
        </span>
      </div>
    </div>

    <p v-if="decided === 'accepted'" class="req-state req-state--ok">
      ✓ 已确认 — 正在按这份需求生成课程大纲
    </p>
    <p v-else-if="decided === 'rejected'" class="req-state">
      已说明不符，回到对话继续细化
    </p>

    <form v-else-if="rejecting" class="req-feedback" @submit.prevent="reject()">
      <textarea
        v-model="feedback"
        rows="2"
        :disabled="disabled"
        placeholder="哪里不对？例如「我想学到能做题，不是只了解」「周期太长了，只有两周」"
        aria-label="不符合的地方"
      />
      <div class="req-actions">
        <button class="req-btn req-btn--primary" type="submit" :disabled="disabled">提交并继续细化</button>
        <button class="req-btn" type="button" :disabled="disabled" @click="rejecting = false">返回</button>
      </div>
    </form>

    <div v-else class="req-actions">
      <button class="req-btn req-btn--primary" type="button" :disabled="disabled" @click="accept()">
        符合，开始生成课程大纲
      </button>
      <button class="req-btn" type="button" :disabled="disabled" @click="reject()">
        不符合，再细化一下
      </button>
    </div>
  </div>
</template>

<style scoped>
.req-card { margin-top: 10px; padding: 14px 15px; border: 1px solid color-mix(in oklch, var(--accent) 22%, var(--border)); border-radius: 13px; background: var(--bg); }
.req-card--decided { border-color: var(--border); background: var(--surface); }
.req-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.req-cell { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.req-cell--wide { grid-column: 1 / -1; }
.req-key { color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .05em; }
.req-value { color: var(--fg); font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.req-topics { display: flex; flex-wrap: wrap; gap: 5px; }
.req-topic { padding: 3px 8px; border: 1px solid color-mix(in oklch, var(--accent) 22%, var(--border)); border-radius: 999px; background: color-mix(in oklch, var(--accent) 6%, var(--bg)); color: var(--accent); font-size: 10px; }

.req-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 13px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
.req-btn { padding: 8px 13px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--muted); cursor: pointer; font: inherit; font-size: 11.5px; transition: border-color 130ms ease, color 130ms ease, background 130ms ease; }
.req-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.req-btn--primary { border-color: transparent; background: var(--accent); color: #fff; font-weight: 600; }
.req-btn--primary:hover:not(:disabled) { background: var(--accent-hover); color: #fff; }
.req-btn:disabled { cursor: not-allowed; opacity: .5; }

.req-feedback { display: flex; flex-direction: column; gap: 8px; margin-top: 13px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
.req-feedback textarea { width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--fg); font: inherit; font-size: 11.5px; line-height: 1.6; resize: vertical; }
.req-feedback textarea:focus { border-color: var(--accent); outline: 0; }
.req-feedback .req-actions { margin-top: 0; padding-top: 0; border-top: 0; }

.req-state { margin: 12px 0 0; padding-top: 11px; border-top: 1px solid var(--border-soft); color: var(--muted); font-size: 11px; }
.req-state--ok { color: var(--accent); }

@media (max-width: 640px) {
  .req-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
