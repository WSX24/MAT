<script setup lang="ts">
import { RouterLink } from 'vue-router'
import robotAuthUrl from '@/assets/robot-auth.png'

/**
 * 入口页（旧链接 `/auth` 仍然可达）。
 *
 * 平台没有账号体系 —— 学习档案在课堂里切换，所以这里不放登录/注册表单：没有校验可做，
 * 放了就是假交互。只留一个真正能进工作区的入口，不承诺不存在的账号服务。
 */
</script>

<template>
  <div class="shell-simple">
    <section class="auth-side">
      <RouterLink class="brand" to="/">
        <span class="brand-mark" aria-hidden="true">
          <svg class="icon icon-lg" viewBox="0 0 24 24">
            <rect x="5.5" y="3.5" width="13" height="11" rx="4.5" />
            <path d="M9 8.5h.01M15 8.5h.01M9.5 17.5h5M12 14.5v3M4 18.5h16" />
          </svg>
        </span>
        <span class="brand-name">智塾<small>AI TEACHING PLATFORM</small></span>
      </RouterLink>

      <div class="auth-headline">
        <h1>回到你的课堂<br>专属学习团队已经就位</h1>
        <p>和智能教师对话，打造你的个性化课堂。完整的教学周期，配套习题，学情跟踪，错题归纳</p>
        <div class="auth-facts">
          <div class="auth-fact"><span class="k">T</span>教师 · 大纲与任务</div>
          <div class="auth-fact"><span class="k">A</span>助教 · 批改与学情报告</div>
          <div class="auth-fact"><span class="k">Q</span>答疑 · 错题库与同类型题</div>
          <div class="auth-fact"><span class="k">C</span>督学 · 复习计划与简报</div>
        </div>
      </div>

      <div class="auth-mini" aria-hidden="true">
        <img :src="robotAuthUrl" alt="" width="359" height="573" decoding="async" >
      </div>
      <div class="auth-foot">智塾 · MMXXVI</div>
    </section>

    <section class="auth-main">
      <div class="auth-card">
        <h2>从这里开始</h2>
        <p class="sub">平台没有账号体系：学习进度按学习档案保存，切换档案就在课堂左下角。</p>
        <div class="auth-actions">
          <RouterLink class="btn btn-primary btn-lg btn-block" :to="{ name: 'app-generate' }">进入工作区</RouterLink>
          <RouterLink class="btn btn-block" :to="{ name: 'app-courses' }">查看我的课堂</RouterLink>
        </div>
        <p class="auth-note">第一次来，建议先在「生成课堂」里挑好模型与教学周期，再回课程库上课。</p>
      </div>
    </section>
  </div>
</template>
