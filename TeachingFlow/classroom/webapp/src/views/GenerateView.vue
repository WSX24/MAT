<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import ImportCourseModal from '@/components/ImportCourseModal.vue'
import { ApiError, api } from '@/api/client'
import { errorText, formatDuration } from '@/api/format'
import type {
  GenerationLevel,
  GenerationSnapshot,
  GenerationStage,
  GenerationTerm,
  ModelDefault,
} from '@/api/types'
import { toast } from '@/state/toast'

/**
 * 生成课堂。
 *
 * 出片是 flow.py 的活，页面只做三件事：把「出片参数」提交成 POST /generations、
 * 每 2 秒拉一次快照画进度、把快照映射成阶段面板。命令原文和阶段标签都由后端给
 * （log_tail[0]、stage/stage_label），前端不拼命令也不猜阶段 —— 流水线改了这里不用跟。
 * 刷新页面靠 listGenerations() 的 active 接回正在跑的任务：进度活在服务端，不活在路由里。
 */
const POLL_MS = 2000
const LOG_LINES = 30
/** 挂载时接回刚结束任务的窗口：越久越容易让人误以为是这次刚跑的。 */
const RECALL_WINDOW_MS = 30 * 60 * 1000
const STAGE_OPTIONS: GenerationStage[] = ['小学', '初中', '高中', '大学', '考研']

/** 与 flow.py 的阶段编号对齐：S3/S4 都是「讲解」，合成一档，避免时间线虚胖。 */
const STAGES: Array<{ code: string; label: string }> = [
  { code: 'S0', label: '规划' },
  { code: 'S1', label: '大纲' },
  { code: 'S2', label: '选单元' },
  { code: 'S3/S4', label: '讲解' },
  { code: 'S5', label: '产片' },
]
const STAGE_OF_DIGIT = [0, 1, 2, 3, 3, 4]

interface Message {
  id: number
  text: string
}

interface TaskState {
  goal: string
  snapshot: GenerationSnapshot | null
  /** 400/409 或轮询失联时的原文：后端的 message 直接给用户看，不改写。 */
  message: string
  /** 后端回 code=no_provider：该去设置页配服务商，而不是回头改出片参数。 */
  needProvider: boolean
  /** 从服务端接回来的终态任务：不是这次刚跑的，标题必须说清楚。 */
  recalled: boolean
}

const router = useRouter()
const chatLog = ref<HTMLElement | null>(null)
const draft = ref('')
const showSettings = ref(false)
const showImport = ref(false)
const messages = ref<Message[]>([])
const task = ref<TaskState | null>(null)
const creating = ref(false)
const cancelling = ref(false)
const scanning = ref(false)
const conflict = ref(false)
const showLog = ref(false)
const showCommand = ref(false)
const modelDefault = ref<ModelDefault | null>(null)
const modelError = ref('')
const modelLoading = ref(false)
const selectedStage = ref<GenerationStage | ''>('')
const subject = ref('')
const selectedCycle = ref<GenerationTerm>('long')
const selectedLevel = ref<GenerationLevel>('beginner')

let nextMessageId = 1
let pollTimer: number | null = null
let disposed = false

function isActiveStatus(status: string | null | undefined): boolean {
  return status === 'queued' || status === 'running'
}

function isTerminalStatus(status: string | null | undefined): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled' || status === 'interrupted'
}

/** finished_at 由后端写 ISO 时间；解析不出来就当不在窗口里 —— 宁可不接，也别伪装成刚跑完。 */
function finishedRecently(current: GenerationSnapshot): boolean {
  if (!current.finished_at) return false
  const finished = Date.parse(current.finished_at)
  if (Number.isNaN(finished)) return false
  return Math.abs(Date.now() - finished) <= RECALL_WINDOW_MS
}

/** stage 可能是 "S3"、"s3_units"、"S3/S4"，也可能没有编号：认不出就按 percent 落到时间线上。 */
function stageIndex(current: GenerationSnapshot): number {
  const match = /s\s*([0-5])/i.exec(current.stage || '')
  if (match) return STAGE_OF_DIGIT[Number(match[1])] ?? 0
  if (current.status === 'succeeded') return STAGES.length - 1
  const percent = Math.max(0, Math.min(100, current.progress?.percent ?? 0))
  return Math.min(STAGES.length - 1, Math.floor(percent / 20))
}

const snapshot = computed(() => task.value?.snapshot ?? null)
const running = computed(() => isActiveStatus(snapshot.value?.status))
const isBusy = computed(() => creating.value || running.value || cancelling.value)
const statusValue = computed(() => snapshot.value?.status ?? '')
const stageLabel = computed(() => snapshot.value?.stage_label || '准备中')
const percent = computed(() => Math.max(0, Math.min(100, Math.round(snapshot.value?.progress?.percent ?? 0))))
const doneSteps = computed(() => snapshot.value?.progress?.done ?? 0)
const totalSteps = computed(() => snapshot.value?.progress?.total ?? 0)
const elapsedText = computed(() => formatDuration(snapshot.value?.elapsed_sec))
/**
 * 日志尾只放**子进程的真实输出**。
 *
 * log_tail[0] 是后端写的命令行原文，它已经在「高级：命令行等价命令」里单独展示；
 * 两边都渲染第 1 行的话，两个按钮点开是同一段字（实测被指出来过：一模一样的两块）。
 * 所以这里切掉第 1 行，并且只留最后 LOG_LINES 行。
 */
const logLines = computed(() => (snapshot.value?.log_tail ?? []).slice(1).slice(-LOG_LINES))
const commandLine = computed(() => snapshot.value?.log_tail?.[0] ?? '')
const failureText = computed(() => snapshot.value?.error ?? '')
const result = computed(() => snapshot.value?.result ?? null)
const courseId = computed(() => result.value?.course_id ?? '')
const courseTitle = computed(() => result.value?.course_title || '未命名课程')
const pptxPreview = computed(() => (result.value?.pptx ?? []).slice(0, 4))
const pptxCount = computed(() => result.value?.pptx?.length ?? 0)
const scanText = computed(() => {
  const scan = result.value?.scan
  if (!scan) return ''
  return '扫描：新增 ' + scan.added + ' · 更新 ' + scan.updated + ' · 共 ' + scan.total + ' 门'
})

/** 接回来的终态任务必须挂「上次」的名分，否则刷新后看着像刚跑完的。 */
const RECALLED_TITLES: Record<string, string> = {
  succeeded: '已完成',
  failed: '已失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

const recalled = computed(() => task.value?.recalled === true)

const taskNote = computed(() => {
  if (recalled.value) return '上次出片任务 · ' + (RECALLED_TITLES[statusValue.value] ?? '已结束')
  const current = snapshot.value
  if (!current) return '出片任务 · 提交中'
  return current.mode === 'mock' ? '出片任务 · dev 模拟出片' : '出片任务 · 真实模型出片'
})

const statusSentence = computed(() => {
  const current = snapshot.value
  if (!current) return '正在提交出片任务…'
  switch (current.status) {
    case 'queued':
      return '任务已排队，流水线空出来就开始。'
    case 'running':
      return '正在出片：' + (current.stage_label || '准备中')
    case 'succeeded':
      return '出片完成' + (current.result?.course_title ? '：' + current.result.course_title : '')
    case 'failed':
      return '出片失败：下面是原因和日志尾，按提示改参数或补配置后再发一次。'
    case 'cancelled':
      return '任务已取消，没有产出课件。'
    case 'interrupted':
      return '任务被中断（课堂服务在出片途中重启过），需要重新发送一次。'
    default:
      return current.stage_label || '处理中'
  }
})

/**
 * 出片用哪个模型由「设置 · 模型服务商」里的那条默认决定：这一页只读，不再自己解释 key 从哪来 ——
 * 两处各存一份必然对不上，用户也会不知道到底该改哪一边。
 */
const modelLabel = computed(() => {
  if (modelLoading.value) return '读取配置…'
  const current = modelDefault.value
  if (!current) return '未配置模型服务商'
  return (current.provider?.name || current.provider?.kind || '服务商') + ' · ' + (current.model || '模型未声明')
})

const baseUrl = computed(() => modelDefault.value?.provider?.base_url ?? '')
const modelMissing = computed(() => !modelLoading.value && !modelDefault.value)

function stageState(index: number): 'done' | 'active' | 'pending' | 'halt' {
  const current = snapshot.value
  if (!current) return 'pending'
  const active = stageIndex(current)
  if (current.status === 'succeeded') return 'done'
  if (index < active) return 'done'
  if (index > active) return 'pending'
  return isActiveStatus(current.status) ? 'active' : 'halt'
}

function scrollChatToBottom() {
  void nextTick(() => {
    if (chatLog.value) chatLog.value.scrollTop = chatLog.value.scrollHeight
  })
}

async function loadModelDefault() {
  modelLoading.value = true
  try {
    const list = await api.listModelProviders()
    modelDefault.value = list.default
    modelError.value = ''
  } catch (error) {
    modelDefault.value = null
    modelError.value = errorText(error)
  } finally {
    modelLoading.value = false
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

function schedulePoll(id: string) {
  stopPolling()
  if (disposed) return
  pollTimer = window.setTimeout(() => {
    pollTimer = null
    void poll(id)
  }, POLL_MS)
}

function applySnapshot(next: GenerationSnapshot) {
  const current = task.value
  if (!current) return
  current.snapshot = next
  current.message = ''
  current.needProvider = false
  conflict.value = false
  if (isActiveStatus(next.status)) return
  stopPolling()
  if (next.status === 'succeeded') {
    toast.ok('出片完成' + (next.result?.course_title ? '：' + next.result.course_title : ''))
  } else if (next.status === 'failed') {
    toast.error(next.error || '出片失败：日志尾里有原因')
  }
}

async function poll(id: string) {
  if (disposed) return
  try {
    const next = await api.getGeneration(id)
    if (disposed) return
    applySnapshot(next)
    if (isActiveStatus(next.status)) schedulePoll(id)
  } catch (error) {
    if (disposed) return
    if (error instanceof ApiError && error.status === 404) {
      if (task.value) task.value.message = '这个任务在服务端已经不存在了（换了数据目录或清过记录），重新发送一次即可。'
      stopPolling()
      return
    }
    // 网络抖动不该让面板停在旧值上：先把原因摆出来，2 秒后继续拉。
    if (task.value) task.value.message = errorText(error)
    schedulePoll(id)
  }
}

/** 接上服务端正在跑的任务：刷新进页面、409 冲突、从别的标签页回来都走这里。 */
async function attachActive(echoGoal = false) {
  try {
    const list = await api.listGenerations(10)
    if (disposed) return
    const active = list.active
    if (active) {
      if (echoGoal) {
        messages.value.push({ id: nextMessageId++, text: active.goal })
        scrollChatToBottom()
      }
      task.value = { goal: active.goal, snapshot: active, message: '', needProvider: false, recalled: false }
      conflict.value = false
      showLog.value = false
      showCommand.value = false
      if (isActiveStatus(active.status)) schedulePoll(active.id)
      return
    }
    // 没有在跑的任务时接回刚结束的那次：后端不留历史页，失败原因只活在这条快照里。
    const recent = list.items[0]
    if (!recent || !isTerminalStatus(recent.status) || !finishedRecently(recent)) return
    if (echoGoal) {
      messages.value.push({ id: nextMessageId++, text: recent.goal })
      scrollChatToBottom()
    }
    task.value = { goal: recent.goal, snapshot: recent, message: '', needProvider: false, recalled: true }
    conflict.value = false
    showLog.value = false
    showCommand.value = false
    // 终态不会变，所以不起轮询：拉到的只会是同一份快照。
  } catch {
    // 拉不到列表时不覆盖手上的提示：此时用户至少还有一条真实错误可读。
  }
}

async function submitGoal() {
  const goal = draft.value.trim()
  if (!goal || isBusy.value) return
  if (!selectedStage.value) {
    showSettings.value = true
    toast.warn('请先在出片参数里选择学段')
    return
  }
  const stage = selectedStage.value

  draft.value = ''
  messages.value.push({ id: nextMessageId++, text: goal })
  task.value = { goal, snapshot: null, message: '', needProvider: false, recalled: false }
  conflict.value = false
  showLog.value = false
  showCommand.value = false
  creating.value = true
  scrollChatToBottom()

  try {
    const created = await api.createGeneration({
      goal,
      stage,
      subject: subject.value.trim() || undefined,
      term: selectedCycle.value,
      level: selectedLevel.value,
    })
    if (disposed) return
    if (task.value) task.value.snapshot = created
    if (isActiveStatus(created.status)) schedulePoll(created.id)
  } catch (error) {
    if (disposed) return
    if (task.value) task.value.message = errorText(error)
    // 400 里 code 决定去哪里补：no_provider 是「先去配服务商」，其余入参问题才是回头改出片参数。
    // 按码分派而不是认文案：同一句 message 后端随时可以改措辞。
    if (error instanceof ApiError && error.status === 400) {
      const needProvider = error.code === 'no_provider'
      if (task.value) task.value.needProvider = needProvider
      showSettings.value = !needProvider
    }
    if (error instanceof ApiError && error.status === 409) conflict.value = true
  } finally {
    if (!disposed) creating.value = false
  }
}

/** 缺学段时发送按钮是真 disabled，浏览器不会派发它的 click：由外层接住这一次点击，把原因说一句。 */
function nudgeStage() {
  if (isBusy.value || !draft.value.trim() || selectedStage.value) return
  showSettings.value = true
  toast.warn('请先在出片参数里选择学段')
}

async function cancelTask() {
  const current = snapshot.value
  if (!current || cancelling.value) return
  cancelling.value = true
  try {
    const updated = await api.cancelGeneration(current.id)
    if (disposed) return
    applySnapshot(updated)
  } catch (error) {
    if (disposed) return
    toast.error(errorText(error))
    // 取消失败也要留痕：toast 会自己消失，面板里的原文不会。
    if (task.value) task.value.message = errorText(error)
    // 409＝已经结束：立刻对一次真实状态，别让「取消」按钮继续骗人。
    if (error instanceof ApiError && error.status === 409) {
      try {
        const latest = await api.getGeneration(current.id)
        if (!disposed) applySnapshot(latest)
      } catch {
        // 拉不到就交给下一轮轮询。
      }
    }
  } finally {
    if (!disposed) cancelling.value = false
  }
}

async function copyCommand() {
  if (!commandLine.value) return
  try {
    await navigator.clipboard.writeText(commandLine.value)
    toast.ok('命令已复制：在 TeachingFlow 目录下粘贴执行可复现这次出片')
  } catch {
    toast.warn('复制失败：请手动选中命令文本复制')
  }
}

async function rescan() {
  if (scanning.value) return
  scanning.value = true
  try {
    const scanned = await api.scanCourses()
    if (scanned.total === 0) toast.warn('课程库里还没有可收录的课件')
    else toast.ok('扫描完成：新增 ' + scanned.added + '、更新 ' + scanned.updated + '，共 ' + scanned.total + ' 门')
    void router.push({ name: 'app-courses' })
  } catch (error) {
    toast.error(errorText(error))
  } finally {
    scanning.value = false
  }
}

function openCourse(id: string) {
  if (!id) return
  void router.push({ name: 'app-course-detail', params: { id } })
}

function openImported(id: string) {
  openCourse(id)
}

function clearConversation() {
  if (isBusy.value) return
  messages.value = []
  task.value = null
  conflict.value = false
  showSettings.value = false
  showLog.value = false
  showCommand.value = false
  draft.value = ''
  nextMessageId = 1
}

onMounted(() => {
  void loadModelDefault()
  void attachActive(true)
})

onBeforeUnmount(() => {
  disposed = true
  stopPolling()
})
</script>

<template>
  <section class="generate-workspace" aria-label="生成课堂">
    <header class="conversation-header">
      <button class="new-chat-button" type="button" :disabled="isBusy" @click="clearConversation()">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        <span>新对话</span>
      </button>
    </header>

    <div ref="chatLog" class="conversation-scroll" :class="{ 'conversation-scroll--empty': !messages.length && !task }" aria-live="polite">
      <div v-if="!messages.length && !task" class="welcome-state">
        <span class="welcome-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12 3.5 13.8 10l6.7 2-6.7 2-1.8 6.5L10.2 14l-6.7-2 6.7-2L12 3.5Z" /></svg>
        </span>
        <h1>今天想学点什么？</h1>
        <p>说清学习主题、目标和时间安排，我会直接启动出片流水线：先规划，再生成大纲与讲解，最后产出整套课件。</p>
        <div class="welcome-actions">
          <button class="welcome-action" type="button" @click="showImport = true">
            <span class="welcome-action-k">A</span>
            <span><strong>导入现成课件</strong><small>已有 .pptx，直接进课程库并备课</small></span>
          </button>
          <button class="welcome-action" type="button" @click="showSettings = true">
            <span class="welcome-action-k">B</span>
            <span><strong>出片参数</strong><small>学段必选；学科、学习周期、起点随任务提交</small></span>
          </button>
        </div>
      </div>

      <div v-else class="message-list">
        <article v-for="message in messages" :key="message.id" class="chat-message chat-message--user">
          <div class="message-content">
            <p class="message-text">{{ message.text }}</p>
          </div>
        </article>

        <article v-if="task" class="chat-message chat-message--assistant">
          <span class="message-mark" aria-hidden="true">师</span>
          <div class="message-content">
            <div class="message-label">{{ taskNote }}</div>
            <p class="message-text">{{ statusSentence }}</p>

            <p v-if="task.message" class="task-notice" role="alert">{{ task.message }}</p>
            <div v-if="task.needProvider" class="result-actions">
              <RouterLink class="provider-link" :to="{ name: 'app-settings' }">去「设置」配置模型服务商</RouterLink>
            </div>
            <div v-if="conflict" class="result-actions">
              <button class="result-secondary" type="button" @click="attachActive()">接上正在跑的任务</button>
            </div>

            <section v-if="snapshot" class="result-card task-card" :class="'task-card--' + statusValue" aria-label="出片任务进度">
              <div class="task-head">
                <strong class="result-heading">{{ stageLabel }}</strong>
                <span class="task-percent">{{ percent }}%</span>
              </div>
              <div class="task-progress" role="progressbar" :aria-valuenow="percent" aria-valuemin="0" aria-valuemax="100">
                <i :style="{ width: percent + '%' }" />
              </div>
              <div class="task-meta">
                <span v-if="recalled">上次任务（已结束，不再刷新）</span>
                <span>{{ doneSteps }} / {{ totalSteps }} 步</span>
                <span>已用 {{ elapsedText }}</span>
              </div>

              <ol class="stage-timeline" aria-label="出片阶段">
                <li v-for="(stageItem, index) in STAGES" :key="stageItem.code" class="stage-item" :class="'stage-item--' + stageState(index)">
                  <span class="stage-code">{{ stageItem.code }}</span>
                  <span>{{ stageItem.label }}</span>
                </li>
              </ol>

              <p v-if="statusValue === 'failed' && failureText" class="task-error">{{ failureText }}</p>
              <p v-if="statusValue === 'failed'" class="task-note">先看下面的流水线输出；常见原因是服务商账户余额不足/额度用尽、key 失效、base_url 或模型名不对，或流水线在 S5 渲染失败。</p>

              <div v-if="result" class="task-result">
                <span class="result-heading">已生成课程</span>
                <p class="task-result-title">{{ courseTitle }}</p>
                <div class="file-list">
                  <span v-for="file in pptxPreview" :key="file" class="file-chip">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3.75h8l4 4v12.5H6zM14 3.75v4h4M9 13h6M9 16.5h6" /></svg>
                    {{ file }}
                  </span>
                  <span v-if="pptxCount > pptxPreview.length" class="file-chip">+{{ pptxCount - pptxPreview.length }} 个 pptx</span>
                </div>
                <div class="task-meta">
                  <span>{{ pptxCount }} 个 pptx</span>
                  <span>{{ scanText }}</span>
                </div>
                <div class="result-actions">
                  <button v-if="courseId" class="save-course-button" type="button" @click="openCourse(courseId)">进入课程</button>
                  <button v-else class="save-course-button" type="button" :disabled="scanning" @click="rescan()">
                    {{ scanning ? '扫描中…' : '重新扫描课程库' }}
                  </button>
                </div>
                <p v-if="!courseId" class="task-note">这次没拿到 course_id：到「我的课堂」点「重新扫描」，课件就会进课程库。</p>
              </div>

              <div class="result-actions">
                <button v-if="running" class="result-secondary result-secondary--danger" type="button" :disabled="cancelling" @click="cancelTask()">
                  {{ cancelling ? '取消中…' : '取消出片任务' }}
                </button>
                <button class="result-secondary" type="button" :aria-expanded="showLog" @click="showLog = !showLog">
                  流水线输出（{{ logLines.length }} 行）
                </button>
                <button class="result-secondary" type="button" :aria-expanded="showCommand" @click="showCommand = !showCommand">
                  高级：这条命令
                </button>
              </div>

              <pre v-if="showLog" class="log-tail">{{ logLines.length ? logLines.join('\n') : '流水线还没有输出。跑起来后这里会逐行刷新；长时间没动静会看到「仍在运行：…」的心跳行。' }}</pre>

              <div v-if="showCommand" class="task-command">
                <pre class="command-box">{{ commandLine || '任务还没写出执行命令，等下一轮轮询。' }}</pre>
                <div class="result-actions">
                  <button class="result-secondary" type="button" :disabled="!commandLine" @click="copyCommand()">复制命令</button>
                </div>
              </div>
            </section>
          </div>
        </article>
      </div>
    </div>

    <footer class="composer-area">
      <section v-if="showSettings" class="settings-panel" aria-label="出片参数">
        <div class="settings-panel-header">
          <strong>出片参数</strong>
          <span>提交时生效</span>
        </div>
        <div class="setting-row">
          <span class="setting-name">学段</span>
          <div class="setting-choices">
            <select v-model="selectedStage" class="setting-select" :disabled="isBusy" aria-label="出片学段">
              <option value="">必选：小学 / 初中 / 高中 / 大学 / 考研</option>
              <option v-for="option in STAGE_OPTIONS" :key="option" :value="option">{{ option }}</option>
            </select>
          </div>
        </div>
        <div class="setting-row">
          <span class="setting-name">学科</span>
          <div class="setting-choices">
            <input v-model="subject" class="setting-input" type="text" :disabled="isBusy" placeholder="数学（可不填）" aria-label="出片学科" />
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
        <div class="setting-row">
          <span class="setting-name">模型</span>
          <div class="setting-choices setting-choices--stack">
            <span class="setting-value">{{ modelLabel }}</span>
            <span v-if="baseUrl" class="setting-hint">{{ baseUrl }}</span>
            <RouterLink v-if="modelMissing" class="setting-link" :to="{ name: 'app-settings' }">去「设置」挑一个模型服务商</RouterLink>
            <span v-else-if="modelError" class="setting-hint">{{ modelError }}</span>
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
            <span>出片参数</span>
            <span class="selected-model">{{ selectedStage ? selectedStage + ' · ' + modelLabel : '学段未选' }}</span>
          </button>
          <div class="composer-submit-group" @click="nudgeStage()">
            <span class="composer-hint">Enter 发送 · Shift + Enter 换行</span>
            <button class="send-button" type="button" :disabled="isBusy || !draft.trim() || !selectedStage" :aria-label="isBusy ? '出片任务进行中' : '启动出片任务'" @click="submitGoal()">
              <svg v-if="!isBusy" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-7 7 7-7 7 7" /></svg>
              <span v-else class="send-spinner" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>
      <p class="composer-disclaimer">出片任务在课堂服务里跑，关掉这一页也会继续；完成后点「进入课程」就能上课。</p>
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
.result-card { margin-top: 15px; padding: 14px 15px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); }
.file-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 11px; }
.file-chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 7px 9px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg); color: var(--muted); font-family: var(--font-mono); font-size: 10px; overflow-wrap: anywhere; }
.file-chip svg { width: 14px; height: 14px; flex: none; fill: none; stroke: var(--accent); stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.5; }
.command-box { margin: 11px 0 0; padding: 10px 12px; overflow-x: auto; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); color: var(--fg); font-family: var(--font-mono); font-size: 10px; line-height: 1.7; white-space: pre-wrap; }
.result-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.save-course-button { padding: 8px 12px; border: 0; border-radius: 9px; background: var(--accent); color: white; cursor: pointer; font: inherit; font-size: 12px; font-weight: 600; }
.save-course-button:hover { filter: brightness(.96); }
.result-secondary { padding: 7px 11px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--muted); cursor: pointer; font: inherit; font-size: 11px; }
.provider-link { display: inline-flex; align-items: center; padding: 7px 11px; border: 1px solid color-mix(in oklch, var(--accent) 42%, var(--border)); border-radius: 9px; color: var(--accent); font-size: 11px; text-decoration: none; transition: border-color 140ms ease, background 140ms ease; }
.provider-link:hover { border-color: var(--accent); background: color-mix(in oklch, var(--accent) 7%, var(--surface)); }
.result-secondary:hover { border-color: var(--accent); color: var(--accent); }
.result-secondary:disabled { cursor: not-allowed; opacity: .5; }

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
.send-button:disabled { cursor: not-allowed; opacity: .38; pointer-events: none; }
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

/* ── 出片任务面板：沿用卡片 / 日志 / 按钮的既有配方，只补进度与阶段时间线 ── */
.task-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.task-percent { color: var(--accent); font-family: var(--font-mono); font-size: 11px; }
.task-progress { height: 6px; margin-top: 9px; overflow: hidden; border-radius: 3px; background: var(--border-soft); }
.task-progress > i { display: block; height: 100%; border-radius: inherit; background: var(--accent); transition: width 280ms ease; }
.task-card--failed .task-progress > i, .task-card--cancelled .task-progress > i, .task-card--interrupted .task-progress > i { background: var(--meta); }
.task-card--failed .task-percent { color: var(--danger); }
.task-meta { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; color: var(--meta); font-size: 10px; }
.stage-timeline { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 0; padding: 0; list-style: none; }
.stage-item { display: inline-flex; align-items: center; gap: 6px; padding: 5px 8px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg); color: var(--meta); font-size: 10px; transition: border-color 160ms ease, color 160ms ease, background 160ms ease; }
.stage-item--done { border-color: var(--border); color: var(--muted); }
.stage-item--active { border-color: color-mix(in oklch, var(--accent) 55%, var(--border)); background: color-mix(in oklch, var(--accent) 7%, var(--surface)); color: var(--accent); }
.stage-item--halt { border-color: color-mix(in oklch, var(--danger) 42%, var(--border)); background: color-mix(in oklch, var(--danger) 6%, var(--surface)); color: var(--danger); }
.stage-code { font-family: var(--font-mono); font-size: 9px; }
.task-notice, .task-error { margin: 11px 0 0; padding: 9px 11px; border: 1px solid color-mix(in oklch, var(--danger) 30%, var(--border)); border-radius: 9px; background: color-mix(in oklch, var(--danger) 5%, var(--surface)); color: var(--fg); font-size: 11px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.task-result { margin-top: 13px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
.task-result-title { margin: 6px 0 0; color: var(--fg); font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.task-note { margin: 8px 0 0; color: var(--meta); font-size: 10px; line-height: 1.6; }
.task-command { margin-top: 11px; }
.log-tail { margin: 11px 0 0; padding: 10px 12px; max-height: 220px; overflow: auto; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); color: var(--muted); font-family: var(--font-mono); font-size: 10px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }
.result-secondary--danger { border-color: color-mix(in oklch, var(--danger) 32%, var(--border)); color: var(--danger); }
.result-secondary--danger:hover:not(:disabled) { border-color: var(--danger); color: var(--danger); }
.setting-select, .setting-input { min-width: 132px; padding: 6px 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--fg); font: inherit; font-size: 10px; }
.setting-select:hover:not(:disabled), .setting-input:hover:not(:disabled) { border-color: var(--accent); }
.setting-select:disabled, .setting-input:disabled { cursor: not-allowed; opacity: .55; }
.setting-input::placeholder { color: var(--meta); }
.setting-value { padding: 6px 9px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg); color: var(--fg); font-family: var(--font-mono); font-size: 10px; }
.setting-link { color: var(--accent); font-size: 10px; text-decoration: underline; }
.setting-choices--stack { flex-direction: column; align-items: flex-start; gap: 6px; }
.setting-hint { color: var(--meta); font-size: 10px; line-height: 1.6; overflow-wrap: anywhere; }

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
