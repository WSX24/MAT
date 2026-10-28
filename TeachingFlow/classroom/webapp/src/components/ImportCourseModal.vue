<script setup lang="ts">
import { ref } from 'vue'
import { api } from '@/api/client'
import { errorText } from '@/api/format'
import { toast } from '@/state/toast'

/**
 * 导入课件（POST /api/v1/courses/import）。
 *
 * 后端接受 .pptx，导入后顺手备一次课，所以可能等几秒到几十秒（没配 key 时是规则兜底，秒级）。
 * 这里不做进度条，只把「正在导入」说清楚，失败给可读原因。
 */
const emit = defineEmits<{
  close: []
  imported: [courseId: string]
}>()

const file = ref<File | null>(null)
const busy = ref(false)
const error = ref('')

function pick(event: Event) {
  const target = event.target as HTMLInputElement
  const chosen = target.files?.[0] ?? null
  if (!chosen) return
  if (!chosen.name.toLowerCase().endsWith('.pptx')) {
    error.value = '只支持 .pptx 课件（PDF / Keynote / 旧版 .ppt 请先另存为 pptx）'
    file.value = null
    return
  }
  error.value = ''
  file.value = chosen
}

async function submit() {
  if (!file.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await api.importCourse(file.value)
    const warnings = result.warnings ?? []
    toast.ok(`已导入：${result.slides} 页课件`)
    warnings.slice(0, 2).forEach((warning) => toast.warn(warning))
    emit('imported', result.course_id)
    emit('close')
  } catch (caught) {
    error.value = errorText(caught)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="modal-scrim" role="presentation" @click.self="!busy && emit('close')">
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="import-title">
      <header class="modal-head">
        <div>
          <span class="modal-kicker">IMPORT DECK</span>
          <h2 id="import-title">导入课件</h2>
        </div>
        <button class="modal-close" type="button" aria-label="关闭" :disabled="busy" @click="emit('close')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
      </header>

      <p class="modal-lead">
        上传一份 <code>.pptx</code>，它会立刻进课程库并顺手备一次课。导入的课件走「导入课件」这一来源，
        和出片产物并列。
      </p>

      <label class="drop-zone" :class="{ 'drop-zone--chosen': file, 'drop-zone--busy': busy }">
        <input class="visually-hidden-input" type="file" accept=".pptx" :disabled="busy" @change="pick">
        <svg class="drop-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 16V4m-4.5 4.5L12 4l4.5 4.5M4 15.5v2.5A1.5 1.5 0 0 0 5.5 19.5h13A1.5 1.5 0 0 0 20 18v-2.5" />
        </svg>
        <strong>{{ file ? file.name : '选择或拖入 .pptx 文件' }}</strong>
        <span>{{ file ? `${(file.size / 1024 / 1024).toFixed(1)} MB · 点击可重新选择` : '单个文件，最大 200MB' }}</span>
      </label>

      <p v-if="error" class="modal-error" role="alert">{{ error }}</p>

      <footer class="modal-foot">
        <button class="btn" type="button" :disabled="busy" @click="emit('close')">取消</button>
        <button class="btn btn-primary" type="button" :disabled="!file || busy" @click="submit">
          {{ busy ? '正在导入并备课…' : '开始导入' }}
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.visually-hidden-input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
</style>
