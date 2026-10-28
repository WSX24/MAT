<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { errorText } from '@/api/format'
import { DEMO_COURSE_ID } from '@/data/demoCourses'

/**
 * 兼容 MAT 文档里的深链 `/learn/<unitId>`（设计文档 §6 的「上课」地址）。
 *
 * 旧设计的工作区路由是 `/app/courses/:id/units/:n/lessons/:m`，而设计文档/旧书签写的是
 * `/learn/<unitId>`。这里把 unitId 反查成「课程 + 章 + 单元」再重定向过去，
 * 两条入口都能用，文档里的地址不会 404。
 */
const route = useRoute()
const router = useRouter()
const error = ref('')
const loading = ref(true)

onMounted(async () => {
  const unitId = String(route.params.unitId ?? '')
  try {
    const unit = await api.getUnit(unitId)
    const course = await api.getCourse(unit.course_id)
    for (let chapterIndex = 0; chapterIndex < course.chapters.length; chapterIndex += 1) {
      const chapter = course.chapters[chapterIndex]
      for (let unitIndex = 0; unitIndex < chapter.units.length; unitIndex += 1) {
        if (chapter.units[unitIndex].id === unitId) {
          const slide = route.query.slide
          await router.replace({
            name: 'app-lesson',
            params: {
              id: course.id,
              unitNumber: String(chapterIndex + 1),
              lessonNumber: String(unitIndex + 1),
            },
            query: slide == null ? {} : { slide: String(slide) },
          })
          return
        }
      }
    }
    error.value = '这个单元不在任何课程里：课程可能被重新扫描过'
  } catch (caught) {
    error.value = errorText(caught)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="learn-redirect">
    <div v-if="loading" class="learn-loading" role="status">
      <span class="loading-bar" aria-hidden="true" />
      <span>正在定位这一节课…</span>
    </div>
    <section v-else class="learn-panel">
      <span class="panel-mark" aria-hidden="true">!</span>
      <h1>这节课打不开</h1>
      <p>{{ error }}</p>
      <RouterLink class="btn btn-primary" :to="{ name: 'app-courses' }">回我的课堂</RouterLink>
    </section>
    <span class="hidden-demo" aria-hidden="true">{{ DEMO_COURSE_ID }}</span>
  </div>
</template>

<style scoped>
.learn-redirect { max-width: 900px; margin-inline: auto; }
.learn-loading { display: flex; align-items: center; gap: 10px; padding: 44px 4px; color: var(--muted); font-size: 12px; }
.loading-bar { width: 42px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--border), var(--accent), var(--border)); background-size: 200% 100%; animation: bar 1.2s linear infinite; }
@keyframes bar { to { background-position: -200% 0; } }
.learn-panel { display: flex; min-height: 50vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.panel-mark { display: grid; width: 44px; height: 44px; place-items: center; border-radius: 14px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 18px; }
.learn-panel h1 { margin-top: 14px; font-size: 20px; }
.learn-panel p { max-width: 48ch; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.learn-panel .btn { margin-top: 16px; }
.hidden-demo { display: none; }
</style>
