/** 列表页/详情页的视图模型：把 HTTP 契约翻译成旧设计的展示口径。 */
import type { CourseDetail, CourseSummary, UnitProgress } from './types'
import { demoCourseSummary } from '@/data/demoCourses'

export interface CourseCardView {
  id: string
  title: string
  subject: string
  source: string
  unitCount: number
  readyCount: number
  percent: number
  updatedAt: string
  /** 卡片上那句说明（旧设计的 goal 位置）。真实课用可核对的概述，不编学习目标。 */
  summary: string
  offline: boolean
}

export function toCourseCard(course: CourseSummary): CourseCardView {
  const ready = course.unit_ready_count ?? 0
  const total = course.unit_count ?? 0
  const percent = Math.round(course.progress?.percent ?? 0)
  const subject = (course.subject || '').trim()
  const prefix = subject ? `${subject} · ` : ''
  const prepared = total > 0 ? `${ready}/${total} 个单元已备课` : '还没有解析出单元'
  return {
    id: course.id,
    title: course.title || '未命名课程',
    subject,
    source: course.source || 'run',
    unitCount: total,
    readyCount: ready,
    percent,
    updatedAt: course.updated_at || '',
    summary: course.source === 'import'
      ? `${prefix}${prepared}，可直接进入课堂学习。`
      : `${prefix}由出片流水线生成，${prepared}。`,
    offline: false,
  }
}

export function demoCourseCard(): CourseCardView {
  const card = toCourseCard(demoCourseSummary)
  return {
    ...card,
    summary: '离线示例：后端未连接时用来预览界面，不会保存到课程库。',
    offline: true,
  }
}

export interface FlatUnit {
  chapterTitle: string
  chapterIndex: number
  unitIndex: number
  unit: CourseDetail['chapters'][number]['units'][number]
}

/** 章 → 单元拍平：路由用「第几章第几单元」，课堂需要 unit.id。 */
export function flattenUnits(course: CourseDetail): FlatUnit[] {
  const flat: FlatUnit[] = []
  course.chapters.forEach((chapter, chapterIndex) => {
    chapter.units.forEach((unit, unitIndex) => {
      flat.push({ chapterTitle: chapter.title, chapterIndex, unitIndex, unit })
    })
  })
  return flat
}

export function findUnit(course: CourseDetail, chapterNumber: number, lessonNumber: number) {
  const chapter = course.chapters[chapterNumber - 1]
  if (!chapter) return null
  const unit = chapter.units[lessonNumber - 1]
  if (!unit) return null
  return { chapter, unit }
}

export function unitStatusText(progress: UnitProgress | undefined): { label: string; tone: string } {
  const status = progress?.status || 'not_started'
  if (status === 'completed') return { label: '已完成', tone: 'done' }
  if (status === 'in_progress') return { label: '进行中', tone: 'progress' }
  if (status === 'paused') return { label: '已暂停', tone: 'paused' }
  return { label: '未完成', tone: 'todo' }
}

export function totalSlides(course: CourseDetail): number {
  return course.chapters.reduce(
    (total, chapter) => total + chapter.units.reduce((sum, unit) => sum + (unit.slides || 0), 0),
    0,
  )
}

export function totalSeconds(course: CourseDetail): number {
  return course.chapters.reduce(
    (total, chapter) => total + chapter.units.reduce((sum, unit) => sum + (unit.est_sec || 0), 0),
    0,
  )
}
