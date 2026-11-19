<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/api/client'
import { errorText, formatDuration, sourceLabel } from '@/api/format'
import { totalSeconds, totalSlides, unitStatusText } from '@/api/view'
import type { CourseDetail, PptPlan, PptPlanItem } from '@/api/types'
import { toast } from '@/state/toast'

/**
 * 课程详情：章 → 单元 → 进入课堂，外加**渐进式 PPT 的进度与入口**。
 *
 * 版式沿用截图里那套「章号 + 章名 / 单元行 + 状态圆点 + 竖虚线」的层级，
 * 因为学生看的就是"这门课分几章、每章几节、我上到哪了"。
 *
 * PPT 的口径是"上哪一课、生成哪一课"：
 *   · 每一行都显示这一课有没有课件（✓ 绿点 / 序号橙点）；
 *   · 页头有「生成下一课 PPT」—— 顺序取第一个还没有课件的单元；
 *   · 正在生成时整页会轮询 /ppt-plan，进度直接画在那一行上。
 */
const POLL_MS = 1500

const route = useRoute()
const router = useRouter()

const course = ref<CourseDetail | null>(null)
const plan = ref<PptPlan | null>(null)
const loading = ref(true)
const loadError = ref('')
const deleting = ref(false)
const starting = ref(false)
const collapsed = ref<Record<string, boolean>>({})

let pollTimer: number | null = null
let disposed = false

interface UnitRow {
  id: string
  title: string
  slides: number
  estSec: number
  ready: boolean
  hasPpt: boolean
  jobStatus: string
  jobStage: string
  jobPercent: number
  error: string
  kp: string[]
  orderIndex: number
  chapterIndex: number
  unitIndex: number
  chapterTitle: string
  progressPercent: number
  progressStatus: string
}

const rows = computed<UnitRow[]>(() => {
  const detail = course.value
  if (!detail) return []
  const items = plan.value?.items ?? []
  const flat = (detail.outline?.chapters ?? []).flatMap((chapter) => chapter.units)
  const out: UnitRow[] = []
  let order = 0
  detail.chapters.forEach((chapter, chapterIndex) => {
    chapter.units.forEach((unit, unitIndex) => {
      const item: PptPlanItem | undefined =
        items.find((candidate) => candidate.order_index === order) ?? items[order]
      const outlineUnit = flat[order]
      out.push({
        id: unit.id,
        title: unit.title || outlineUnit?.title || '未命名单元',
        slides: unit.slides,
        estSec: unit.est_sec,
        ready: unit.ready,
        hasPpt: item ? item.has_ppt : unit.slides > 0,
        jobStatus: item?.job_status ?? '',
        jobStage: item?.job_stage ?? '',
        jobPercent: item?.job_percent ?? 0,
        error: item?.error ?? '',
        kp: outlineUnit?.knowledge_points ?? [],
        orderIndex: order,
        chapterIndex,
        unitIndex,
        chapterTitle: chapter.title,
        progressPercent: Math.round(unit.progress?.percent ?? 0),
        progressStatus: unit.progress?.status ?? '',
      })
      order += 1
    })
  })
  return out
})

const chapters = computed(() => {
  const grouped: Array<{ title: string; index: number; units: UnitRow[] }> = []
  for (const row of rows.value) {
    const last = grouped[grouped.length - 1]
    if (last && last.title === row.chapterTitle) last.units.push(row)
    else grouped.push({ title: row.chapterTitle, index: row.chapterIndex, units: [row] })
  }
  return grouped
})

const totalUnits = computed(() => rows.value.length)
const readyUnits = computed(() => rows.value.filter((row) => row.hasPpt).length)
const nextUnit = computed(() => {
  const id = plan.value?.next_unit_id ?? ''
  return rows.value.find((row) => row.id === id) ?? rows.value.find((row) => !row.hasPpt) ?? null
})
const runningRow = computed(() =>
  rows.value.find((row) => row.jobStatus === 'queued' || row.jobStatus === 'running') ?? null)
const canGenerate = computed(() => !!course.value?.outline && totalUnits.value > 0)

const slideCount = computed(() => (course.value ? totalSlides(course.value) : 0))
const duration = computed(() => (course.value ? formatDuration(totalSeconds(course.value)) : '—'))
const completedCount = computed(() =>
  rows.value.filter((row) => row.progressStatus === 'completed').length)
const percent = computed(() => {
  if (!rows.value.length) return 0
  const sum = rows.value.reduce((total, row) => total + row.progressPercent, 0)
  return Math.round(sum / rows.value.length)
})

const overviewText = computed(() => {
  const detail = course.value
  if (!detail) return ''
  const free = detail.outline?.summary?.trim()
  const goal = detail.outline?.goal?.trim()
  // 概述与目标是两块内容，不能同一句话贴两遍（模型给的 summary 经常就是 goal）
  if (free && free !== goal) return free
  return `本课程共 ${detail.chapters.length} 章、${totalUnits.value} 个单元，已生成 ${readyUnits.value} 个单元的课件。`
    + `预计学习 ${duration.value}。当前完成 ${completedCount.value} 个单元，整体进度 ${percent.value}%。`
})

const goalText = computed(() => {
  const detail = course.value
  if (!detail) return ''
  const goal = detail.outline?.goal?.trim()
  if (goal) return goal
  const minutes = Math.max(1, Math.round(totalSeconds(detail) / Math.max(1, totalUnits.value) / 60))
  return `完成全部 ${totalUnits.value} 个单元后，可覆盖本课程的全部课件内容。`
    + `建议按「章 → 单元」顺序学习，每个单元约 ${minutes} 分钟。`
})

const outlineMeta = computed(() => {
  const outline = course.value?.outline
  if (!outline) return ''
  const parts: string[] = []
  if (outline.subject) parts.push(outline.subject)
  if (outline.stage) parts.push(outline.stage)
  if (outline.student_level) parts.push(`起点 ${outline.student_level}`)
  parts.push(`${outline.weeks} 周 × 每周 ${outline.hours_per_week} 课时`)
  return parts.join(' · ')
})

function unitRoute(row: UnitRow) {
  return {
    name: 'app-lesson',
    params: {
      id: course.value?.id ?? '',
      unitNumber: String(row.chapterIndex + 1),
      lessonNumber: String(row.unitIndex + 1),
    },
  }
}

function toggleChapter(key: string) {
  collapsed.value = { ...collapsed.value, [key]: !collapsed.value[key] }
}

async function load() {
  const id = String(route.params.id ?? '')
  loading.value = true
  loadError.value = ''
  try {
    course.value = await api.getCourse(id)
  } catch (error) {
    loadError.value = errorText(error)
    course.value = null
  } finally {
    loading.value = false
  }
  await loadPlan(id)
}

async function loadPlan(id: string) {
  try {
    plan.value = await api.getPptPlan(id)
    if (plan.value.items.some((item) => item.job_status === 'queued' || item.job_status === 'running')) {
      schedulePoll(id)
    }
  } catch {
    // 没有 ppt-plan 不应该让整页打不开：结构照常显示，只是看不到课件状态
    plan.value = null
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
  pollTimer = window.setTimeout(() => { void poll(id) }, POLL_MS)
}

async function poll(id: string) {
  pollTimer = null
  if (disposed) return
  try {
    const before = plan.value
    plan.value = await api.getPptPlan(id)
    const finishedNow = plan.value.items.some((item, index) => {
      const previous = before?.items[index]
      return item.has_ppt && previous && !previous.has_ppt
    })
    if (finishedNow) {
      toast.ok('这一课的课件已经生成好了')
      await refreshCourse(id)
    }
    if (plan.value.items.some((item) => item.job_status === 'queued' || item.job_status === 'running')) {
      schedulePoll(id)
    }
  } catch {
    schedulePoll(id)
  }
}

async function refreshCourse(id: string) {
  try {
    course.value = await api.getCourse(id)
  } catch {
    // 刷新失败只是列表上的页数晚一步更新
  }
}

async function generateNext() {
  const id = course.value?.id ?? ''
  if (!id || starting.value) return
  starting.value = true
  try {
    await api.generateNextPpt(id)
    toast.ok(`正在生成「${nextUnit.value?.title ?? '下一课'}」的课件`)
    await loadPlan(id)
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) toast.warn(error.message)
    else toast.error(errorText(error))
  } finally {
    starting.value = false
  }
}

async function generateOne(row: UnitRow) {
  if (starting.value || runningRow.value) return
  starting.value = true
  try {
    await api.generateUnitPpt(row.id)
    toast.ok(`正在生成「${row.title}」的课件`)
    await loadPlan(course.value?.id ?? '')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) toast.warn(error.message)
    else toast.error(errorText(error))
  } finally {
    starting.value = false
  }
}

async function cancelRunning() {
  const row = runningRow.value
  if (!row) return
  try {
    await api.cancelUnitPpt(row.id)
    toast.warn('已取消生成')
    await loadPlan(course.value?.id ?? '')
  } catch (error) {
    toast.error(errorText(error))
  }
}

async function removeCourse() {
  if (!course.value || deleting.value) return
  if (!window.confirm(`从课程库移除「${course.value.title}」？该课程的单元、会话与进度会一起删除（源文件不动）。`)) return
  deleting.value = true
  try {
    await api.deleteCourse(course.value.id)
    toast.ok('课程已移除')
    await router.push({ name: 'app-courses' })
  } catch (error) {
    toast.error(errorText(error))
  } finally {
    deleting.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => { disposed = true; stopPolling() })
</script>

<template>
  <div class="course-detail-page">
    <RouterLink class="detail-back" :to="{ name: 'app-courses' }">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回我的课堂
    </RouterLink>

    <div v-if="loading" class="detail-loading" role="status">
      <span class="loading-bar" aria-hidden="true" />
      <span>正在读取课程…</span>
    </div>

    <section v-else-if="loadError && !course" class="detail-not-found">
      <span class="not-found-mark" aria-hidden="true">!</span>
      <h1>课程没有打开</h1>
      <p>{{ loadError }}</p>
      <button class="btn btn-primary" type="button" @click="load">重试</button>
    </section>

    <template v-else-if="course">
      <header class="detail-hero">
        <div class="detail-hero-copy">
          <span class="eyebrow">{{ sourceLabel(course.source) }}</span>
          <h1>{{ course.title }}</h1>
          <div class="detail-tags">
            <span v-if="course.subject">{{ course.subject }}</span>
            <span>{{ course.chapters.length }} 章 · {{ totalUnits }} 个小节</span>
            <span>课件 {{ readyUnits }} / {{ totalUnits }}</span>
            <span v-if="outlineMeta">{{ outlineMeta }}</span>
          </div>
        </div>
        <div class="detail-hero-art" aria-hidden="true">
          <svg viewBox="0 0 160 112">
            <path class="cover-book-shadow" d="M80 34c-15-12-34-14-54-8v54c19-6 38-4 54 8 16-12 35-14 54-8V26c-20-6-39-4-54 8Z" />
            <path class="cover-book-page" d="M80 37c-14-11-31-12-48-7v43c17-5 34-3 48 8V37Z" />
            <path class="cover-book-page cover-book-page--right" d="M80 37c14-11 31-12 48-7v43c-17-5-34-3-48 8V37Z" />
            <path class="cover-book-line" d="M43 43c10-2 19-1 27 3M43 53c10-2 19-1 27 3M90 46c8-4 17-5 27-3M90 56c8-4 17-5 27-3" />
            <path class="cover-book-spine" d="M80 37v44" />
          </svg>
        </div>
      </header>

      <div class="detail-layout">
        <main class="detail-main">
          <section class="detail-section">
            <div class="detail-section-heading">
              <div>
                <span class="section-kicker">COURSE OVERVIEW</span>
                <h2>课程概述</h2>
              </div>
            </div>
            <p class="overview-copy">{{ overviewText }}</p>
          </section>

          <section class="detail-section goal-section">
            <div class="detail-section-heading">
              <div>
                <span class="section-kicker">YOUR LEARNING GOAL</span>
                <h2>学习目标</h2>
              </div>
            </div>
            <p>{{ goalText }}</p>
          </section>

          <section class="detail-section outline-section">
            <div class="detail-section-heading">
              <div>
                <span class="section-kicker">COURSE OUTLINE</span>
                <h2>课程大纲</h2>
              </div>
              <span class="outline-count">{{ course.chapters.length }} 章 · {{ totalUnits }} 个小节</span>
            </div>

            <!-- PPT 进度条：只在"有课件要生成 / 正在生成"时出现 -->
            <div class="ppt-strip">
              <div class="ppt-strip-copy">
                <strong>课件进度 {{ readyUnits }} / {{ totalUnits }}</strong>
                <span v-if="runningRow">
                  正在生成「{{ runningRow.title }}」{{ runningRow.jobStage ? ' · ' + runningRow.jobStage : '' }}
                </span>
                <span v-else-if="nextUnit">
                  下一课：{{ nextUnit.title }}（上到哪一课，就生成哪一课）
                </span>
                <span v-else>全部单元的课件都已生成，可以直接上课</span>
              </div>
              <div class="ppt-strip-actions">
                <template v-if="runningRow">
                  <span class="ppt-percent">{{ Math.round(runningRow.jobPercent) }}%</span>
                  <button class="btn" type="button" @click="cancelRunning()">取消</button>
                </template>
                <button
                  v-else-if="nextUnit && canGenerate"
                  class="btn btn-primary"
                  type="button"
                  :disabled="starting"
                  @click="generateNext()"
                >
                  {{ starting ? '正在启动…' : '生成下一课 PPT' }}
                </button>
              </div>
            </div>

            <ol class="chapter-list">
              <li v-for="chapter in chapters" :key="chapter.index" class="chapter-block">
                <button class="chapter-head" type="button" @click="toggleChapter(String(chapter.index))">
                  <span class="chapter-no" aria-hidden="true">{{ chapter.index + 1 }}</span>
                  <span class="chapter-copy">
                    <strong>{{ chapter.title }}</strong>
                    <small>{{ chapter.units.length }} 个小节 · {{ chapter.units.filter((u) => u.hasPpt).length }} 个已有课件</small>
                  </span>
                  <span class="chapter-chevron" :class="{ 'chapter-chevron--closed': collapsed[String(chapter.index)] }" aria-hidden="true">
                    <svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6" /></svg>
                  </span>
                </button>

                <ol v-show="!collapsed[String(chapter.index)]" class="unit-list">
                  <li v-for="row in chapter.units" :key="row.id" class="unit-row">
                    <span
                      class="unit-dot"
                      :class="row.hasPpt ? 'unit-dot--ready' : 'unit-dot--pending'"
                      :title="row.hasPpt ? '课件已生成' : '课件还没生成'"
                      aria-hidden="true"
                    >
                      <template v-if="row.hasPpt">✓</template>
                      <template v-else>{{ row.unitIndex + 1 }}</template>
                    </span>

                    <RouterLink
                      v-if="row.hasPpt"
                      class="unit-main"
                      :to="unitRoute(row)"
                      :aria-label="`${row.title}，${unitStatusText({ percent: row.progressPercent, last_slide: 0, status: row.progressStatus }).label}，进入学习`"
                    >
                      <span class="unit-title">{{ row.title }}</span>
                      <span class="unit-meta">
                        <template v-if="row.jobStatus === 'running'">生成中 {{ Math.round(row.jobPercent) }}%</template>
                        <template v-else-if="row.progressPercent > 0">{{ row.progressPercent }}%</template>
                        <template v-else>未开始</template>
                      </span>
                    </RouterLink>
                    <span v-else class="unit-main unit-main--pending">
                      <span class="unit-title">{{ row.title }}</span>
                      <span class="unit-meta">
                        <template v-if="row.jobStatus === 'running' || row.jobStatus === 'queued'">
                          {{ row.jobStage || '排队中' }} {{ Math.round(row.jobPercent) }}%
                        </template>
                        <template v-else-if="row.jobStatus === 'failed'">生成失败</template>
                        <template v-else>课件待生成</template>
                      </span>
                    </span>

                    <span v-if="row.kp.length" class="unit-kp">
                      <span v-for="(kp, kpIndex) in row.kp.slice(0, 4)" :key="kpIndex" class="kp-chip">{{ kp }}</span>
                      <span v-if="row.kp.length > 4" class="kp-chip kp-chip--more">+{{ row.kp.length - 4 }}</span>
                    </span>

                    <span class="unit-actions">
                      <RouterLink
                        v-if="row.hasPpt"
                        class="unit-action"
                        :to="unitRoute(row)"
                      >进入学习</RouterLink>
                      <button
                        v-else-if="canGenerate"
                        class="unit-action unit-action--primary"
                        type="button"
                        :disabled="starting || !!runningRow"
                        @click="generateOne(row)"
                      >生成课件</button>
                      <RouterLink
                        v-if="row.hasPpt"
                        class="unit-action unit-action--ghost"
                        :to="{ name: 'app-unit-outline', params: { unitId: row.id } }"
                      >大纲</RouterLink>
                    </span>

                    <p v-if="row.jobStatus === 'failed' && row.error" class="unit-error">{{ row.error }}</p>
                  </li>
                </ol>
              </li>
            </ol>

            <p v-if="!canGenerate" class="outline-note">
              这门课没有结构化大纲（扫描或导入得来的），所以只能上已经备好课的单元。
              想要"一课一课生成课件"，请在「生成课堂」里重新建一门课。
            </p>
          </section>
        </main>

        <aside class="detail-sidebar">
          <section class="course-info-card">
            <h2>课程信息</h2>
            <dl>
              <div><dt>课程来源</dt><dd>{{ sourceLabel(course.source) }}</dd></div>
              <div><dt>学科</dt><dd>{{ course.subject || '未标注' }}</dd></div>
              <div><dt>章 / 小节</dt><dd>{{ course.chapters.length }} / {{ totalUnits }}</dd></div>
              <div><dt>已生成课件</dt><dd>{{ readyUnits }} / {{ totalUnits }}</dd></div>
              <div><dt>课件页数</dt><dd>{{ slideCount }} 页</dd></div>
              <div><dt>预计时长</dt><dd>{{ duration }}</dd></div>
              <div><dt>已完成</dt><dd>{{ completedCount }} / {{ totalUnits }}</dd></div>
            </dl>
            <div class="info-progress" aria-hidden="true"><i :style="{ width: `${percent}%` }" /></div>
          </section>
          <RouterLink class="back-to-library" :to="{ name: 'app-courses' }">
            返回我的课堂
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6" /></svg>
          </RouterLink>
          <button class="remove-course" type="button" :disabled="deleting" @click="removeCourse">
            {{ deleting ? '正在移除…' : '从课程库移除' }}
          </button>
        </aside>
      </div>
    </template>

    <section v-else class="detail-not-found">
      <span class="not-found-mark" aria-hidden="true">?</span>
      <h1>没有找到这门课堂</h1>
      <p>课堂可能已被移除，或者课程库还没扫描到它。</p>
      <RouterLink class="btn btn-primary" :to="{ name: 'app-courses' }">返回我的课堂</RouterLink>
    </section>
  </div>
</template>

<style scoped>
.course-detail-page { max-width: 1160px; margin-inline: auto; }
.detail-back { display: inline-flex; align-items: center; gap: 7px; margin: 0 0 18px; color: var(--muted); font-size: 12px; transition: color 140ms ease; }
.detail-back:hover { color: var(--accent); }
.detail-back svg, .back-to-library svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.detail-loading { display: flex; align-items: center; gap: 10px; padding: 40px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }

.detail-hero { position: relative; display: flex; min-height: 220px; align-items: center; justify-content: space-between; gap: 28px; overflow: hidden; padding: 28px 34px; border: 1px solid color-mix(in oklch, var(--accent) 17%, var(--border)); border-radius: 18px; background: linear-gradient(112deg, color-mix(in oklch, var(--accent) 10%, var(--surface)), var(--surface) 78%); }
.detail-hero::after { position: absolute; right: 8%; bottom: -128px; width: 300px; height: 300px; border: 1px solid color-mix(in oklch, var(--accent) 12%, transparent); border-radius: 50%; content: ''; }
.detail-hero-copy { position: relative; z-index: 1; max-width: 690px; }
.detail-hero .eyebrow { color: var(--accent); }
.detail-hero h1 { margin-top: 9px; font-size: clamp(26px, 4vw, 38px); font-weight: 680; letter-spacing: -.035em; line-height: 1.25; }
.detail-tags { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
.detail-tags span { padding: 6px 9px; border: 1px solid color-mix(in oklch, var(--accent) 18%, var(--border)); border-radius: 7px; background: color-mix(in oklch, var(--surface) 80%, transparent); color: var(--muted); font-size: 10px; }
.detail-hero-art { position: relative; z-index: 1; display: grid; width: 190px; height: 150px; flex: none; place-items: center; }
.detail-hero-art::before { position: absolute; width: 156px; height: 156px; border: 1px solid color-mix(in oklch, var(--accent) 20%, transparent); border-radius: 50%; content: ''; }
.detail-hero-art svg { width: 160px; filter: drop-shadow(0 10px 12px rgb(35 23 54 / 13%)); }
.cover-book-shadow { fill: color-mix(in oklch, var(--accent) 32%, #b8a8d1); }
.cover-book-page { fill: #fffdfd; stroke: color-mix(in oklch, var(--accent) 18%, var(--border)); stroke-width: 1.2; }
.cover-book-page--right { fill: color-mix(in oklch, var(--accent) 4%, #fff); }
.cover-book-line, .cover-book-spine { fill: none; stroke: color-mix(in oklch, var(--accent) 36%, #9583ae); stroke-linecap: round; stroke-width: 1.5; }

.detail-layout { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 22px; align-items: start; margin-top: 22px; }
.detail-main { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
.detail-section, .course-info-card { border: 1px solid var(--border); border-radius: 15px; background: var(--surface); }
.detail-section { padding: 22px 24px; }
.detail-section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.section-kicker { color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .08em; }
.detail-section-heading h2 { margin-top: 5px; font-size: 18px; font-weight: 650; }
.outline-count { padding: 6px 9px; border: 1px solid var(--border-soft); border-radius: 7px; color: var(--muted); font-size: 10px; }
.overview-copy { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.85; white-space: pre-wrap; overflow-wrap: anywhere; }
.goal-section > p { color: var(--muted); font-size: 13px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }

/* ── 课件进度条 ───────────────────────────────────────────────── */
.ppt-strip { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; padding: 11px 13px; border: 1px solid color-mix(in oklch, var(--accent) 16%, var(--border)); border-radius: 11px; background: color-mix(in oklch, var(--accent) 4%, var(--bg)); }
.ppt-strip-copy { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.ppt-strip-copy strong { font-size: 11.5px; font-weight: 650; }
.ppt-strip-copy span { color: var(--muted); font-size: 10.5px; line-height: 1.5; }
.ppt-strip-actions { display: flex; flex: none; align-items: center; gap: 8px; }
.ppt-strip-actions .btn { padding: 6px 11px; font-size: 11px; }
.ppt-percent { color: var(--accent); font-family: var(--font-mono); font-size: 11px; }

/* ── 章 → 单元（截图里那套层级）────────────────────────────────── */
.chapter-list { display: flex; flex-direction: column; gap: 10px; margin: 0; padding: 0; list-style: none; }
.chapter-block { min-width: 0; }
.chapter-head { display: flex; width: 100%; align-items: center; gap: 12px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 12px; background: color-mix(in oklch, var(--surface) 88%, var(--border-soft)); color: var(--fg); cursor: pointer; font: inherit; text-align: left; transition: border-color 140ms ease; }
.chapter-head:hover { border-color: color-mix(in oklch, var(--accent) 35%, var(--border)); }
.chapter-no { display: grid; width: 30px; height: 30px; flex: none; place-items: center; border-radius: 10px; background: var(--bg); color: var(--accent); font-family: var(--font-mono); font-size: 13px; font-weight: 600; }
.chapter-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 3px; }
.chapter-copy strong { font-size: 13.5px; font-weight: 650; line-height: 1.4; }
.chapter-copy small { color: var(--meta); font-size: 10px; }
.chapter-chevron { flex: none; color: var(--meta); transition: transform 180ms ease; }
.chapter-chevron svg { display: block; width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }
.chapter-chevron--closed { transform: rotate(-90deg); }

.unit-list { display: flex; flex-direction: column; gap: 2px; margin: 6px 0 0 18px; padding: 2px 0 2px 26px; border-left: 1px dashed var(--border); list-style: none; }
.unit-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 7px 6px 7px 0; border-radius: 8px; transition: background 140ms ease; }
.unit-row:hover { background: color-mix(in oklch, var(--accent) 5%, var(--surface)); }
.unit-dot { display: grid; width: 22px; height: 22px; flex: none; place-items: center; border-radius: 50%; font-family: var(--font-mono); font-size: 10px; font-weight: 700; }
.unit-dot--ready { border: 1px solid color-mix(in oklch, var(--success) 42%, var(--border)); background: color-mix(in oklch, var(--success) 14%, var(--bg)); color: color-mix(in oklch, var(--success) 72%, #0f5132); }
.unit-dot--pending { border: 1px solid color-mix(in oklch, var(--warn) 45%, var(--border)); background: color-mix(in oklch, var(--warn) 13%, var(--bg)); color: color-mix(in oklch, var(--warn) 78%, #7a4c00); }
.unit-main { display: flex; min-width: 0; flex: 1; align-items: center; gap: 10px; padding: 2px 4px; color: var(--fg); font-size: 12px; line-height: 1.55; text-decoration: none; }
.unit-main--pending { color: var(--muted); }
.unit-main:hover { color: var(--accent); }
.unit-title { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.unit-meta { flex: none; color: var(--meta); font-size: 9.5px; white-space: nowrap; }
.unit-kp { display: flex; flex-wrap: wrap; gap: 4px; width: 100%; padding-left: 32px; }
.kp-chip { padding: 2px 7px; border: 1px solid var(--border-soft); border-radius: 999px; background: var(--bg); color: var(--muted); font-size: 9px; }
.kp-chip--more { color: var(--meta); }
.unit-actions { display: flex; flex: none; align-items: center; gap: 5px; }
.unit-action { padding: 4px 9px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg); color: var(--muted); cursor: pointer; font: inherit; font-size: 9.5px; text-decoration: none; transition: border-color 130ms ease, color 130ms ease; }
.unit-action:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.unit-action--primary { border-color: color-mix(in oklch, var(--accent) 40%, var(--border)); color: var(--accent); }
.unit-action--ghost { color: var(--meta); }
.unit-action:disabled { cursor: not-allowed; opacity: .45; }
.unit-error { width: 100%; margin: 2px 0 0 32px; color: var(--danger); font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
.outline-note { margin: 16px 0 0; padding-top: 12px; border-top: 1px solid var(--border-soft); color: var(--meta); font-size: 10.5px; line-height: 1.7; }

.detail-sidebar { position: sticky; top: 80px; }
.course-info-card { padding: 18px; }
.course-info-card h2 { font-size: 14px; font-weight: 650; }
.course-info-card dl { margin: 10px 0 0; }
.course-info-card dl > div { display: flex; justify-content: space-between; gap: 12px; padding: 11px 0; border-bottom: 1px solid var(--border-soft); font-size: 11px; }
.course-info-card dl > div:last-child { border-bottom: 0; }
.course-info-card dt { color: var(--muted); }
.course-info-card dd { max-width: 58%; margin: 0; color: var(--fg); text-align: right; overflow-wrap: anywhere; }
.info-progress { height: 2px; margin-top: 14px; border-radius: 2px; background: var(--border-soft); }
.info-progress i { display: block; height: 100%; border-radius: 2px; background: var(--accent); }
.back-to-library { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface); color: var(--muted); font-size: 11px; transition: border-color 140ms ease, color 140ms ease; }
.back-to-library:hover { border-color: var(--accent); color: var(--accent); }
.remove-course { width: 100%; margin-top: 9px; padding: 9px 12px; border: 1px solid var(--border); border-radius: 11px; background: transparent; color: var(--danger); cursor: pointer; font: inherit; font-size: 10px; }
.remove-course:hover { border-color: color-mix(in oklch, var(--danger) 40%, var(--border)); background: color-mix(in oklch, var(--danger) 4%, transparent); }
.remove-course:disabled { cursor: not-allowed; opacity: .5; }
.detail-not-found { display: flex; min-height: 50vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.not-found-mark { display: grid; width: 46px; height: 46px; place-items: center; border-radius: 15px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 20px; }
.detail-not-found h1 { margin-top: 15px; font-size: 21px; }
.detail-not-found p { max-width: 48ch; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.detail-not-found .btn { margin-top: 17px; }

@media (max-width: 900px) {
  .detail-layout { grid-template-columns: minmax(0, 1fr) 245px; gap: 15px; }
  .detail-hero { padding-inline: 24px; }
  .detail-hero-art { width: 145px; }
}

@media (max-width: 700px) {
  .detail-hero { min-height: unset; padding: 22px; }
  .detail-hero-art { display: none; }
  .detail-layout { grid-template-columns: minmax(0, 1fr); }
  .detail-sidebar { position: static; grid-row: 1; }
  .detail-section { padding: 18px 16px; }
  .unit-list { margin-left: 8px; padding-left: 14px; }
  .unit-kp { padding-left: 0; }
  .unit-meta { display: none; }
}
</style>
