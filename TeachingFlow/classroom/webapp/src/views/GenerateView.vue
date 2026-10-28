<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import ImportCourseModal from '@/components/ImportCourseModal.vue'
import { api } from '@/api/client'
import { errorText } from '@/api/format'
import { toast } from '@/state/toast'

/**
 * 生成课堂。
 *
 * 旧仓库这一页是纯前端模拟：定时器「生成」一份大纲，再存进 localStorage。
 * 本仓库没有「对话即出片」的 HTTP 接口 —— 出片是 TeachingFlow/flow.py 的活 ——
 * 所以保留旧对话体验（示例大纲、课程偏好、发送/中断），把**结果区**换成三条真实链路：
 *   1. 复制出片命令（带上用户这句话）→ 在 TeachingFlow 下执行；
 *   2. 导入现成 .pptx（POST /courses/import）；
 *   3. 出片/导入完成后重新扫描（POST /courses/scan）→ 进课程库。
 * 示例大纲只是预览，不保存、不冒充真实课程；偏好面板也只影响示例。
 */
type Message = {
  id: number
  role: 'assistant' | 'user'
  note?: string
  text: string
  outline?: Array<{ title: string; lessons: string[] }>
  files?: string[]
}

interface SampleUnit {
  title: string
  lessons: string[]
}

const router = useRouter()
const chatLog = ref<HTMLElement | null>(null)
const draft = ref('')
const isBusy = ref(false)
const showSettings = ref(false)
const generatedTitle = ref('')
const generatedGoal = ref('')
const messages = ref<Message[]>([])
const selectedModel = ref('智塾 3 Pro')
const selectedCycle = ref<'short' | 'long'>('long')
const selectedLevel = ref<'beginner' | 'intermediate'>('beginner')
const showImport = ref(false)
const scanning = ref(false)

const models = [
  { name: '智塾 3 Pro', description: '适合课程规划' },
  { name: '通用推理模型', description: '适合复杂主题' },
  { name: '轻量模型', description: '响应更快' },
]

let nextMessageId = 1
let timers: number[] = []
let disposed = false

function setTimer(callback: () => void, delay: number) {
  const timer = window.setTimeout(() => {
    timers = timers.filter((pending) => pending !== timer)
    if (!disposed) callback()
  }, delay)
  timers.push(timer)
}

function scrollChatToBottom() {
  void nextTick(() => {
    if (chatLog.value) chatLog.value.scrollTop = chatLog.value.scrollHeight
  })
}

function makeOutline(goal: string): { title: string; units: SampleUnit[] } {
  const firstClause = goal.split(/[，。；;、]/)[0]?.trim() || goal.trim()
  const title = firstClause.length > 26 ? `${firstClause.slice(0, 26)}…` : firstClause
  const topic = title.replace(/^(我想(要)?|我希望|请帮我|帮我|想要|想学)/, '').trim() || title

  const unitTitles = selectedCycle.value === 'short'
    ? [
        `${topic}：基础概念与核心方法`,
        `${topic}：典型案例与专项练习`,
        `${topic}：综合复习与阶段测验`,
      ]
    : [
        `${topic}：基础概念与学习准备`,
        `${topic}：核心方法与关键知识`,
        `${topic}：典型案例与专项练习`,
        `${topic}：综合复习与阶段测验`,
      ]

  return {
    title: topic,
    units: unitTitles.map((unitTitle) => ({
      title: unitTitle,
      lessons: ['核心概念与基本原理', '典型例题与解题方法', '知识应用与巩固练习'],
    })),
  }
}

function submitGoal(value = draft.value) {
  const goal = value.trim()
  if (!goal || isBusy.value) return

  draft.value = ''
  isBusy.value = true
  messages.value.push({ id: nextMessageId++, role: 'user', text: goal })
  scrollChatToBottom()

  setTimer(() => {
    const sample = makeOutline(goal)
    generatedTitle.value = sample.title
    generatedGoal.value = goal
    messages.value.push({
      id: nextMessageId++,
      role: 'assistant',
      note: '教师 · 示例大纲',
      text: `先根据你的目标整理「${sample.title}」的示例大纲（仅用于预览结构）。真实课件要由出片流水线或导入 pptx 产生，完成后回课程库就能上课。`,
      outline: sample.units,
    })
    scrollChatToBottom()

    setTimer(() => {
      isBusy.value = false
      messages.value.push({
        id: nextMessageId++,
        role: 'assistant',
        note: '接下来怎么拿到真实课件',
        text: '这一步在 MAT 主流程里完成。选一条路径：让 TeachingFlow 出片，或直接导入一份现成的 .pptx。',
        files: ['课程大纲.md', '课程课件.pptx', '课件说明.md'],
      })
      scrollChatToBottom()
    }, 1250)
  }, 650)
}

function clearConversation() {
  if (isBusy.value) return
  messages.value = []
  generatedTitle.value = ''
  generatedGoal.value = ''
  showSettings.value = false
  draft.value = ''
  nextMessageId = 1
}

function produceCommand(): string {
  const goal = (generatedGoal.value || draft.value || '我想系统学习一门课').replace(/"/g, '\\"')
  return `cd TeachingFlow\npython flow.py "${goal}"`
}

async function copyCommand() {
  try {
    await navigator.clipboard.writeText(produceCommand())
    toast.ok('出片命令已复制：到 TeachingFlow 目录粘贴执行')
  } catch {
    toast.warn('复制失败：请手动选中命令文本复制')
  }
}

async function rescan() {
  if (scanning.value) return
  scanning.value = true
  try {
    const result = await api.scanCourses()
    if (result.total === 0) {
      toast.warn('还没扫描到出片产物：先跑一次 flow.py')
    } else {
      toast.ok(`扫描完成：新增 ${result.added}、更新 ${result.updated}，共 ${result.total} 门`)
      void router.push({ name: 'app-courses' })
    }
  } catch (error) {
    toast.error(errorText(error))
  } finally {
    scanning.value = false
  }
}

function openImported(courseId: string) {
  void router.push({ name: 'app-course-detail', params: { id: courseId } })
}

onBeforeUnmount(() => {
  disposed = true
  timers.forEach((timer) => window.clearTimeout(timer))
})
</script>

<template>
  <section class="generate-workspace" aria-label="生成课堂">
    <header class="conversation-header">
      <button class="new-chat-button" type="button" :disabled="isBusy" @click="clearConversation">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        <span>新对话</span>
      </button>
    </header>

    <div ref="chatLog" class="conversation-scroll" :class="{ 'conversation-scroll--empty': !messages.length }" aria-live="polite">
      <div v-if="!messages.length" class="welcome-state">
        <span class="welcome-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12 3.5 13.8 10l6.7 2-6.7 2-1.8 6.5L10.2 14l-6.7-2 6.7-2L12 3.5Z" /></svg>
        </span>
        <h1>今天想学点什么？</h1>
        <p>告诉我学习主题、目标和时间安排。我会先把结构整理给你看，再告诉你去哪里把它变成真实课件。</p>
        <div class="welcome-actions">
          <button class="welcome-action" type="button" @click="showImport = true">
            <span class="welcome-action-k">A</span>
            <span><strong>导入现成课件</strong><small>已有 .pptx，直接进课程库并备课</small></span>
          </button>
          <button class="welcome-action" type="button" @click="copyCommand">
            <span class="welcome-action-k">B</span>
            <span><strong>复制出片命令</strong><small>让 TeachingFlow 从一句话产出整套课件</small></span>
          </button>
        </div>
      </div>

      <div v-else class="message-list">
        <article v-for="message in messages" :key="message.id" class="chat-message" :class="`chat-message--${message.role}`">
          <span v-if="message.role === 'assistant'" class="message-mark" aria-hidden="true">师</span>
          <div class="message-content">
            <div v-if="message.role === 'assistant' && message.note" class="message-label">{{ message.note }}</div>
            <p class="message-text">{{ message.text }}</p>

            <section v-if="message.outline" class="outline-card" aria-label="示例课程大纲">
              <div class="outline-heading">
                <strong>示例大纲</strong>
                <span>{{ message.outline.length }} 个单元 · {{ message.outline.reduce((total, unit) => total + unit.lessons.length, 0) }} 个小节</span>
              </div>
              <ol>
                <li v-for="(unit, index) in message.outline" :key="unit.title">
                  <div class="outline-unit-heading">
                    <span class="unit-number">{{ String(index + 1).padStart(2, '0') }}</span>
                    <strong>{{ unit.title }}</strong>
                  </div>
                  <ol class="outline-lesson-list">
                    <li v-for="(lesson, lessonIndex) in unit.lessons" :key="`${unit.title}-${lesson}`">
                      <span class="lesson-number">{{ index + 1 }}.{{ lessonIndex + 1 }}</span>
                      <span>{{ lesson }}</span>
                    </li>
                  </ol>
                </li>
              </ol>
              <p class="outline-footnote">示例结构不会保存；真实课程由出片产物或导入课件生成。</p>
            </section>

            <div v-if="message.files" class="result-card">
              <span class="result-heading">拿到真实课件</span>
              <div class="file-list">
                <span v-for="file in message.files" :key="file" class="file-chip">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3.75h8l4 4v12.5H6zM14 3.75v4h4M9 13h6M9 16.5h6" /></svg>
                  {{ file }}
                </span>
              </div>
              <pre class="command-box">{{ produceCommand() }}</pre>
              <div class="result-actions">
                <button class="save-course-button" type="button" @click="copyCommand">复制出片命令</button>
                <button class="result-secondary" type="button" @click="showImport = true">导入现成课件</button>
                <button class="result-secondary" type="button" :disabled="scanning" @click="rescan">
                  {{ scanning ? '扫描中…' : '重新扫描' }}
                </button>
              </div>
            </div>
          </div>
        </article>

        <div v-if="isBusy" class="thinking-indicator" role="status">
          <span class="message-mark" aria-hidden="true">师</span>
          <span class="thinking-dots"><i /><i /><i /></span>
          <span>{{ generatedTitle ? '正在整理下一步…' : '正在整理示例大纲…' }}</span>
        </div>
      </div>
    </div>

    <footer class="composer-area">
      <section v-if="showSettings" class="settings-panel" aria-label="课程设置">
        <div class="settings-panel-header">
          <strong>课程偏好 <span class="settings-scope">（只影响示例大纲）</span></strong>
          <span>可随时调整</span>
        </div>
        <div class="setting-row">
          <span class="setting-name">模型</span>
          <div class="setting-choices">
            <button v-for="model in models" :key="model.name" class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedModel === model.name" @click="selectedModel = model.name">
              {{ model.name }}
            </button>
          </div>
        </div>
        <div class="setting-row">
          <span class="setting-name">学习周期</span>
          <div class="setting-choices">
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedCycle === 'short'" @click="selectedCycle = 'short'">短期计划</button>
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedCycle === 'long'" @click="selectedCycle = 'long'">长期计划</button>
          </div>
        </div>
        <div class="setting-row">
          <span class="setting-name">起点</span>
          <div class="setting-choices">
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedLevel === 'beginner'" @click="selectedLevel = 'beginner'">零基础</button>
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedLevel === 'intermediate'" @click="selectedLevel = 'intermediate'">有基础</button>
          </div>
        </div>
      </section>

      <div class="composer-box">
        <textarea
          v-model="draft"
          rows="2"
          :disabled="isBusy"
          placeholder="描述你想学习的主题、目标和时间安排…"
          aria-label="描述课程学习目标"
          @keydown.enter.exact.prevent="submitGoal()"
        />
        <div class="composer-toolbar">
          <button class="settings-toggle" type="button" :aria-expanded="showSettings" :disabled="isBusy" @click="showSettings = !showSettings">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h9M17 7h3M4 17h3m4 0h9M13 4v6M7 14v6" /></svg>
            <span>课程偏好</span>
            <span class="selected-model">{{ selectedModel }}</span>
          </button>
          <div class="composer-submit-group">
            <span class="composer-hint">Enter 发送 · Shift + Enter 换行</span>
            <button class="send-button" type="button" :disabled="isBusy || !draft.trim()" :aria-label="isBusy ? '正在整理' : '发送消息'" @click="submitGoal()">
              <svg v-if="!isBusy" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-7 7 7-7 7 7" /></svg>
              <span v-else class="send-spinner" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>
      <p class="composer-disclaimer">出片：在 TeachingFlow 下执行命令；导入：右上角「导入课件」。完成后回「我的课堂」即可上课。</p>
    </footer>

    <ImportCourseModal v-if="showImport" @close="showImport = false" @imported="openImported" />
  </section>
</template>

<style scoped>
.generate-workspace {
  display: flex;
  width: min(100%, 900px);
  height: calc(100dvh - 142px);
  min-height: 500px;
  flex-direction: column;
  margin-inline: auto;
  color: var(--fg);
}

.conversation-header {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding: 0 0 10px;
}
.message-mark {
  display: grid;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  border: 1px solid color-mix(in oklch, var(--accent) 25%, var(--border));
  border-radius: 12px;
  background: color-mix(in oklch, var(--accent) 9%, var(--surface));
  color: var(--accent);
  font-size: 14px;
  font-weight: 650;
}
.new-chat-button, .settings-toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  transition: border-color 140ms ease, color 140ms ease, background 140ms ease;
}
.new-chat-button { padding: 8px 11px; font: inherit; font-size: 12px; }
.new-chat-button:hover, .settings-toggle:hover { border-color: var(--accent); color: var(--fg); }
.new-chat-button:disabled, .settings-toggle:disabled { cursor: not-allowed; opacity: .5; }
.new-chat-button svg, .settings-toggle svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }

.conversation-scroll { min-height: 0; flex: 1; overflow-y: auto; overscroll-behavior: contain; scrollbar-color: var(--border) transparent; scrollbar-width: thin; }
.conversation-scroll--empty { display: grid; place-items: center; }
.welcome-state { width: min(100%, 620px); padding: 36px 12px 44px; text-align: center; }
.welcome-mark {
  display: inline-grid;
  width: 54px;
  height: 54px;
  place-items: center;
  border: 1px solid color-mix(in oklch, var(--accent) 22%, var(--border));
  border-radius: 19px;
  background: color-mix(in oklch, var(--accent) 8%, var(--surface));
  color: var(--accent);
}
.welcome-mark svg { width: 25px; height: 25px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.5; }
.welcome-state h1 { margin: 17px 0 8px; font-size: clamp(25px, 4vw, 32px); font-weight: 650; letter-spacing: -.035em; }
.welcome-state > p { max-width: 52ch; margin: 0 auto; color: var(--muted); font-size: 14px; line-height: 1.75; }
.welcome-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-top: 22px; text-align: left; }
.welcome-action {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: border-color 140ms ease, transform 140ms ease;
}
.welcome-action:hover { border-color: color-mix(in oklch, var(--accent) 35%, var(--border)); transform: translateY(-1px); }
.welcome-action-k { display: grid; width: 22px; height: 22px; flex: none; place-items: center; border-radius: 7px; background: color-mix(in oklch, var(--accent) 10%, var(--bg)); color: var(--accent); font-family: var(--font-mono); font-size: 10px; }
.welcome-action strong { display: block; font-size: 12px; font-weight: 600; }
.welcome-action small { display: block; margin-top: 3px; color: var(--meta); font-size: 10px; line-height: 1.5; }

.message-list { display: flex; flex-direction: column; gap: 28px; padding: 28px 0 36px; }
.chat-message { display: flex; align-items: flex-start; gap: 11px; }
.chat-message--user { justify-content: flex-end; }
.message-content { min-width: 0; max-width: min(100%, 720px); }
.chat-message--assistant .message-content { flex: 1; }
.chat-message--user .message-content { max-width: min(82%, 650px); padding: 12px 16px; border: 1px solid var(--border-soft); border-radius: 18px 18px 5px 18px; background: color-mix(in oklch, var(--surface) 78%, var(--border-soft)); }
.message-mark { width: 29px; height: 29px; border-radius: 10px; font-size: 12px; }
.message-label { margin: 1px 0 8px; color: var(--muted); font-size: 12px; font-weight: 600; }
.message-text { margin: 0; color: var(--fg); font-size: 14px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.outline-card, .result-card { margin-top: 15px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); }
.outline-card { padding: 15px 17px; }
.outline-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding-bottom: 12px; border-bottom: 1px solid var(--border-soft); }
.outline-heading strong, .result-heading { font-size: 13px; font-weight: 650; }
.outline-heading > span { color: var(--muted); font-size: 11px; }
.outline-card > ol { display: flex; flex-direction: column; gap: 2px; margin: 9px 0 0; padding: 0; list-style: none; }
.outline-card > ol > li { padding: 10px 0; color: var(--muted); font-size: 13px; line-height: 1.55; }
.outline-unit-heading { display: flex; align-items: baseline; gap: 12px; }
.outline-unit-heading strong { color: var(--fg); font-size: 12px; font-weight: 600; }
.outline-lesson-list { display: flex; flex-direction: column; gap: 3px; margin: 6px 0 0 28px; padding: 0; list-style: none; }
.outline-lesson-list li { display: flex; align-items: baseline; gap: 9px; padding: 3px 0; color: var(--muted); font-size: 11px; line-height: 1.5; }
.lesson-number { flex: none; color: var(--meta); font-family: var(--font-mono); font-size: 9px; }
.unit-number { flex: none; color: var(--accent); font-family: var(--font-mono); font-size: 10px; }
.outline-footnote { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border-soft); color: var(--meta); font-size: 10px; }
.result-card { padding: 14px 15px; }
.file-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 11px; }
.file-chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 7px 9px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg); color: var(--muted); font-family: var(--font-mono); font-size: 10px; overflow-wrap: anywhere; }
.file-chip svg { width: 14px; height: 14px; flex: none; fill: none; stroke: var(--accent); stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.5; }
.command-box { margin: 11px 0 0; padding: 10px 12px; overflow-x: auto; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); color: var(--fg); font-family: var(--font-mono); font-size: 10px; line-height: 1.7; white-space: pre-wrap; }
.result-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.save-course-button { padding: 8px 12px; border: 0; border-radius: 9px; background: var(--accent); color: white; cursor: pointer; font: inherit; font-size: 12px; font-weight: 600; }
.save-course-button:hover { filter: brightness(.96); }
.result-secondary { padding: 7px 11px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--muted); cursor: pointer; font: inherit; font-size: 11px; }
.result-secondary:hover { border-color: var(--accent); color: var(--accent); }
.result-secondary:disabled { cursor: not-allowed; opacity: .5; }
.thinking-indicator { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }
.thinking-dots { display: flex; gap: 3px; }
.thinking-dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--accent); animation: thinking 1s infinite ease-in-out; }
.thinking-dots i:nth-child(2) { animation-delay: .14s; }
.thinking-dots i:nth-child(3) { animation-delay: .28s; }
@keyframes thinking { 0%, 60%, 100% { opacity: .32; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-3px); } }

.composer-area { position: relative; z-index: 2; flex: none; padding-top: 12px; background: linear-gradient(180deg, transparent, var(--bg) 18%); }
.composer-box { padding: 12px 13px 10px; border: 1px solid var(--border); border-radius: 17px; background: var(--surface); box-shadow: 0 8px 28px color-mix(in oklch, var(--fg) 5%, transparent); }
.composer-box:focus-within { border-color: color-mix(in oklch, var(--accent) 48%, var(--border)); box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent) 9%, transparent); }
.composer-box textarea { display: block; width: 100%; min-height: 52px; max-height: 160px; resize: vertical; border: 0; outline: 0; background: transparent; color: var(--fg); font: inherit; font-size: 14px; line-height: 1.6; }
.composer-box textarea::placeholder { color: var(--meta); }
.composer-box textarea:disabled { opacity: .65; }
.composer-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 8px; }
.settings-toggle { min-width: 0; padding: 7px 9px; font: inherit; font-size: 11px; }
.settings-toggle[aria-expanded="true"] { border-color: color-mix(in oklch, var(--accent) 42%, var(--border)); color: var(--accent); }
.selected-model { overflow: hidden; max-width: 120px; padding-left: 7px; border-left: 1px solid var(--border); color: var(--meta); text-overflow: ellipsis; white-space: nowrap; }
.composer-submit-group { display: flex; align-items: center; gap: 12px; }
.composer-hint, .composer-disclaimer { color: var(--meta); font-size: 10px; }
.send-button { display: grid; width: 34px; height: 34px; flex: none; place-items: center; border: 0; border-radius: 11px; background: var(--accent); color: #fff; cursor: pointer; transition: opacity 120ms ease, transform 120ms ease; }
.send-button:hover:not(:disabled) { transform: translateY(-1px); }
.send-button:disabled { cursor: not-allowed; opacity: .38; }
.send-button svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }
.send-spinner { width: 15px; height: 15px; border: 2px solid rgb(255 255 255 / 38%); border-top-color: #fff; border-radius: 50%; animation: spin .75s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.composer-disclaimer { margin: 8px 0 0; text-align: center; }

.settings-panel { margin-bottom: 10px; padding: 15px 16px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); box-shadow: 0 8px 28px color-mix(in oklch, var(--fg) 5%, transparent); }
.settings-panel-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.settings-panel-header strong { font-size: 12px; }
.settings-scope { color: var(--meta); font-size: 10px; font-weight: 400; }
.settings-panel-header span { color: var(--meta); font-size: 10px; }
.setting-row { display: grid; grid-template-columns: 78px minmax(0, 1fr); gap: 12px; align-items: center; padding: 8px 0; }
.setting-row + .setting-row { border-top: 1px solid var(--border-soft); }
.setting-name { color: var(--muted); font-size: 11px; }
.setting-choices { display: flex; flex-wrap: wrap; gap: 7px; }
.setting-choice { padding: 6px 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--muted); cursor: pointer; font: inherit; font-size: 10px; transition: border-color 140ms ease, color 140ms ease; }
.setting-choice:hover:not(:disabled) { border-color: var(--accent); color: var(--fg); }
.setting-choice[aria-pressed="true"] { border-color: color-mix(in oklch, var(--accent) 55%, var(--border)); background: color-mix(in oklch, var(--accent) 7%, var(--surface)); color: var(--accent); }
.setting-choice:disabled { cursor: not-allowed; opacity: .55; }

@media (max-width: 760px) {
  .generate-workspace { height: calc(100dvh - 128px); min-height: 470px; }
  .conversation-header { padding-bottom: 12px; }
  .welcome-state { padding-inline: 2px; }
  .welcome-actions { grid-template-columns: minmax(0, 1fr); }
  .message-list { gap: 22px; padding-top: 20px; }
  .chat-message--user .message-content { max-width: 90%; }
  .composer-hint { display: none; }
  .setting-row { grid-template-columns: 65px minmax(0, 1fr); gap: 8px; }
}

@media (max-height: 720px) and (min-width: 761px) {
  .generate-workspace { height: calc(100dvh - 120px); min-height: 420px; }
  .welcome-state { padding-block: 16px 20px; }
}
</style>
