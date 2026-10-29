<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { Rate, TranscriptEntry } from './useClassroom'

/**
 * 全屏演示时的右下角浮窗（对齐原型 #fsfab 的分工）。
 *
 * 为什么全屏里必须有它：进入全屏之后页面里的控制条、字幕栏都被藏起来让位给课件，
 * 视线又全在课件上、鼠标不在手边。原型把"翻页 / 暂停 / 字幕 / 提问 / 设置 / 退出全屏"
 * 全部收进右下角这一枚胶囊，全屏时抬手就能点到 —— 这一版沿用同一套信息层级：
 *
 *   常驻胶囊：上一页 / 页码 / 下一页 ｜ 暂停 · 字幕 ｜ 提问 ｜ 退出全屏
 *   展开面板：提问（快捷提问 + 输入框） / 对话（历史回看） / 设置（字幕与声音）
 *
 * 三处刻意的取舍：
 * 1. **只在全屏出现**：非全屏时页面里已经有控制条与字幕栏，再放一个浮窗就是重复。
 * 2. **不做成"另一套控制逻辑"**：所有动作都 emit 给父组件，走的还是 useClassroom 的
 *    control/ask/setRate/setVolume —— 两套状态机迟早会不同步。
 * 3. **设置项只放全屏时调得到的东西**：字幕（开关/字号/位置/颜色）与声音（音量/语速）。
 *    它们原来在侧栏与大控制条上，全屏时都够不着，所以这里必须有一份。
 */
const props = defineProps<{
  open: boolean
  paused: boolean
  rate: Rate
  volume: number
  autoplayBlocked: boolean
  slideIndex: number
  slideCount: number
  captionsOn: boolean
  subSize: number
  subPos: 'bottom' | 'top'
  subColor: string
  entries: TranscriptEntry[]
  connected: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  prev: []
  next: []
  togglePause: []
  toggleCaptions: []
  rate: [value: Rate]
  volume: [value: number]
  retryAudio: []
  subSize: [value: number]
  subPos: [value: 'bottom' | 'top']
  subColor: [value: string]
  ask: [text: string]
  exit: []
}>()

const RATES: Rate[] = [0.8, 1, 1.2]
const SIZES: Array<{ value: number; label: string }> = [
  { value: 18, label: '小' },
  { value: 22, label: '标准' },
  { value: 27, label: '大' },
  { value: 32, label: '特大' },
]
const COLORS = ['#111827', '#334155', '#1e3a8a', '#991b1b']
const QUICK_ASKS = ['这一页的重点是什么？', '能再举个例子吗？', '刚才那句没听懂']

const tab = ref<'ask' | 'talk' | 'set'>('ask')
const draft = ref('')
const talkList = ref<HTMLDivElement | null>(null)

const pageText = computed(() =>
  props.slideCount ? `${props.slideIndex + 1} / ${props.slideCount}` : '—',
)
const talkCount = computed(() => props.entries.length)
const volumePercent = computed(() => Math.round((props.volume ?? 1) * 100))
const muted = computed(() => (props.volume ?? 1) <= 0.001)

function close() {
  emit('update:open', false)
}

function submit() {
  const text = draft.value.trim()
  if (!text || !props.connected) return
  emit('ask', text)
  draft.value = ''
  close()
}

function quickAsk(text: string) {
  if (!props.connected) return
  emit('ask', text)
  close()
}

function onVolume(event: Event) {
  const input = event.target as HTMLInputElement
  emit('volume', Number(input.value) / 100)
}

function toggleMute() {
  emit('volume', muted.value ? 1 : 0)
}

function scrollTalkToBottom() {
  void nextTick(() => {
    if (talkList.value) talkList.value.scrollTop = talkList.value.scrollHeight
  })
}

watch(
  () => [props.open, tab.value, props.entries.length],
  () => {
    if (props.open && tab.value === 'talk') scrollTalkToBottom()
  },
  { immediate: true },
)
</script>

<template>
  <div class="classroom-fab" :class="{ 'classroom-fab--open': open }">
    <section
      v-if="open"
      id="classroom-fab-panel"
      class="fab-panel"
      role="dialog"
      aria-label="全屏课堂助手"
      @keydown.esc.stop="close"
    >
      <header class="fab-panel-head">
        <div class="fab-tabs" role="tablist" aria-label="浮窗内容">
          <button
            class="fab-tab"
            type="button"
            role="tab"
            :aria-selected="tab === 'ask'"
            @click="tab = 'ask'"
          >提问</button>
          <button
            class="fab-tab"
            type="button"
            role="tab"
            :aria-selected="tab === 'talk'"
            @click="tab = 'talk'"
          >对话<span class="fab-tab-count">{{ talkCount }}</span></button>
          <button
            class="fab-tab"
            type="button"
            role="tab"
            :aria-selected="tab === 'set'"
            @click="tab = 'set'"
          >设置</button>
        </div>
        <button class="fab-close" type="button" aria-label="收起浮窗" title="收起浮窗" @click="close">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
      </header>

      <!-- 提问 -->
      <div v-if="tab === 'ask'" class="fab-ask">
        <div class="fab-quick">
          <button
            v-for="text in QUICK_ASKS"
            :key="text"
            class="fab-quick-btn"
            type="button"
            :disabled="!connected"
            @click="quickAsk(text)"
          >{{ text }}</button>
        </div>
        <div class="fab-ask-row">
          <input
            v-model="draft"
            type="text"
            :disabled="!connected"
            placeholder="想问什么…（Enter 发送）"
            aria-label="输入问题"
            @keydown.enter.prevent="submit"
          >
          <button class="fab-send" type="button" :disabled="!connected || !draft.trim()" @click="submit">
            发送
          </button>
        </div>
        <p class="fab-hint">
          {{ connected ? '提问会打断讲解，答完自动接回原处。' : '课堂连接断开，恢复后可以继续提问。' }}
        </p>
      </div>

      <!-- 对话 -->
      <div v-else-if="tab === 'talk'" ref="talkList" class="fab-talk" role="log" aria-live="polite">
        <p v-if="!entries.length" class="fab-talk-empty">还没有对话：老师和你的每一句都会记在这里。</p>
        <div v-for="entry in entries" :key="entry.id" class="fab-talk-line" :class="{ 'fab-talk-line--me': entry.who === 'me' }">
          <span class="fab-talk-who">{{ entry.who === 'me' ? '你' : entry.who === 'system' ? '提示' : '老师' }}</span>
          <span class="fab-talk-text">{{ entry.text }}</span>
        </div>
      </div>

      <!-- 设置：全屏时唯一够得着的字幕与声音开关 -->
      <div v-else class="fab-set">
        <div class="fab-sec">
          <div class="fab-sec-label">
            <span>字幕</span>
            <button
              class="fab-switch"
              type="button"
              :aria-pressed="captionsOn"
              @click="emit('toggleCaptions')"
            >{{ captionsOn ? '已开启' : '已关闭' }}</button>
          </div>
          <div class="fab-ctrl-row">
            <span class="lbl">字号</span>
            <div class="fab-seg" role="group" aria-label="字幕字号">
              <button
                v-for="item in SIZES"
                :key="item.value"
                type="button"
                :aria-pressed="subSize === item.value"
                :disabled="!captionsOn"
                @click="emit('subSize', item.value)"
              >{{ item.label }}</button>
            </div>
          </div>
          <div class="fab-ctrl-row">
            <span class="lbl">位置</span>
            <div class="fab-seg" role="group" aria-label="字幕位置">
              <button type="button" :aria-pressed="subPos === 'bottom'" :disabled="!captionsOn" @click="emit('subPos', 'bottom')">底部</button>
              <button type="button" :aria-pressed="subPos === 'top'" :disabled="!captionsOn" @click="emit('subPos', 'top')">顶部</button>
            </div>
            <div class="fab-colors" role="group" aria-label="字幕颜色">
              <button
                v-for="color in COLORS"
                :key="color"
                class="fab-swatch"
                type="button"
                :style="{ background: color }"
                :aria-label="`字幕颜色 ${color}`"
                :aria-pressed="subColor === color"
                :disabled="!captionsOn"
                @click="emit('subColor', color)"
              />
            </div>
          </div>
        </div>

        <div class="fab-sec">
          <div class="fab-sec-label">
            <span>声音</span>
            <span class="fab-vol-val">{{ volumePercent }}%</span>
          </div>
          <div class="fab-vol-row">
            <button
              class="fab-icon fab-icon--sm"
              type="button"
              :aria-label="muted ? '取消静音' : '静音'"
              :title="muted ? '取消静音' : '静音'"
              @click="toggleMute"
            >
              <svg v-if="muted" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="m16 9 5 6M21 9l-5 6" /></svg>
              <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 6a9 9 0 0 1 0 12" /></svg>
            </button>
            <input
              class="fab-slider"
              type="range"
              min="0"
              max="100"
              :value="volumePercent"
              aria-label="音量"
              @input="onVolume"
            >
          </div>
          <div class="fab-ctrl-row">
            <span class="lbl">语速</span>
            <div class="fab-seg" role="group" aria-label="语速">
              <button
                v-for="value in RATES"
                :key="value"
                type="button"
                :aria-pressed="rate === value"
                @click="emit('rate', value)"
              >{{ value }}×</button>
            </div>
          </div>
          <button v-if="autoplayBlocked" class="fab-btn fab-btn--warn" type="button" @click="emit('retryAudio')">
            浏览器挡住了声音 — 点这里播放
          </button>
        </div>

        <p class="fab-hint">设置只作用于本机；退出全屏后控制条上也有同样的开关。</p>
      </div>
    </section>

    <div class="fab-dock" role="toolbar" aria-label="全屏课堂控制">
      <button class="fab-icon" type="button" aria-label="上一页" title="上一页（←）" :disabled="slideIndex <= 0" @click="emit('prev')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14.5 6-6 6 6 6" /></svg>
      </button>
      <span class="fab-page" aria-live="polite">{{ pageText }}</span>
      <button class="fab-icon" type="button" aria-label="下一页" title="下一页（→）" :disabled="!slideCount || slideIndex >= slideCount - 1" @click="emit('next')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 6 6 6-6 6" /></svg>
      </button>

      <span class="fab-divider" aria-hidden="true" />

      <button class="fab-icon" type="button" :aria-label="paused ? '继续讲解' : '暂停讲解'" :title="paused ? '继续（空格）' : '暂停（空格）'" @click="emit('togglePause')">
        <svg v-if="paused" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l10-6.5z" /></svg>
        <svg v-else viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="5" width="3.6" height="14" rx="1" /><rect x="13.4" y="5" width="3.6" height="14" rx="1" /></svg>
      </button>
      <button
        class="fab-icon"
        type="button"
        :class="{ 'fab-icon--on': captionsOn }"
        :aria-pressed="captionsOn"
        aria-label="显示或隐藏字幕"
        title="字幕"
        @click="emit('toggleCaptions')"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3" /><path d="M7.5 12h3M7.5 15.5h5M14.5 12h2" /></svg>
      </button>

      <span class="fab-divider" aria-hidden="true" />

      <button
        class="fab-ask-pill"
        type="button"
        :aria-expanded="open"
        aria-controls="classroom-fab-panel"
        @click="emit('update:open', !open)"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11.5a8.5 8.5 0 0 1-9 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.2A8.5 8.5 0 1 1 21 11.5Z" /></svg>
        提问
      </button>

      <button class="fab-icon" type="button" aria-label="退出全屏" title="退出全屏（F / Esc）" @click="emit('exit')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4H5.5A1.5 1.5 0 0 0 4 5.5V9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M4 15v3.5A1.5 1.5 0 0 0 5.5 20H9" /></svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 浮窗整体：右下角，只在自己的元素里定位（全屏时父级铺满屏幕，fixed 才是屏幕坐标） */
.classroom-fab {
  position: fixed;
  right: 18px;
  bottom: 16px;
  z-index: 90;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 10px;
}

/* ── 常驻胶囊 ────────────────────────────────────────────────────── */
.fab-dock {
  display: flex;
  align-items: center;
  gap: 3px;
  padding: 5px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: color-mix(in oklch, var(--surface) 92%, transparent);
  box-shadow: 0 10px 30px color-mix(in oklch, var(--fg) 12%, transparent);
  backdrop-filter: blur(14px) saturate(160%);
}
.fab-divider { width: 1px; height: 18px; margin: 0 3px; background: var(--border); flex: none; }

.fab-icon {
  display: inline-flex;
  width: 32px;
  height: 32px;
  flex: none;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  transition: background 140ms ease, color 140ms ease, border-color 140ms ease;
}
.fab-icon svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.9; }
.fab-icon:hover:not(:disabled) { border-color: var(--border); background: var(--bg); color: var(--fg); }
.fab-icon:disabled { cursor: not-allowed; opacity: .38; }
.fab-icon--on { background: color-mix(in oklch, var(--accent) 10%, transparent); color: var(--accent); }
.fab-icon--sm { width: 28px; height: 28px; border: 1px solid var(--border); border-radius: 8px; }
.fab-icon--sm svg { width: 15px; height: 15px; }

.fab-page {
  display: inline-flex;
  min-width: 52px;
  height: 28px;
  align-items: center;
  justify-content: center;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--surface);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.fab-ask-pill {
  display: inline-flex;
  height: 30px;
  align-items: center;
  gap: 5px;
  padding: 0 12px 0 10px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  transition: background 140ms ease, transform 140ms ease;
}
.fab-ask-pill svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 2; }
.fab-ask-pill:hover { background: var(--accent-hover); transform: translateY(-1px); }

/* ── 展开面板 ────────────────────────────────────────────────────── */
.fab-panel {
  display: flex;
  width: min(380px, calc(100vw - 32px));
  max-height: min(62dvh, 520px);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--bg);
  box-shadow: 0 22px 48px color-mix(in oklch, var(--fg) 18%, transparent);
  animation: fab-pop 160ms cubic-bezier(.16, 1, .3, 1);
}
@keyframes fab-pop {
  from { opacity: 0; transform: translateY(8px) scale(.97); }
  to { opacity: 1; transform: none; }
}

.fab-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 10px 10px 12px;
  border-bottom: 1px solid var(--border-soft);
}
.fab-tabs { display: flex; gap: 3px; padding: 2px; border-radius: 9px; background: var(--surface); }
.fab-tab {
  padding: 5px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 11.5px;
  font-weight: 500;
  transition: background 130ms ease, color 130ms ease;
}
.fab-tab[aria-selected="true"] { background: var(--bg); color: var(--accent); font-weight: 600; box-shadow: 0 1px 3px color-mix(in oklch, var(--fg) 8%, transparent); }
.fab-tab-count { margin-left: 5px; color: var(--meta); font-family: var(--font-mono); font-size: 9.5px; }
.fab-close {
  display: inline-flex;
  width: 26px;
  height: 26px;
  flex: none;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 999px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
}
.fab-close svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-width: 2; }
.fab-close:hover { background: var(--border-soft); color: var(--fg); }

.fab-ask { display: flex; flex-direction: column; gap: 9px; padding: 12px; }
.fab-quick { display: flex; flex-wrap: wrap; gap: 6px; }
.fab-quick-btn {
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  transition: border-color 130ms ease, color 130ms ease;
}
.fab-quick-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.fab-quick-btn:disabled { cursor: not-allowed; opacity: .5; }

.fab-ask-row { display: flex; gap: 6px; }
.fab-ask-row input {
  min-width: 0;
  flex: 1;
  padding: 8px 11px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  color: var(--fg);
  font: inherit;
  font-size: 12.5px;
}
.fab-ask-row input:focus { border-color: var(--accent); outline: 0; box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent) 12%, transparent); }
.fab-ask-row input::placeholder { color: var(--meta); }
.fab-send {
  flex: none;
  padding: 8px 14px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
}
.fab-send:disabled { cursor: not-allowed; opacity: .45; }
.fab-hint { margin: 0; padding: 10px 12px 12px; color: var(--meta); font-size: 10px; line-height: 1.6; }
.fab-ask .fab-hint { padding: 0; }

/* ── 设置 ────────────────────────────────────────────────────────── */
.fab-set { overflow-y: auto; padding: 0 12px; scrollbar-width: thin; }
.fab-sec { padding: 12px 0; border-bottom: 1px solid var(--border-soft); }
.fab-sec:last-of-type { border-bottom: 0; }
.fab-sec-label { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; color: var(--muted); font-size: 11px; font-weight: 600; }
.fab-vol-val { color: var(--meta); font-family: var(--font-mono); font-size: 10px; font-weight: 400; }

.fab-switch {
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 10px;
}
.fab-switch[aria-pressed="true"] { border-color: color-mix(in oklch, var(--accent) 45%, var(--border)); background: color-mix(in oklch, var(--accent) 9%, var(--bg)); color: var(--accent); }

.fab-ctrl-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.fab-ctrl-row:first-of-type { margin-top: 0; }
.fab-ctrl-row .lbl { flex: none; width: 28px; color: var(--meta); font-size: 10.5px; }

.fab-seg { display: flex; flex: 1; gap: 2px; padding: 2px; border-radius: 8px; background: var(--surface); }
.fab-seg button {
  flex: 1;
  padding: 4px 6px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 10.5px;
}
.fab-seg button[aria-pressed="true"] { background: var(--bg); color: var(--accent); font-weight: 600; box-shadow: 0 1px 2px color-mix(in oklch, var(--fg) 9%, transparent); }
.fab-seg button:disabled { cursor: not-allowed; opacity: .45; }

.fab-colors { display: flex; flex: none; gap: 5px; }
.fab-swatch {
  width: 18px;
  height: 18px;
  padding: 0;
  border: 2px solid var(--bg);
  border-radius: 50%;
  box-shadow: 0 0 0 1px var(--border);
  cursor: pointer;
  transition: transform 130ms ease, box-shadow 130ms ease;
}
.fab-swatch[aria-pressed="true"] { box-shadow: 0 0 0 2px var(--accent); transform: scale(1.08); }
.fab-swatch:disabled { cursor: not-allowed; opacity: .4; }

.fab-vol-row { display: flex; align-items: center; gap: 8px; }
.fab-slider {
  min-width: 0;
  flex: 1;
  height: 4px;
  border-radius: 999px;
  background: var(--border);
  accent-color: var(--accent);
  cursor: pointer;
}

.fab-btn {
  width: 100%;
  margin-top: 9px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  font-size: 11px;
}
.fab-btn--warn { border-color: color-mix(in oklch, var(--warn) 45%, var(--border)); background: color-mix(in oklch, var(--warn) 8%, var(--bg)); color: var(--fg); }

/* ── 对话 ────────────────────────────────────────────────────────── */
.fab-talk { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 12px 14px; scrollbar-width: thin; }
.fab-talk-empty { margin: 18px 0; color: var(--meta); font-size: 11.5px; text-align: center; }
.fab-talk-line { display: flex; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border-soft); font-size: 12px; line-height: 1.65; }
.fab-talk-line:last-child { border-bottom: 0; }
.fab-talk-who { flex: none; width: 26px; color: var(--meta); font-family: var(--font-mono); font-size: 9.5px; }
.fab-talk-line--me .fab-talk-who { color: var(--accent); }
.fab-talk-text { min-width: 0; color: var(--fg); overflow-wrap: anywhere; }

@media (max-width: 560px) {
  .classroom-fab { right: 8px; bottom: 8px; }
  .fab-panel { width: calc(100vw - 16px); max-height: 66dvh; }
}
</style>
