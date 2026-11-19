<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { api } from '@/api/client'
import { errorText, formatClock, formatEstimate } from '@/api/format'
import type { CourseDetail, Outline, UnitDetail } from '@/api/types'
import { unitStatusText } from '@/api/view'
import { toast } from '@/state/toast'

/**
 * 单元大纲（GET /api/v1/units/{id}/outline）。
 *
 * 旧前端的课程详情只展示「课程 → 章 → 小节」，没有单页大纲；这里按旧设计的
 * 「detail-section」卡片语言补上这一页：这单元讲什么、每页有哪些讲解单元、
 * 从某一页开始上课（?slide=N → POST /sessions 的 start_slide）。
 */
const route = useRoute()
const unitId = computed(() => String(route.params.unitId ?? ''))

const unit = ref<UnitDetail | null>(null)
const course = ref<CourseDetail | null>(null)
const outline = ref<Outline | null>(null)
const loading = ref(true)
const error = ref('')
const preparing = ref(false)

const lessonLink = computed(() => {
  if (!course.value || !unit.value) return null
  for (let chapterIndex = 0; chapterIndex < course.value.chapters.length; chapterIndex += 1) {
    const chapter = course.value.chapters[chapterIndex]
    for (let unitIndex = 0; unitIndex < chapter.units.length; unitIndex += 1) {
      if (chapter.units[unitIndex].id === unit.value.id) {
        return { chapterIndex, chapter, unitIndex }
      }
    }
  }
  return null
})

const backLink = computed(() => ({
  name: 'app-course-detail',
  params: { id: unit.value?.course_id || course.value?.id || '' },
}))

const empty = computed(() => !outline.value || outline.value.pages.length === 0)

/** plan_source 是后端内部标识，页面上给人看的话术（未知值原样显示，不猜）。 */
function planSourceLabel(source: string): string {
  if (source === 'rule') return '规则兜底'
  if (source === 'llm') return '模型备课'
  if (source === 'cache') return '缓存'
  return source
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const detail = await api.getUnit(unitId.value)
    unit.value = detail
    course.value = await api.getCourse(detail.course_id).catch(() => null)
    outline.value = await api.getUnitOutline(unitId.value)
  } catch (caught) {
    error.value = errorText(caught)
  } finally {
    loading.value = false
  }
}

async function prepare() {
  if (preparing.value) return
  preparing.value = true
  try {
    const result = await api.prepareUnit(unitId.value)
    if (!result.ok && !result.deck_ready && !result.plan_ready) {
      toast.warn(result.warnings?.[0] || '备课没有成功：可能缺源 pptx')
    } else {
      toast.ok(result.degraded ? '课件已备好（模型不可用，走规则兜底）' : '课件与大纲已生成')
    }
    await load()
  } catch (caught) {
    toast.error(errorText(caught))
  } finally {
    preparing.value = false
  }
}

function startFrom(index: number) {
  const link = lessonLink.value
  if (!link || !unit.value) return
  return {
    name: 'app-lesson',
    params: {
      id: unit.value.course_id || course.value?.id || '',
      unitNumber: String(link.chapterIndex + 1),
      lessonNumber: String(link.unitIndex + 1),
    },
    query: { slide: String(index) },
  }
}

onMounted(load)
</script>

<template>
  <div class="outline-page">
    <RouterLink class="detail-back" :to="backLink">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回课程大纲
    </RouterLink>

    <div v-if="loading" class="outline-loading" role="status">
      <span class="loading-bar" aria-hidden="true" />
      <span>正在读取单元大纲…</span>
    </div>

    <section v-else-if="error" class="outline-panel">
      <span class="panel-mark" aria-hidden="true">!</span>
      <h1>大纲没有打开</h1>
      <p>{{ error }}</p>
      <button class="btn btn-primary" type="button" @click="load">重试</button>
    </section>

    <template v-else-if="unit">
      <header class="outline-hero">
        <span class="eyebrow">UNIT OUTLINE</span>
        <h1>{{ unit.title }}</h1>
        <p v-if="lessonLink">
          {{ course?.title }}<span aria-hidden="true"> / </span>第 {{ lessonLink.chapterIndex + 1 }} 单元 · 第 {{ lessonLink.unitIndex + 1 }} 小节
        </p>
        <div class="outline-tags">
          <span>{{ unit.slides }} 页课件</span>
          <span>{{ formatEstimate(unit.est_sec) }}</span>
          <span>{{ unitStatusText(unit.progress).label }}</span>
          <span v-if="outline">{{ outline.totals.units }} 个讲解单元</span>
          <span v-if="unit.plan_source">备课来源：{{ planSourceLabel(unit.plan_source) }}</span>
        </div>
      </header>

      <section v-if="empty" class="outline-panel outline-panel--inline">
        <h2>这个单元还没有大纲</h2>
        <p>大纲来自备课产物（plan.json）。先备一次课，就能看到每页讲什么，并从某一页开始上课。</p>
        <button class="btn btn-primary" type="button" :disabled="preparing" @click="prepare">
          {{ preparing ? '正在备课…' : '备课并生成大纲' }}
        </button>
      </section>

      <section v-else class="detail-card">
        <div class="detail-card-head">
          <div>
            <span class="section-kicker">PAGE BY PAGE</span>
            <h2>这单元讲什么</h2>
          </div>
          <span class="outline-count">{{ outline?.totals.slides ?? 0 }} 页 · {{ formatClock(outline?.totals.est_sec ?? 0) }}</span>
        </div>

        <ol class="page-list">
          <li v-for="page in outline?.pages" :key="page.index" class="page-item">
            <div class="page-head">
              <span class="page-no">{{ String(page.index + 1).padStart(2, '0') }}</span>
              <strong>{{ page.title || `第 ${page.index + 1} 页` }}</strong>
              <RouterLink v-if="lessonLink && startFrom(page.index)" class="page-start" :to="startFrom(page.index)!">
                从这一页开始
              </RouterLink>
            </div>
            <ul class="seg-list">
              <li v-for="(segment, segmentIndex) in page.units" :key="segmentIndex" class="seg-item">
                <p class="seg-preview">{{ segment.preview || '（这一段的讲稿还没生成）' }}</p>
                <div class="seg-meta">
                  <span class="seg-time">{{ formatEstimate(segment.est_sec) }}</span>
                  <span v-for="kp in segment.kp" :key="kp" class="kp-chip">{{ kp }}</span>
                </div>
              </li>
            </ul>
          </li>
        </ol>
      </section>
    </template>
  </div>
</template>

<style scoped>
.outline-page { max-width: 980px; margin-inline: auto; }
.detail-back { display: inline-flex; align-items: center; gap: 7px; margin: 0 0 18px; color: var(--muted); font-size: 12px; transition: color 140ms ease; }
.detail-back:hover { color: var(--accent); }
.detail-back svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.outline-loading { display: flex; align-items: center; gap: 10px; padding: 40px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }

.outline-hero { padding: 6px 0 20px; }
.outline-hero .eyebrow { color: var(--accent); }
.outline-hero h1 { margin-top: 8px; font-size: clamp(24px, 3.4vw, 34px); font-weight: 670; letter-spacing: -.03em; line-height: 1.28; }
.outline-hero p { margin-top: 9px; color: var(--muted); font-size: 12px; }
.outline-tags { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 13px; }
.outline-tags span { padding: 5px 8px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); color: var(--muted); font-size: 10px; }

.detail-card { padding: 20px 22px; border: 1px solid var(--border); border-radius: 15px; background: var(--surface); }
.detail-card-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; padding-bottom: 13px; border-bottom: 1px solid var(--border-soft); }
.section-kicker { color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .08em; }
.detail-card-head h2 { margin-top: 5px; font-size: 18px; font-weight: 650; }
.outline-count { color: var(--meta); font-size: 10px; }

.page-list { display: flex; flex-direction: column; gap: 10px; margin: 13px 0 0; padding: 0; list-style: none; }
.page-item { padding: 11px 12px; border: 1px solid var(--border); border-radius: 11px; background: var(--bg); }
.page-head { display: flex; align-items: center; gap: 10px; }
.page-no { color: var(--accent); font-family: var(--font-mono); font-size: 10px; }
.page-head strong { flex: 1; min-width: 0; font-size: 12px; font-weight: 600; overflow-wrap: anywhere; }
.page-start { flex: none; padding: 4px 8px; border: 1px solid color-mix(in oklch, var(--accent) 28%, var(--border)); border-radius: 7px; color: var(--accent); font-size: 9px; transition: background 140ms ease; }
.page-start:hover { background: color-mix(in oklch, var(--accent) 7%, var(--bg)); }

.seg-list { display: flex; flex-direction: column; gap: 8px; margin: 9px 0 0; padding: 9px 0 0; border-top: 1px dashed var(--border-soft); list-style: none; }
.seg-item { min-width: 0; }
.seg-preview { color: var(--muted); font-size: 11px; line-height: 1.7; overflow-wrap: anywhere; }
.seg-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; margin-top: 5px; }
.seg-time { color: var(--meta); font-family: var(--font-mono); font-size: 9px; }
.kp-chip { padding: 2px 6px; border: 1px solid var(--border-soft); border-radius: 999px; background: var(--surface); color: var(--meta); font-size: 9px; }

.outline-panel { display: flex; min-height: 40vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.outline-panel--inline { min-height: unset; padding: 30px 20px; border: 1px dashed var(--border); border-radius: 14px; background: var(--surface); }
.panel-mark { display: grid; width: 44px; height: 44px; place-items: center; border-radius: 14px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 18px; }
.outline-panel h1 { margin-top: 14px; font-size: 20px; }
.outline-panel h2 { font-size: 17px; font-weight: 650; }
.outline-panel p { max-width: 52ch; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.outline-panel .btn { margin-top: 16px; }
</style>
