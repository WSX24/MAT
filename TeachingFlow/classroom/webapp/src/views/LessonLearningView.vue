<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import ClassroomSession from '@/features/classroom/ClassroomSession.vue'
import { api } from '@/api/client'
import { errorText } from '@/api/format'
import { findUnit } from '@/api/view'
import type { CourseDetail, SessionCreated, UnitDetail } from '@/api/types'
import { activeProfile, appState, loadProfiles } from '@/state/app'

/**
 * 小节学习 = 真实课堂（课件重绘 + 字幕 + 提问 + 翻页/暂停/语速）。
 *
 * 路由沿用旧形状（课程 / 第几单元 / 第几小节），进入时按章-单元索引换成真实 unit.id，
 * 再开一次会话；打不开就说明原因，不拿别的内容顶上。
 */
const route = useRoute()

const course = ref<CourseDetail | null>(null)
const unit = ref<UnitDetail | null>(null)
const session = ref<SessionCreated | null>(null)
const booting = ref(true)
const bootError = ref('')

const unitNumber = computed(() => Number(route.params.unitNumber))
const lessonNumber = computed(() => Number(route.params.lessonNumber))
const courseId = computed(() => String(route.params.id ?? ''))

const startSlide = computed(() => {
  const raw = route.query.slide
  const value = Array.isArray(raw) ? raw[0] : raw
  if (value == null || value === '') return 0
  const parsed = Number.parseInt(String(value), 10)
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0
})

async function boot() {
  booting.value = true
  bootError.value = ''
  session.value = null
  unit.value = null
  course.value = null

  try {
    const detail = await api.getCourse(courseId.value)
    course.value = detail
    const found = findUnit(detail, unitNumber.value, lessonNumber.value)
    if (!found) throw new Error('这个单元不在课程里：课程可能被重新扫描过，回课程大纲重新选一节')

    let detailUnit = await api.getUnit(found.unit.id)
    if (!detailUnit.ready) {
      const prepared = await api.prepareUnit(found.unit.id)
      if (!prepared.ok && !prepared.deck_ready) {
        throw new Error(prepared.warnings?.[0] || '这个单元的课件还没备好（可能缺源文件）')
      }
      detailUnit = await api.getUnit(found.unit.id)
    }

    if (!activeProfile.value) await loadProfiles()
    if (!activeProfile.value) {
      throw new Error('还没有学习档案：先在左下角新建一个档案，再回来上课')
    }

    const created = await api.createSession({
      unit_id: found.unit.id,
      profile_id: activeProfile.value.id,
      start_slide: startSlide.value || undefined,
    })
    unit.value = detailUnit
    session.value = created
  } catch (error) {
    bootError.value = errorText(error)
  } finally {
    booting.value = false
  }
}

const backRoute = computed(() => ({ name: 'app-course-detail', params: { id: courseId.value } }))

onMounted(boot)
</script>

<template>
  <div class="lesson-page">
    <RouterLink v-if="!(session && unit)" class="lesson-back" :to="backRoute">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回课程大纲
    </RouterLink>

    <div v-if="booting" class="lesson-loading" role="status">
      <span class="loading-bar" aria-hidden="true" />
      <span>正在打开课堂…</span>
    </div>

    <section v-else-if="bootError" class="lesson-panel lesson-panel--error">
      <span class="lesson-panel-icon" aria-hidden="true">!</span>
      <h1>课堂没能打开</h1>
      <p>{{ bootError }}</p>
      <div class="lesson-panel-actions">
        <button class="btn btn-primary" type="button" @click="boot">重试</button>
        <RouterLink class="btn" :to="backRoute">返回课程大纲</RouterLink>
        <RouterLink v-if="appState.profilesStatus === 'error'" class="btn" :to="{ name: 'app-courses' }">回课程库</RouterLink>
      </div>
    </section>

    <ClassroomSession
      v-else-if="session && unit"
      :key="`${session.session_id}:${startSlide}`"
      :unit="unit"
      :session="session"
      :start-slide="startSlide || session.position.slide"
      :course-id="courseId"
      :unit-number="unitNumber"
      :lesson-number="lessonNumber"
    />

    <section v-else class="lesson-panel">
      <span class="lesson-panel-icon" aria-hidden="true">?</span>
      <h1>没有找到这个小节</h1>
      <p>课程或小节地址可能已失效，请回到课程列表重新选择。</p>
      <RouterLink class="btn btn-primary" :to="{ name: 'app-courses' }">返回我的课堂</RouterLink>
    </section>
  </div>
</template>

<style scoped>
.lesson-page { max-width: 1280px; min-height: 60vh; margin-inline: auto; }
.lesson-back { display: inline-flex; align-items: center; gap: 7px; margin-bottom: 16px; color: var(--muted); font-size: 12px; transition: color 140ms ease; }
.lesson-back:hover { color: var(--accent); }
.lesson-back svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.lesson-loading { display: flex; align-items: center; gap: 10px; padding: 44px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }

.lesson-panel { display: flex; min-height: 50vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.lesson-panel-icon { display: grid; width: 46px; height: 46px; place-items: center; border-radius: 15px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 20px; }
.lesson-panel h1 { margin-top: 15px; font-size: 21px; }
.lesson-panel p { max-width: 52ch; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.lesson-panel-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 9px; margin-top: 17px; }

</style>
