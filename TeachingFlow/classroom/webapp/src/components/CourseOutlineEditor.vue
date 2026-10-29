<script setup lang="ts">
import type { CourseOutline } from '@/api/types'

/**
 * 课程大纲编辑器（截图里的「章 → 单元」两级结构，可改可增删）。
 *
 * 两条约定：
 * 1. **直接改传进来的对象**（而不是再拷一份）：大纲是一棵树，逐层 emit 会让
 *    「加一个单元」变成三四层事件；这里只保证父组件拿到的是同一份实时数据，
 *    保存时由后端再规整一次（序号、id 都以后端为准）。
 * 2. **改完立刻重排序号**：章号 / 单元号 / id 都是位置的函数，不重排的话
 *    删掉第 2 章之后界面上会出现「1、3」这种断号，用户会以为数据坏了。
 */
const props = defineProps<{
  outline: CourseOutline
  disabled?: boolean
}>()

/** 按位置重排章号、单元号与 id（口径与后端 course_plan.normalize_outline 一致）。 */
function renumber() {
  props.outline.chapters.forEach((chapter, chapterIndex) => {
    chapter.number = chapterIndex + 1
    chapter.id = `ch${String(chapter.number).padStart(2, '0')}`
    chapter.units.forEach((unit, unitIndex) => {
      unit.number = unitIndex + 1
      unit.id = `${chapter.id}-u${unit.number}`
    })
  })
  props.outline.chapter_count = props.outline.chapters.length
  props.outline.unit_count = props.outline.chapters.reduce((sum, c) => sum + c.units.length, 0)
}

function addChapter() {
  if (props.disabled) return
  const number = props.outline.chapters.length + 1
  props.outline.chapters.push({
    id: `ch${String(number).padStart(2, '0')}`,
    number,
    title: '新的章节',
    summary: '',
    units: [{
      id: `ch${String(number).padStart(2, '0')}-u1`,
      number: 1,
      title: '新的单元',
      type: '新授',
      hours: 1,
      summary: '',
      knowledge_points: [],
    }],
  })
  renumber()
}

function removeChapter(index: number) {
  if (props.disabled || props.outline.chapters.length <= 1) return
  props.outline.chapters.splice(index, 1)
  renumber()
}

function moveChapter(index: number, delta: number) {
  if (props.disabled) return
  const target = index + delta
  if (target < 0 || target >= props.outline.chapters.length) return
  const [item] = props.outline.chapters.splice(index, 1)
  props.outline.chapters.splice(target, 0, item)
  renumber()
}

function addUnit(chapterIndex: number) {
  if (props.disabled) return
  const chapter = props.outline.chapters[chapterIndex]
  chapter.units.push({
    id: '',
    number: chapter.units.length + 1,
    title: '新的单元',
    type: '新授',
    hours: 1,
    summary: '',
    knowledge_points: [],
  })
  renumber()
}

function removeUnit(chapterIndex: number, unitIndex: number) {
  if (props.disabled) return
  const chapter = props.outline.chapters[chapterIndex]
  if (chapter.units.length <= 1) return
  chapter.units.splice(unitIndex, 1)
  renumber()
}

function moveUnit(chapterIndex: number, unitIndex: number, delta: number) {
  if (props.disabled) return
  const units = props.outline.chapters[chapterIndex].units
  const target = unitIndex + delta
  if (target < 0 || target >= units.length) return
  const [item] = units.splice(unitIndex, 1)
  units.splice(target, 0, item)
  renumber()
}

/** 课时只收正数：0 或负数会让后面"本单元课时"的提示词变成一句废话。 */
function normalizeHours(unit: { hours: number }) {
  const value = Number(unit.hours)
  unit.hours = Number.isFinite(value) && value > 0 ? Math.round(value * 2) / 2 : 1
}
</script>

<template>
  <section class="outline-editor" aria-label="课程大纲编辑器">
    <header class="editor-head">
      <label class="editor-title">
        <span class="editor-kicker">课程名称</span>
        <input v-model="outline.course_title" type="text" :disabled="disabled" aria-label="课程名称">
      </label>
      <div class="editor-meta">
        <label class="meta-field">
          <span>学科</span>
          <input v-model="outline.subject" type="text" :disabled="disabled" aria-label="学科">
        </label>
        <label class="meta-field">
          <span>学段</span>
          <select v-model="outline.stage" :disabled="disabled" aria-label="学段">
            <option>小学</option>
            <option>初中</option>
            <option>高中</option>
            <option>大学</option>
            <option>考研</option>
          </select>
        </label>
        <label class="meta-field meta-field--narrow">
          <span>周期（周）</span>
          <input v-model.number="outline.weeks" type="number" min="1" max="24" :disabled="disabled" aria-label="周期">
        </label>
        <label class="meta-field meta-field--narrow">
          <span>每周课时</span>
          <input v-model.number="outline.hours_per_week" type="number" min="1" max="12" :disabled="disabled" aria-label="每周课时">
        </label>
      </div>
      <p class="editor-counts">
        共 {{ outline.chapter_count }} 章 · {{ outline.unit_count }} 个单元 ·
        约 {{ (outline.weeks || 0) * (outline.hours_per_week || 0) }} 课时
      </p>
    </header>

    <ol class="chapter-list">
      <li v-for="(chapter, chapterIndex) in outline.chapters" :key="chapterIndex" class="chapter-block">
        <div class="chapter-head">
          <span class="chapter-no" aria-hidden="true">{{ chapterIndex + 1 }}</span>
          <input
            v-model="chapter.title"
            class="chapter-title"
            type="text"
            :disabled="disabled"
            :aria-label="`第 ${chapterIndex + 1} 章标题`"
          >
          <div class="row-actions">
            <button class="tiny" type="button" :disabled="disabled || chapterIndex === 0" aria-label="本章上移" @click="moveChapter(chapterIndex, -1)">↑</button>
            <button class="tiny" type="button" :disabled="disabled || chapterIndex === outline.chapters.length - 1" aria-label="本章下移" @click="moveChapter(chapterIndex, 1)">↓</button>
            <button class="tiny tiny--danger" type="button" :disabled="disabled || outline.chapters.length <= 1" aria-label="删除本章" @click="removeChapter(chapterIndex)">删除</button>
          </div>
        </div>

        <ul class="unit-list">
          <li v-for="(unit, unitIndex) in chapter.units" :key="unitIndex" class="unit-row">
            <span class="unit-no" aria-hidden="true">{{ chapterIndex + 1 }}.{{ unitIndex + 1 }}</span>
            <input
              v-model="unit.title"
              class="unit-title"
              type="text"
              :disabled="disabled"
              :aria-label="`第 ${chapterIndex + 1}.${unitIndex + 1} 单元标题`"
            >
            <span class="unit-hours">
              <input
                v-model.number="unit.hours"
                type="number"
                min="0.5"
                max="12"
                step="0.5"
                :disabled="disabled"
                :aria-label="`${unit.title} 课时`"
                @change="normalizeHours(unit)"
              >
              <em>课时</em>
            </span>
            <div class="row-actions">
              <button class="tiny" type="button" :disabled="disabled || unitIndex === 0" aria-label="单元上移" @click="moveUnit(chapterIndex, unitIndex, -1)">↑</button>
              <button class="tiny" type="button" :disabled="disabled || unitIndex === chapter.units.length - 1" aria-label="单元下移" @click="moveUnit(chapterIndex, unitIndex, 1)">↓</button>
              <button class="tiny tiny--danger" type="button" :disabled="disabled || chapter.units.length <= 1" aria-label="删除单元" @click="removeUnit(chapterIndex, unitIndex)">删除</button>
            </div>
            <span v-if="unit.knowledge_points.length" class="kp-line">
              <span class="kp-label">知识点</span>
              <span v-for="(kp, kpIndex) in unit.knowledge_points" :key="kpIndex" class="kp-chip">{{ kp }}</span>
            </span>
          </li>
        </ul>

        <button class="add-unit" type="button" :disabled="disabled" @click="addUnit(chapterIndex)">
          + 添加单元
        </button>
      </li>
    </ol>

    <button class="add-chapter" type="button" :disabled="disabled" @click="addChapter">
      + 添加一章
    </button>
  </section>
</template>

<style scoped>
.outline-editor { display: flex; flex-direction: column; gap: 14px; }

.editor-head { display: flex; flex-direction: column; gap: 10px; padding: 14px 15px; border: 1px solid var(--border); border-radius: 12px; background: var(--bg); }
.editor-kicker { display: block; margin-bottom: 4px; color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .06em; }
.editor-title input { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--fg); font: inherit; font-size: 15px; font-weight: 650; }
.editor-title input:focus { outline: 2px solid color-mix(in oklch, var(--accent) 40%, transparent); outline-offset: -1px; }
.editor-meta { display: flex; flex-wrap: wrap; gap: 10px; }
.meta-field { display: flex; min-width: 120px; flex: 1; flex-direction: column; gap: 4px; }
.meta-field--narrow { min-width: 88px; flex: 0 0 96px; }
.meta-field span { color: var(--meta); font-size: 10px; }
.meta-field input, .meta-field select { padding: 5px 7px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface); color: var(--fg); font: inherit; font-size: 11px; }
.editor-counts { margin: 0; color: var(--meta); font-size: 10px; }

.chapter-list { display: flex; flex-direction: column; gap: 12px; margin: 0; padding: 0; list-style: none; }
.chapter-block { padding: 12px 13px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
.chapter-head { display: flex; align-items: center; gap: 9px; }
.chapter-no { display: grid; width: 26px; height: 26px; flex: none; place-items: center; border-radius: 9px; background: color-mix(in oklch, var(--accent) 10%, var(--bg)); color: var(--accent); font-family: var(--font-mono); font-size: 12px; font-weight: 600; }
.chapter-title { min-width: 0; flex: 1; padding: 6px 8px; border: 1px solid transparent; border-radius: 7px; background: transparent; color: var(--fg); font: inherit; font-size: 13px; font-weight: 650; }
.chapter-title:hover:not(:disabled) { border-color: var(--border); }
.chapter-title:focus { border-color: var(--accent); outline: 0; background: var(--bg); }
.row-actions { display: flex; flex: none; gap: 4px; }
.tiny { padding: 3px 7px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--muted); cursor: pointer; font: inherit; font-size: 9px; line-height: 1.5; }
.tiny:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.tiny:disabled { cursor: not-allowed; opacity: .4; }
.tiny--danger:hover:not(:disabled) { border-color: var(--danger); color: var(--danger); }

.unit-list { display: flex; flex-direction: column; gap: 4px; margin: 9px 0 0; padding: 2px 0 2px 12px; border-left: 1px dashed var(--border); list-style: none; }
.unit-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 4px 0; }
.unit-no { flex: none; width: 34px; color: var(--meta); font-family: var(--font-mono); font-size: 9px; }
.unit-title { min-width: 160px; flex: 1; padding: 5px 7px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--fg); font: inherit; font-size: 12px; }
.unit-title:hover:not(:disabled) { border-color: var(--border); }
.unit-title:focus { border-color: var(--accent); outline: 0; background: var(--bg); }
.unit-hours { display: inline-flex; align-items: center; gap: 4px; flex: none; }
.unit-hours input { width: 46px; padding: 4px 5px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--fg); font: inherit; font-size: 10px; text-align: center; }
.unit-hours em { color: var(--meta); font-size: 9px; font-style: normal; }

.kp-line { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; width: 100%; padding: 2px 0 3px 42px; }
.kp-label { color: var(--meta); font-family: var(--font-mono); font-size: 8px; }
.kp-chip { padding: 2px 6px; border: 1px solid var(--border-soft); border-radius: 999px; background: var(--bg); color: var(--muted); font-size: 9px; }

.add-unit { margin: 8px 0 0 12px; padding: 4px 9px; border: 1px dashed var(--border); border-radius: 7px; background: transparent; color: var(--muted); cursor: pointer; font: inherit; font-size: 10px; }
.add-unit:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.add-chapter { align-self: flex-start; padding: 6px 11px; border: 1px dashed var(--border); border-radius: 9px; background: transparent; color: var(--muted); cursor: pointer; font: inherit; font-size: 11px; }
.add-chapter:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.add-unit:disabled, .add-chapter:disabled { cursor: not-allowed; opacity: .45; }

@media (max-width: 640px) {
  .unit-title { min-width: 100%; }
  .kp-line { padding-left: 0; }
  .meta-field--narrow { flex: 1; }
}
</style>
