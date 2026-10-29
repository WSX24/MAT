import { createRouter, createWebHistory } from 'vue-router'
import AppShell from '../components/AppShell.vue'
import AuthView from '../views/AuthView.vue'
import CourseDetailView from '../views/CourseDetailView.vue'
import CoursesView from '../views/CoursesView.vue'
import GenerateView from '../views/GenerateView.vue'
import LandingView from '../views/LandingView.vue'
import LearnRedirectView from '../views/LearnRedirectView.vue'
import LessonLearningView from '../views/LessonLearningView.vue'
import OutlineView from '../views/OutlineView.vue'
import SettingsView from '../views/SettingsView.vue'
import SummaryView from '../views/SummaryView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'landing',
      component: LandingView,
      meta: { bodyClass: 'landing' },
    },
    // 旧链接 /auth 仍要能打开：这里没有登录流程，只是指向工作区的入口页。
    {
      path: '/auth',
      name: 'auth',
      component: AuthView,
      meta: { bodyClass: 'auth' },
    },
    {
      path: '/app',
      component: AppShell,
      meta: { bodyClass: 'workspace' },
      children: [
        {
          path: '',
          redirect: { name: 'app-generate' },
        },
        {
          path: 'generate',
          name: 'app-generate',
          component: GenerateView,
          meta: { title: '生成课堂' },
        },
        {
          path: 'courses',
          name: 'app-courses',
          component: CoursesView,
          meta: { title: '我的课堂' },
        },
        {
          path: 'courses/:id/units/:unitNumber/lessons/:lessonNumber',
          name: 'app-lesson',
          component: LessonLearningView,
          meta: { title: '课堂学习' },
        },
        {
          path: 'courses/:id',
          name: 'app-course-detail',
          component: CourseDetailView,
          meta: { title: '课堂详情' },
        },
        {
          path: 'units/:unitId/outline',
          name: 'app-unit-outline',
          component: OutlineView,
          meta: { title: '单元大纲' },
        },
        {
          path: 'sessions/:id/summary',
          name: 'app-session-summary',
          component: SummaryView,
          meta: { title: '课后小结' },
        },
        {
          path: 'settings',
          name: 'app-settings',
          component: SettingsView,
          meta: { title: '设置' },
        },
      ],
    },
    // 设计文档 §6 的深链（旧书签 / 验收脚本 / 文档截图用的地址）：统一转到工作区路由。
    // 课堂入口 `/learn/<unitId>` 需要把 unitId 反查成课程里的位置，走一个轻量重定向页。
    { path: '/courses/:id', redirect: (to) => ({ name: 'app-course-detail', params: { id: to.params.id } }) },
    { path: '/units/:unitId/outline', redirect: (to) => ({ name: 'app-unit-outline', params: { unitId: to.params.unitId } }) },
    { path: '/sessions/:id/summary', redirect: (to) => ({ name: 'app-session-summary', params: { id: to.params.id } }) },
    { path: '/learn/:unitId', name: 'learn', component: LearnRedirectView, meta: { bodyClass: 'workspace', title: '课堂学习' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior(to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) return { el: to.hash, behavior: 'smooth' }
    return { top: 0 }
  },
})

export default router
