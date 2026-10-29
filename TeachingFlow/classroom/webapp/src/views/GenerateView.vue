<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import AskCard from '@/components/AskCard.vue'
import CourseOutlineEditor from '@/components/CourseOutlineEditor.vue'
import ImportCourseModal from '@/components/ImportCourseModal.vue'
import RequirementConfirmCard from '@/components/RequirementConfirmCard.vue'
import { ApiError, api } from '@/api/client'
import { errorText } from '@/api/format'
import type {
  BuilderAskTool,
  BuilderConfirmTool,
  BuilderMessage,
  BuilderSession,
  CourseOutline,
} from '@/api/types'
import { toast } from '@/state/toast'

/**
 * 生成课堂 = **真的和模型聊**，不是填几个参数就出片。
 *
 * 整条流程（也是页面的五个状态）：
 *
 *   clarifying            模型提问：想学到什么程度 / 有什么基础 / 学多久 / 重点是什么
 *   confirming_requirements 模型把理解摊开请你确认（不符合就继续说）
 *   generating_outline    确认后才排章 → 单元（一次模型调用，几十秒）
 *   editing_outline       大纲给你改（改标题、增删单元、调顺序），改完手动确认
 *   generating_course     逐章生成单元课程安排设计（学生看不到），然后存入「我的课堂」
 *   ready                 课程已入库 → 去课程页一课一课生成 PPT
 *
 * 前端只做三件事：把用户的话发过去、把服务端快照画出来、把工具调用渲染成卡片。
 * 所有判定（能不能确认、大纲合不合法、课程有没有入库）都在服务端 —— 页面不猜。
 */

const POLL_MS = 1200
const STORAGE_KEY = 'zhishu:builder-session'

const router = useRouter()
const chatLog = ref<HTMLElement | null>(null)
const draft = ref('')
const session = ref<BuilderSession | null>(null)
const draftOutline = ref<CourseOutline | null>(null)
const sending = ref(false)
const confirming = ref(false)
const outlineBusy = ref(false)
const booting = ref(true)
const errorMessage = ref('')
const showImport = ref(false)
const showAllLog = ref(false)

let pollTimer: number | null = null
let disposed = false
/** 服务端大纲的指纹：只有它变了才重建本地草稿（否则轮询会把用户正在改的内容冲掉）。 */
let outlineFingerprint = ''
let lastPhase = ''

const messages = computed<BuilderMessage[]>(() => session.value?.messages ?? [])
const phase = computed(() => session.value?.phase ?? 'clarifying')

/** 阶段文案：只用于页脚那一行状态，不做任何判定。 */
const PHASE_LABELS: Record<string, string> = {
  clarifying: '正在了解你的需求',
  confirming_requirements: '等你确认需求',
  generating_outline: '正在生成课程大纲',
  editing_outline: '大纲待你确认',
  generating_course: '正在编排每一课',
  ready: '课程已建好',
}
const phaseLabel = computed(() => PHASE_LABELS[phase.value] ?? '进行中')
const task = computed(() => session.value?.task ?? null)
const llm = computed(() => session.value?.llm ?? null)
const courseId = computed(() => session.value?.course_id ?? '')

const generating = computed(
  () => phase.value === 'generating_outline' || phase.value === 'generating_course',
)
const awaitingOutline = computed(() => phase.value === 'editing_outline' && !!draftOutline.value)
const finished = computed(() => phase.value === 'ready')

/** 什么时候可以打字：正在跑后台任务时一律不许（后端也会 409，但按钮该先灰）。 */
const composerLocked = computed(() => generating.value || sending.value || finished.value)
const busy = computed(() => sending.value || confirming.value || outlineBusy.value || generating.value)

const draftDirty = computed(() => {
  if (!draftOutline.value) return false
  return JSON.stringify(draftOutline.value) !== outlineFingerprint
})

const placeholder = computed(() => {
  if (finished.value) return '这门课已经建好了 — 想再建一门，点右上角「新对话」'
  if (generating.value) return runningLabel.value
  if (awaitingOutline.value) return '想改需求就继续说；想改大纲请直接编辑上面的章节'
  if (phase.value === 'confirming_requirements') return '对上面这份有异议就直说，没问题就点「符合」'
  return '说说你想学什么，例如「我想学习信息安全」'
})

const runningLabel = computed(() => {
  const current = task.value
  if (current && current.status === 'running') {
    return current.kind === 'design' ? '正在编排每一课的知识点与安排…' : '正在生成课程大纲…'
  }
  return '正在生成…'
})

const progressPercent = computed(() => {
  const current = task.value
  if (!current) return 0
  if (current.status === 'succeeded') return 100
  return Math.max(0, Math.min(100, Math.round(current.percent || 0)))
})

const progressLines = computed(() => {
  const lines = task.value?.log ?? []
  return showAllLog.value ? lines.slice(-40) : lines.slice(-3)
})

const unitSummary = computed(() => {
  const outline = session.value?.outline
  if (!outline) return ''
  return `${outline.chapter_count} 章 · ${outline.unit_count} 个单元`
})

function stopPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

function needsPoll(): boolean {
  const current = session.value
  if (!current) return false
  return (current.phase === 'generating_outline' || current.phase === 'generating_course'
    || current.task?.status === 'running')
}

function schedulePoll() {
  stopPolling()
  if (disposed) return
  pollTimer = window.setTimeout(() => { void poll() }, POLL_MS)
}

async function poll() {
  pollTimer = null
  if (disposed || !session.value) return
  try {
    applySession(await api.getBuilderSession(session.value.id))
  } catch (error) {
    // 网络抖动不该把面板定在旧值上：先把原因说出来，下一轮继续拉
    errorMessage.value = errorText(error)
  }
  if (needsPoll()) schedulePoll()
}

function applySession(next: BuilderSession) {
  const previousPhase = lastPhase
  session.value = next
  lastPhase = next.phase
  errorMessage.value = ''

  const fingerprint = JSON.stringify(next.outline ?? null)
  if (fingerprint !== outlineFingerprint) {
    outlineFingerprint = fingerprint
    draftOutline.value = next.outline ? JSON.parse(fingerprint) : null
  }

  if (needsPoll()) schedulePoll()
  else stopPolling()

  if (previousPhase !== next.phase) {
    if (next.phase === 'editing_outline') toast.ok(`大纲排好了：${unitSummary.value || '可以开始编辑'}`)
    if (next.phase === 'ready') toast.ok('课程已存入「我的课堂」')
    if (next.task?.status === 'failed' && next.task.error) toast.error(next.task.error)
  }
  scrollToBottom()
}

function scrollToBottom() {
  void nextTick(() => {
    if (chatLog.value) chatLog.value.scrollTop = chatLog.value.scrollHeight
  })
}

// ---------------------------------------------------------------- 会话

async function restore() {
  booting.value = true
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY)
    if (saved) {
      try {
        applySession(await api.getBuilderSession(saved))
        return
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          window.localStorage.removeItem(STORAGE_KEY)
        } else {
          throw error
        }
      }
    }
    const list = await api.listBuilderSessions(1)
    const recent = list.items[0]
    if (recent) {
      applySession(await api.getBuilderSession(recent.id))
    }
  } catch (error) {
    errorMessage.value = errorText(error)
  } finally {
    booting.value = false
  }
}

async function submit(text: string) {
  const content = text.trim()
  if (!content || composerLocked.value) return
  draft.value = ''
  sending.value = true
  errorMessage.value = ''
  try {
    if (!session.value) {
      const created = await api.createBuilderSession(content)
      window.localStorage.setItem(STORAGE_KEY, created.id)
      applySession(created)
    } else {
      applySession(await api.sendBuilderMessage(session.value.id, content))
    }
  } catch (error) {
    errorMessage.value = errorText(error)
    toast.error(errorText(error))
    draft.value = content
  } finally {
    sending.value = false
  }
}

function answerQuestion(text: string) {
  void submit(text)
}

async function acceptRequirements() {
  if (!session.value || confirming.value) return
  confirming.value = true
  try {
    applySession(await api.confirmBuilderRequirements(session.value.id, { accepted: true }))
  } catch (error) {
    errorMessage.value = errorText(error)
    toast.error(errorText(error))
  } finally {
    confirming.value = false
  }
}

async function rejectRequirements(feedback: string) {
  if (!session.value || confirming.value) return
  confirming.value = true
  try {
    applySession(await api.confirmBuilderRequirements(session.value.id,
      { accepted: false, feedback }))
  } catch (error) {
    errorMessage.value = errorText(error)
    toast.error(errorText(error))
  } finally {
    confirming.value = false
  }
}

function newConversation() {
  stopPolling()
  window.localStorage.removeItem(STORAGE_KEY)
  session.value = null
  draftOutline.value = null
  outlineFingerprint = ''
  lastPhase = ''
  draft.value = ''
  errorMessage.value = ''
  toast.ok('开了个新对话：上一门课还在「我的课堂」里')
}

// ---------------------------------------------------------------- 大纲

function outlineProblem(): string {
  const outline = draftOutline.value
  if (!outline) return '还没有大纲'
  if (!outline.chapters.length) return '至少要有一章'
  for (const [index, chapter] of outline.chapters.entries()) {
    if (!chapter.title.trim()) return `第 ${index + 1} 章还没有标题`
    if (!chapter.units.length) return `第 ${index + 1} 章至少要有一个单元`
    for (const [unitIndex, unit] of chapter.units.entries()) {
      if (!unit.title.trim()) return `第 ${index + 1}.${unitIndex + 1} 单元还没有标题`
    }
  }
  return ''
}

async function saveOutline(): Promise<boolean> {
  if (!session.value || !draftOutline.value || outlineBusy.value) return false
  const problem = outlineProblem()
  if (problem) {
    toast.warn(problem)
    return false
  }
  outlineBusy.value = true
  try {
    applySession(await api.saveBuilderOutline(session.value.id, draftOutline.value))
    toast.ok('大纲已保存')
    return true
  } catch (error) {
    errorMessage.value = errorText(error)
    toast.error(errorText(error))
    return false
  } finally {
    outlineBusy.value = false
  }
}

async function confirmOutline() {
  if (!session.value || !draftOutline.value || outlineBusy.value) return
  const problem = outlineProblem()
  if (problem) {
    toast.warn(problem)
    return
  }
  outlineBusy.value = true
  try {
    applySession(await api.confirmBuilderOutline(session.value.id, draftOutline.value))
  } catch (error) {
    errorMessage.value = errorText(error)
    toast.error(errorText(error))
  } finally {
    outlineBusy.value = false
  }
}

// ---------------------------------------------------------------- 课程

function openCourse(id: string) {
  if (!id) return
  void router.push({ name: 'app-course-detail', params: { id } })
}

async function generateFirstPpt() {
  const id = courseId.value
  if (!id) return
  try {
    await api.generateNextPpt(id)
    toast.ok('开始生成第一课 PPT：完成后就能直接上课')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      toast.warn(error.message)
    } else {
      toast.error(errorText(error))
    }
  }
  openCourse(id)
}

// ---------------------------------------------------------------- 渲染辅助

/** 消息上的工具卡：类型窄化在这里一次做完，模板里就不必和联合类型较劲。 */
function askTool(message: BuilderMessage): BuilderAskTool | null {
  const tool = message.tool
  return tool && tool.name === 'ask_user_question' ? tool : null
}

function confirmTool(message: BuilderMessage): BuilderConfirmTool | null {
  const tool = message.tool
  return tool && tool.name === 'confirm_course_requirements' ? tool : null
}

/** 只有最后一张卡可交互：历史卡片保持只读，避免"点了没反应还以为坏了"。 */
function isLive(messageId: number): boolean {
  const list = messages.value
  const last = list[list.length - 1]
  return last?.id === messageId && !busy.value
}

function submitDraft() {
  if (composerLocked.value) return
  void submit(draft.value)
}

function onEnter(event: KeyboardEvent) {
  if (event.shiftKey) return
  event.preventDefault()
  submitDraft()
}

watch(() => messages.value.length, () => scrollToBottom())

onMounted(() => { void restore() })
onBeforeUnmount(() => { disposed = true; stopPolling() })
</script>

<template>
  <section class="generate-workspace" aria-label="生成课堂">
    <header class="conversation-header">
      <div class="header-left">
        <span class="header-kicker">COURSE BUILDER</span>
        <span v-if="session" class="header-phase">{{ session.title || '新课程' }}</span>
      </div>
      <button class="new-chat-button" type="button" :disabled="busy" @click="newConversation()">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        <span>新对话</span>
      </button>
    </header>

    <div
      ref="chatLog"
      class="conversation-scroll"
      :class="{ 'conversation-scroll--empty': !messages.length && !session }"
      aria-live="polite"
    >
      <div v-if="booting" class="boot-state" role="status">
        <span class="loading-bar" aria-hidden="true" />
        <span>正在打开上次的课程规划…</span>
      </div>

      <div v-else-if="!messages.length && !session" class="welcome-state">
        <span class="welcome-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12 3.5 13.8 10l6.7 2-6.7 2-1.8 6.5L10.2 14l-6.7-2 6.7-2L12 3.5Z" /></svg>
        </span>
        <h1>今天想学点什么？</h1>
        <p>
          先说你的一句话目标（例如「我想学习信息安全」）。我会先问清楚你想学到什么程度、
          有什么基础、能投入多长时间，再把课程大纲排给你看 —— 你确认之后才会开始生成。
        </p>
        <div class="welcome-actions">
          <button class="welcome-action" type="button" @click="showImport = true">
            <span class="welcome-action-k">A</span>
            <span><strong>导入现成课件</strong><small>已有 .pptx，直接进课程库并备课</small></span>
          </button>
          <RouterLink class="welcome-action" :to="{ name: 'app-courses' }">
            <span class="welcome-action-k">B</span>
            <span><strong>回到我的课堂</strong><small>看已建好的课程与已生成的课件</small></span>
          </RouterLink>
        </div>
      </div>

      <div v-else class="message-list">
        <article
          v-for="message in messages"
          :key="message.id"
          class="chat-message"
          :class="[
            message.role === 'user' ? 'chat-message--user' : 'chat-message--assistant',
            message.kind === 'notice' ? 'chat-message--notice' : '',
          ]"
        >
          <span v-if="message.role !== 'user' && message.kind !== 'notice'" class="message-mark" aria-hidden="true">师</span>
          <div class="message-content">
            <p v-if="message.text" class="message-text">{{ message.text }}</p>
            <p v-else-if="message.role !== 'user' && !message.tool" class="message-text">（继续）</p>

            <!-- 大纲卡：生成好之后就地展示可编辑的章 / 单元树 -->
            <section v-if="message.kind === 'outline' && draftOutline" class="outline-card" aria-label="课程大纲">
              <header class="outline-card-head">
                <div>
                  <span class="card-kicker">COURSE OUTLINE</span>
                  <strong>课程大纲</strong>
                  <span class="outline-card-meta">{{ unitSummary }}</span>
                </div>
                <span v-if="awaitingOutline" class="outline-card-flag" :class="{ 'outline-card-flag--dirty': draftDirty }">
                  {{ draftDirty ? '有未保存的修改' : '已保存' }}
                </span>
                <span v-else class="outline-card-flag">已完成</span>
              </header>

              <CourseOutlineEditor
                :outline="draftOutline"
                :disabled="!isLive(message.id) || outlineBusy"
              />

              <div v-if="awaitingOutline" class="outline-card-actions">
                <button class="btn-solid" type="button" :disabled="outlineBusy" @click="confirmOutline()">
                  {{ outlineBusy ? '处理中…' : '确认大纲，开始编排课程' }}
                </button>
                <button class="btn-ghost" type="button" :disabled="outlineBusy || !draftDirty" @click="saveOutline()">
                  保存草稿
                </button>
                <span class="outline-hint">确认后我会逐章设计每一课的知识点与教学安排，然后存入「我的课堂」。</span>
              </div>
              <p v-else-if="finished" class="outline-hint outline-hint--done">
                这份大纲已经用于生成课程，可在课程详情页查看。
              </p>
            </section>

            <!-- 课程已入库卡 -->
            <section v-else-if="message.kind === 'course_ready'" class="ready-card" aria-label="课程已生成">
              <div class="ready-head">
                <span class="ready-mark" aria-hidden="true">✓</span>
                <div>
                  <strong>{{ session?.title || '课程' }} 已存入「我的课堂」</strong>
                  <span>{{ session?.unit_designs_count || 0 }} 个单元的课程设计已完成 · PPT 按课生成</span>
                </div>
              </div>
              <div class="ready-actions">
                <button class="btn-solid" type="button" @click="openCourse(courseId)">
                  去课程页生成 PPT
                </button>
                <button class="btn-ghost" type="button" @click="generateFirstPpt()">
                  直接生成第一课 PPT
                </button>
              </div>
            </section>

            <AskCard
              v-else-if="askTool(message)"
              :tool="askTool(message)!"
              :disabled="!isLive(message.id)"
              @answer="answerQuestion"
            />
            <RequirementConfirmCard
              v-else-if="confirmTool(message)"
              :tool="confirmTool(message)!"
              :disabled="!isLive(message.id)"
              @accept="acceptRequirements"
              @reject="rejectRequirements"
            />
          </div>
        </article>

        <!-- 后台任务进度：生成大纲 / 编排单元设计 -->
        <article v-if="generating" class="chat-message chat-message--assistant">
          <span class="message-mark" aria-hidden="true">师</span>
          <div class="message-content">
            <div class="task-card" aria-label="生成进度">
              <div class="task-head">
                <strong>{{ runningLabel }}</strong>
                <span class="task-percent">{{ progressPercent }}%</span>
              </div>
              <div class="task-progress" role="progressbar" :aria-valuenow="progressPercent" aria-valuemin="0" aria-valuemax="100">
                <i :style="{ width: progressPercent + '%' }" />
              </div>
              <p class="task-stage">{{ task?.stage || '准备中' }}</p>
              <pre v-if="progressLines.length" class="task-log">{{ progressLines.join('\n') }}</pre>
              <button class="task-more" type="button" @click="showAllLog = !showAllLog">
                {{ showAllLog ? '收起' : '看完整过程' }}
              </button>
            </div>
          </div>
        </article>

        <p v-if="errorMessage" class="inline-error" role="alert">{{ errorMessage }}</p>
      </div>
    </div>

    <footer class="composer-area">
      <div v-if="llm && !llm.ready" class="llm-note">
        {{ llm.reason }}
        <RouterLink :to="{ name: 'app-settings' }">去设置</RouterLink>
      </div>
      <div class="composer-box">
        <textarea
          v-model="draft"
          rows="2"
          :disabled="composerLocked"
          :placeholder="placeholder"
          aria-label="和课程规划师对话"
          @keydown.enter="onEnter"
        />
        <div class="composer-toolbar">
          <span class="composer-state">
            <template v-if="session">{{ phaseLabel }}</template>
            <template v-else>还没开始：先说一句你想学什么</template>
          </span>
          <div class="composer-submit-group">
            <span class="composer-hint">Enter 发送 · Shift + Enter 换行</span>
            <button
              class="send-button"
              type="button"
              :disabled="composerLocked || !draft.trim()"
              :aria-label="composerLocked ? '现在不能发送' : '发送'"
              @click="submitDraft()"
            >
              <svg v-if="!sending" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-7 7 7-7 7 7" /></svg>
              <span v-else class="send-spinner" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>
      <p class="composer-disclaimer">
        课程规划在课堂服务里跑，关掉这一页也会继续；大纲确认后才会存入课程库。
      </p>
    </footer>

    <ImportCourseModal v-if="showImport" @close="showImport = false" @imported="openCourse" />
  </section>
</template>

<style scoped>
.generate-workspace {
  display: flex;
  width: min(100%, 940px);
  height: calc(100dvh - 142px);
  min-height: 520px;
  flex-direction: column;
  margin-inline: auto;
  color: var(--fg);
}

.conversation-header { display: flex; flex: none; align-items: center; justify-content: space-between; gap: 16px; padding: 0 0 10px; }
.header-left { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.header-kicker { color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .08em; }
.header-phase { overflow: hidden; color: var(--fg); font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }

.new-chat-button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 8px 11px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  transition: border-color 140ms ease, color 140ms ease;
}
.new-chat-button:hover:not(:disabled) { border-color: var(--accent); color: var(--fg); }
.new-chat-button:disabled { cursor: not-allowed; opacity: .5; }
.new-chat-button svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }

.conversation-scroll { min-height: 0; flex: 1; overflow-y: auto; overscroll-behavior: contain; scrollbar-color: var(--border) transparent; scrollbar-width: thin; }
.conversation-scroll--empty { display: grid; place-items: center; }

.boot-state { display: flex; align-items: center; gap: 10px; padding: 40px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }

.welcome-state { width: min(100%, 640px); padding: 36px 12px 44px; text-align: center; }
.welcome-mark { display: inline-grid; width: 54px; height: 54px; place-items: center; border: 1px solid color-mix(in oklch, var(--accent) 22%, var(--border)); border-radius: 19px; background: color-mix(in oklch, var(--accent) 8%, var(--surface)); color: var(--accent); }
.welcome-mark svg { width: 25px; height: 25px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.5; }
.welcome-state h1 { margin: 17px 0 8px; font-size: clamp(25px, 4vw, 32px); font-weight: 650; letter-spacing: -.035em; }
.welcome-state > p { max-width: 54ch; margin: 0 auto; color: var(--muted); font-size: 14px; line-height: 1.8; }
.welcome-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; margin-top: 22px; text-align: left; }
.welcome-action { display: flex; align-items: flex-start; gap: 10px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--fg); cursor: pointer; font: inherit; text-align: left; transition: border-color 140ms ease, transform 140ms ease; }
.welcome-action:hover { border-color: color-mix(in oklch, var(--accent) 35%, var(--border)); transform: translateY(-1px); }
.welcome-action-k { display: grid; width: 22px; height: 22px; flex: none; place-items: center; border-radius: 7px; background: color-mix(in oklch, var(--accent) 10%, var(--bg)); color: var(--accent); font-family: var(--font-mono); font-size: 10px; }
.welcome-action strong { display: block; font-size: 12px; font-weight: 600; }
.welcome-action small { display: block; margin-top: 3px; color: var(--meta); font-size: 10px; line-height: 1.5; }

.message-list { display: flex; flex-direction: column; gap: 26px; padding: 26px 0 36px; }
.chat-message { display: flex; align-items: flex-start; gap: 11px; }
.chat-message--user { justify-content: flex-end; }
.message-content { min-width: 0; max-width: min(100%, 760px); }
.chat-message--assistant .message-content { flex: 1; }
.chat-message--user .message-content { max-width: min(82%, 650px); padding: 11px 15px; border: 1px solid var(--border-soft); border-radius: 18px 18px 5px 18px; background: color-mix(in oklch, var(--surface) 78%, var(--border-soft)); }
.message-mark { display: grid; width: 29px; height: 29px; flex: none; place-items: center; border: 1px solid color-mix(in oklch, var(--accent) 25%, var(--border)); border-radius: 10px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 12px; font-weight: 650; }
.message-text { margin: 0; color: var(--fg); font-size: 14px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
/* 模型不可用时的自述：它是"系统说明"，不是老师说的话，所以去掉了头像与气泡 */
.chat-message--notice .message-text { padding: 8px 11px; border: 1px dashed var(--border); border-radius: 9px; background: var(--surface); color: var(--meta); font-size: 11px; line-height: 1.7; }

.outline-card { margin-top: 12px; padding: 15px 16px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); }
.outline-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 13px; }
.card-kicker { display: block; margin-bottom: 3px; color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .07em; }
.outline-card-head strong { display: block; font-size: 14px; font-weight: 650; }
.outline-card-meta { display: block; margin-top: 2px; color: var(--muted); font-size: 10.5px; }
.outline-card-flag { flex: none; padding: 4px 8px; border: 1px solid var(--border); border-radius: 999px; color: var(--meta); font-size: 9px; }
.outline-card-flag--dirty { border-color: color-mix(in oklch, var(--warn) 45%, var(--border)); color: var(--warn); }
.outline-card-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 14px; padding-top: 13px; border-top: 1px solid var(--border-soft); }
.outline-hint { flex: 1; min-width: 180px; color: var(--meta); font-size: 10px; line-height: 1.6; }
.outline-hint--done { display: block; margin: 12px 0 0; padding-top: 11px; border-top: 1px solid var(--border-soft); }

.ready-card { margin-top: 12px; padding: 14px 15px; border: 1px solid color-mix(in oklch, var(--accent) 22%, var(--border)); border-radius: 13px; background: color-mix(in oklch, var(--accent) 4%, var(--surface)); }
.ready-head { display: flex; align-items: center; gap: 11px; }
.ready-mark { display: grid; width: 30px; height: 30px; flex: none; place-items: center; border-radius: 50%; background: color-mix(in oklch, var(--success) 18%, var(--bg)); color: var(--success); font-size: 14px; font-weight: 700; }
.ready-head strong { display: block; font-size: 13px; font-weight: 650; }
.ready-head span { display: block; margin-top: 2px; color: var(--muted); font-size: 10.5px; }
.ready-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 13px; }

.btn-solid { padding: 8px 13px; border: 1px solid transparent; border-radius: 9px; background: var(--accent); color: #fff; cursor: pointer; font: inherit; font-size: 11.5px; font-weight: 600; transition: background 130ms ease; }
.btn-solid:hover:not(:disabled) { background: var(--accent-hover); }
.btn-ghost { padding: 8px 13px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--muted); cursor: pointer; font: inherit; font-size: 11.5px; transition: border-color 130ms ease, color 130ms ease; }
.btn-ghost:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.btn-solid:disabled, .btn-ghost:disabled { cursor: not-allowed; opacity: .5; }

.task-card { margin-top: 4px; padding: 14px 15px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); }
.task-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.task-head strong { font-size: 12.5px; font-weight: 650; }
.task-percent { color: var(--accent); font-family: var(--font-mono); font-size: 11px; }
.task-progress { height: 6px; margin-top: 9px; overflow: hidden; border-radius: 3px; background: var(--border-soft); }
.task-progress > i { display: block; height: 100%; border-radius: inherit; background: var(--accent); transition: width 280ms ease; }
.task-stage { margin: 8px 0 0; color: var(--muted); font-size: 11px; }
.task-log { margin: 9px 0 0; padding: 9px 11px; max-height: 160px; overflow: auto; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); color: var(--meta); font-family: var(--font-mono); font-size: 10px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }
.task-more { margin-top: 7px; padding: 0; border: 0; background: transparent; color: var(--accent); cursor: pointer; font: inherit; font-size: 10px; }

.inline-error { margin: 0; padding: 9px 11px; border: 1px solid color-mix(in oklch, var(--danger) 30%, var(--border)); border-radius: 9px; background: color-mix(in oklch, var(--danger) 5%, var(--surface)); color: var(--fg); font-size: 11px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }

.composer-area { position: relative; z-index: 2; flex: none; padding-top: 12px; background: linear-gradient(180deg, transparent, var(--bg) 18%); }
.llm-note { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; padding: 7px 11px; border: 1px solid color-mix(in oklch, var(--warn) 32%, var(--border)); border-radius: 9px; background: color-mix(in oklch, var(--warn) 6%, var(--surface)); color: var(--fg); font-size: 10.5px; line-height: 1.6; }
.llm-note a { flex: none; color: var(--accent); font-size: 10.5px; }
.composer-box { padding: 12px 13px 10px; border: 1px solid var(--border); border-radius: 17px; background: var(--surface); box-shadow: 0 8px 28px color-mix(in oklch, var(--fg) 5%, transparent); }
.composer-box:focus-within { border-color: color-mix(in oklch, var(--accent) 48%, var(--border)); box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent) 9%, transparent); }
.composer-box textarea { display: block; width: 100%; min-height: 52px; max-height: 160px; resize: vertical; border: 0; outline: 0; background: transparent; color: var(--fg); font: inherit; font-size: 14px; line-height: 1.6; }
.composer-box textarea::placeholder { color: var(--meta); }
.composer-box textarea:disabled { opacity: .6; }
.composer-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 8px; }
.composer-state { min-width: 0; overflow: hidden; color: var(--meta); font-size: 10.5px; text-overflow: ellipsis; white-space: nowrap; }
.composer-submit-group { display: flex; flex: none; align-items: center; gap: 12px; }
.composer-hint { color: var(--meta); font-size: 10px; }
.send-button { display: grid; width: 34px; height: 34px; flex: none; place-items: center; border: 0; border-radius: 11px; background: var(--accent); color: #fff; cursor: pointer; transition: opacity 120ms ease, transform 120ms ease; }
.send-button:hover:not(:disabled) { transform: translateY(-1px); }
.send-button:disabled { cursor: not-allowed; opacity: .38; }
.send-button svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }
.send-spinner { width: 15px; height: 15px; border: 2px solid rgb(255 255 255 / 38%); border-top-color: #fff; border-radius: 50%; animation: spin .75s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.composer-disclaimer { margin: 8px 0 0; color: var(--meta); font-size: 10px; text-align: center; }

@media (max-width: 760px) {
  .generate-workspace { height: calc(100dvh - 128px); min-height: 470px; }
  .welcome-state { padding-inline: 2px; }
  .welcome-actions { grid-template-columns: minmax(0, 1fr); }
  .message-list { gap: 20px; padding-top: 18px; }
  .chat-message--user .message-content { max-width: 90%; }
  .composer-hint { display: none; }
  .outline-hint { min-width: 100%; }
}

@media (max-height: 720px) and (min-width: 761px) {
  .generate-workspace { height: calc(100dvh - 120px); min-height: 430px; }
  .welcome-state { padding-block: 16px 20px; }
}
</style>
