<script setup lang="ts">
import { computed } from 'vue'
import { formatEstimate } from '@/api/format'
import { displayNodeNo, type NodeView, type RouteView } from './model'

/**
 * 走向图详情面板：固定在右下角，只读。
 *
 * 为什么常驻一块面板而不是浮层跟随指针：图是可以拖动的，跟着鼠标飘的
 * 浮层在拖动时会一直糊在卡片上；固定面板在拖的时候完全不动，读完再关。
 */
const props = defineProps<{
  node: NodeView | null
  route: RouteView | null
}>()

const emit = defineEmits<{ close: [] }>()

function close() {
  emit('close')
}

/** 汇回格是 0 基格号，显示 +1；mergeN 为 null 表示汇回课尾，不是「第 0 格」，别跟着加。 */
const mergeText = computed(() =>
  props.route?.mergeN == null ? '课尾（之后不回主线）' : `第 ${displayNodeNo(props.route.mergeN)} 格`,
)

/** 路线的 est_sec 带符号：负数省时间、正数多花时间（与后端 Route 的口径一致，沿用原型的老约定）。 */
function routeDuration(seconds: number): string {
  if (!seconds) return '—'
  return seconds < 0 ? `省 ${formatEstimate(-seconds)}` : `多花 ${formatEstimate(seconds)}`
}
</script>

<template>
  <aside v-if="props.node || props.route" class="ug-detail" aria-label="走向图详情" role="dialog">
    <header class="ug-detail-head">
      <span class="ug-detail-kind">{{ props.node ? '主线一格' : '分支路线' }}</span>
      <button class="ug-detail-close" type="button" aria-label="关闭详情面板（Esc）" @click="close">✕</button>
    </header>

    <template v-if="props.node">
      <h2 class="ug-detail-title">{{ props.node.pageTitle || `第 ${props.node.page} 页` }}</h2>
      <dl class="ug-kv">
        <div><dt>页号</dt><dd>第 {{ props.node.page }} 页</dd></div>
        <div><dt>格号</dt><dd>第 {{ displayNodeNo(props.node.n) }} 格</dd></div>
        <div><dt>预计时长</dt><dd>{{ formatEstimate(props.node.estSec) }}</dd></div>
        <div v-if="props.node.pageEnd"><dt>页末</dt><dd>这一页讲完就落在这一格</dd></div>
      </dl>
      <p class="ug-detail-sum">{{ props.node.summary || '（这一格没有摘要）' }}</p>
      <p class="ug-detail-text">{{ props.node.preview || '（这一格的讲稿还没生成）' }}</p>
      <div v-if="props.node.kp.length" class="ug-kp">
        <span v-for="item in props.node.kp" :key="item" class="ug-kp-chip">{{ item }}</span>
      </div>
    </template>

    <template v-else-if="props.route">
      <h2 class="ug-detail-title">
        <span class="ug-kind" :class="`is-${props.route.kind}`">{{ props.route.kindLabel }}</span>
        {{ props.route.label || '未命名路线' }}
      </h2>
      <dl class="ug-kv">
        <div><dt>岔出格</dt><dd>第 {{ displayNodeNo(props.route.forkN) }} 格</dd></div>
        <div><dt>汇回格</dt><dd>{{ mergeText }}</dd></div>
        <div><dt>跨格数</dt><dd>{{ props.route.span }} 格</dd></div>
        <div><dt>跳过</dt><dd>{{ props.route.skipped }} 格</dd></div>
        <div><dt>重讲</dt><dd>{{ props.route.repeats }} 格</dd></div>
        <div><dt>时长</dt><dd>{{ routeDuration(props.route.estSec) }}</dd></div>
      </dl>
      <p v-if="props.route.summary" class="ug-detail-sum">{{ props.route.summary }}</p>
      <p v-if="props.route.reason" class="ug-detail-text">{{ props.route.reason }}</p>
      <ol v-if="props.route.steps.length" class="ug-steps">
        <li v-for="(step, index) in props.route.steps" :key="index">
          <div class="ug-step-head">
            <span class="ug-step-label">{{ step.label || step.unitId || `第 ${index + 1} 步` }}</span>
            <span class="ug-step-time">{{ formatEstimate(step.estSec) }}</span>
          </div>
          <p class="ug-step-text">{{ step.text }}</p>
        </li>
      </ol>
    </template>
  </aside>
</template>
