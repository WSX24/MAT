<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { errorText, formatClock, formatDuration } from '@/api/format'
import type { CourseDetail, Recap } from '@/api/types'

/**
 * 课后小结（GET /api/v1/sessions/{id}/recap）。
 *
 * 课堂结束时把快照放进 history.state 直接渲染；刷新或从别处打开则回后端读同一份
 * （后端结束那一刻写过 recap 事件，可重放）。旧设计里没有这一页，版式按
 * 「detail-section + course-info-card」那套卡片语言补齐。
 */
interface SummaryPayload {
  recap?: Recap
  courseId?: string
  unitId?: string
  unitNumber?: number
  lessonNumber?: number
  unitTitle?: string
}

const route = useRoute()
const router = useRouter()
const sessionId = computed(() => String(route.params.id ?? ''))

/** 课堂结束时把小结塞进 history.state（JSON 字符串），刷新后拿不到就回后端读。 */
function readPayload(): SummaryPayload {
  const raw = (history.state as { summary?: string } | null)?.summary
  if (!raw) return {}
  try {
    return JSON.parse(raw) as SummaryPayload
  } catch {
    return {}
  }
}

const state = readPayload()

const recap = ref<Recap | null>(state?.recap ?? null)
const courseId = ref(state?.courseId ?? '')
const unitId = ref(state?.unitId ?? '')
const unitTitle = ref(state?.unitTitle ?? '')
const unitNumber = ref<number | null>(state?.unitNumber ?? null)
const lessonNumber = ref<number | null>(state?.lessonNumber ?? null)
const loading = ref(!recap.value)
const error = ref('')

const lessonLink = computed(() => {
  if (!courseId.value || !unitNumber.value || !lessonNumber.value) return null
  return {
    name: 'app-lesson',
    params: {
      id: courseId.value,
      unitNumber: String(unitNumber.value),
      lessonNumber: String(lessonNumber.value),
    },
  }
})
const detailLink = computed(() => ({
  name: 'app-course-detail',
  params: { id: courseId.value || '__missing__' },
}))

async function locateUnit() {
  if (!unitId.value) return
  try {
    const unit = await api.getUnit(unitId.value)
    courseId.value = courseId.value || unit.course_id
    unitTitle.value = unitTitle.value || unit.title
    if (courseId.value && (!unitNumber.value || !lessonNumber.value)) {
      const course: CourseDetail = await api.getCourse(courseId.value)
      course.chapters.forEach((chapter, chapterIndex) => {
        chapter.units.forEach((item, unitIndex) => {
          if (item.id === unitId.value) {
            unitNumber.value = chapterIndex + 1
            lessonNumber.value = unitIndex + 1
          }
        })
      })
    }
  } catch {
    /* 定位失败不影响看小结，只是少一个「继续学」入口 */
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (!recap.value) recap.value = await api.getSessionRecap(sessionId.value)
    if (!unitId.value) {
      const session = await api.getSession(sessionId.value)
      unitId.value = session.unit_id
    }
    await locateUnit()
  } catch (caught) {
    error.value = errorText(caught)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="summary-page">
    <RouterLink v-if="courseId" class="detail-back" :to="detailLink">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回课程大纲
    </RouterLink>

    <div v-if="loading" class="summary-loading" role="status">
      <span class="loading-bar" aria-hidden="true" />
      <span>正在读取课后小结…</span>
    </div>

    <section v-else-if="error && !recap" class="summary-panel">
      <span class="panel-mark" aria-hidden="true">!</span>
      <h1>小结没有读出来</h1>
      <p>{{ error }}</p>
      <div class="summary-actions">
        <button class="btn btn-primary" type="button" @click="load">重试</button>
        <RouterLink class="btn" :to="{ name: 'app-courses' }">回我的课堂</RouterLink>
      </div>
    </section>

    <template v-else>
      <header class="summary-hero">
        <span class="eyebrow">LESSON RECAP</span>
        <h1>这节课上完了。</h1>
        <p v-if="unitTitle">{{ unitTitle }}</p>
        <div class="summary-time">
          <strong>{{ formatDuration(recap?.elapsed_sec ?? 0) }}</strong>
          <span>本次学习时长</span>
        </div>
      </header>

      <div class="summary-grid">
        <section class="detail-card">
          <span class="section-kicker">WHAT HAPPENED</span>
          <h2>这次课发生了什么</h2>
          <dl class="stat-list">
            <div><dt>覆盖页数</dt><dd>{{ recap?.slides_covered ?? 0 }} 页</dd></div>
            <div><dt>讲解单元</dt><dd>{{ recap?.units_given ?? 0 }} 个</dd></div>
            <div><dt>提问</dt><dd>{{ recap?.qa_count ?? 0 }} 次</dd></div>
            <div><dt>用时</dt><dd>{{ formatClock(recap?.elapsed_sec ?? 0) }}</dd></div>
          </dl>
        </section>

        <section class="detail-card">
          <span class="section-kicker">TOPICS</span>
          <h2>讲过的话题</h2>
          <div v-if="recap?.topics?.length" class="topic-list">
            <span v-for="topic in recap.topics" :key="topic" class="topic-chip">{{ topic }}</span>
          </div>
          <p v-else class="muted-note">这次没有记录到明确的话题标签；下次上课时可以直接向老师提问，话题会自动归到这里。</p>
        </section>
      </div>

      <div class="summary-actions">
        <RouterLink v-if="lessonLink" class="btn btn-primary" :to="lessonLink">继续学这一节</RouterLink>
        <RouterLink v-if="courseId" class="btn" :to="detailLink">回课程大纲</RouterLink>
        <RouterLink class="btn" :to="{ name: 'app-courses' }">回我的课堂</RouterLink>
        <button
          class="btn"
          type="button"
          @click="router.push({ name: 'app-session-summary', params: { id: sessionId } })"
        >刷新小结</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.summary-page { max-width: 900px; margin-inline: auto; }
.detail-back { display: inline-flex; align-items: center; gap: 7px; margin: 0 0 18px; color: var(--muted); font-size: 12px; transition: color 140ms ease; }
.detail-back:hover { color: var(--accent); }
.detail-back svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.summary-loading { display: flex; align-items: center; gap: 10px; padding: 40px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }

.summary-hero { position: relative; overflow: hidden; padding: 30px 32px; border: 1px solid color-mix(in oklch, var(--accent) 16%, var(--border)); border-radius: 18px; background: linear-gradient(112deg, color-mix(in oklch, var(--accent) 9%, var(--surface)), var(--surface) 76%); }
.summary-hero .eyebrow { color: var(--accent); }
.summary-hero h1 { margin-top: 9px; font-size: clamp(26px, 4vw, 36px); font-weight: 680; letter-spacing: -.035em; }
.summary-hero > p { margin-top: 8px; color: var(--muted); font-size: 12px; }
.summary-time { display: flex; align-items: baseline; gap: 10px; margin-top: 18px; }
.summary-time strong { font-family: var(--font-mono); font-size: 26px; font-weight: 600; letter-spacing: -.03em; }
.summary-time span { color: var(--meta); font-size: 10px; }

.summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 18px; }
.detail-card { padding: 20px 22px; border: 1px solid var(--border); border-radius: 15px; background: var(--surface); }
.section-kicker { color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .08em; }
.detail-card h2 { margin-top: 5px; font-size: 17px; font-weight: 650; }
.stat-list { margin: 12px 0 0; }
.stat-list > div { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-soft); font-size: 12px; }
.stat-list > div:last-child { border-bottom: 0; }
.stat-list dt { color: var(--muted); }
.stat-list dd { margin: 0; color: var(--fg); font-family: var(--font-mono); font-size: 12px; }

.topic-list { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 13px; }
.topic-chip { padding: 5px 9px; border: 1px solid color-mix(in oklch, var(--accent) 18%, var(--border)); border-radius: 999px; background: color-mix(in oklch, var(--accent) 5%, var(--surface)); color: var(--muted); font-size: 10px; }
.muted-note { margin-top: 12px; color: var(--muted); font-size: 12px; line-height: 1.75; }

.summary-actions { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 20px; }
.summary-panel { display: flex; min-height: 40vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.panel-mark { display: grid; width: 44px; height: 44px; place-items: center; border-radius: 14px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 18px; }
.summary-panel h1 { margin-top: 14px; font-size: 20px; }
.summary-panel p { max-width: 48ch; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.summary-panel .summary-actions { justify-content: center; margin-top: 16px; }

@media (max-width: 720px) {
  .summary-grid { grid-template-columns: minmax(0, 1fr); }
  .summary-hero { padding: 22px; }
}
</style>
