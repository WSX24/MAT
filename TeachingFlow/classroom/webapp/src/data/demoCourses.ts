/**
 * 离线示例课程。
 *
 * 唯一用途：后端没起来（`ApiError(status=0)`）时，让「我的课堂」仍然有一套完整的
 * 演示内容可看。页面会明确标注「离线示例」，**不冒充真实课程**，也不写进数据库。
 * 内容沿用旧仓库的「高中物理必修一」示例，结构与真实契约对齐（章 → 单元 → 进度）。
 */
import type { CourseDetail, CourseSummary } from '@/api/types'

export interface DemoLesson {
  title: string
  slides: number
  estSec: number
  done: boolean
}

export interface DemoChapter {
  title: string
  lessons: DemoLesson[]
}

export interface DemoCourse {
  id: string
  title: string
  subject: string
  goal: string
  overview: string
  chapters: DemoChapter[]
  createdAt: string
  updatedAt: string
  model: string
}

export const DEMO_COURSE_ID = 'demo-physics-1'

export const physicsDemoCourse: DemoCourse = {
  id: DEMO_COURSE_ID,
  title: '高中物理必修一',
  subject: '高中物理',
  goal: '掌握描述直线运动的基本概念和规律，学会分析常见受力情境，并能运用牛顿运动定律解决基础问题。',
  overview:
    '课程围绕高中物理必修第一册的力学基础展开，从如何描述物体运动开始，逐步学习匀变速直线运动、相互作用中的力，以及运动与力的关系。通过典型情境建立清晰的分析方法，为后续物理学习打下基础。',
  chapters: [
    {
      title: '运动的描述',
      lessons: [
        { title: '质点 参考系', slides: 12, estSec: 420, done: true },
        { title: '时间 位移', slides: 14, estSec: 480, done: true },
        { title: '位置变化快慢的描述——速度', slides: 16, estSec: 540, done: true },
        { title: '速度变化快慢的描述——加速度', slides: 15, estSec: 510, done: true },
      ],
    },
    {
      title: '匀变速直线运动的研究',
      lessons: [
        { title: '实验：探究小车速度随时间变化的规律', slides: 18, estSec: 660, done: false },
        { title: '匀变速直线运动的速度与时间的关系', slides: 15, estSec: 540, done: false },
        { title: '匀变速直线运动的位移与时间的关系', slides: 17, estSec: 600, done: false },
        { title: '自由落体运动', slides: 13, estSec: 480, done: false },
      ],
    },
    {
      title: '相互作用——力',
      lessons: [
        { title: '重力与弹力', slides: 16, estSec: 540, done: false },
        { title: '摩擦力', slides: 15, estSec: 510, done: false },
        { title: '牛顿第三定律', slides: 12, estSec: 420, done: false },
        { title: '力的合成和分解', slides: 18, estSec: 660, done: false },
        { title: '共点力的平衡', slides: 16, estSec: 570, done: false },
      ],
    },
    {
      title: '运动和力的关系',
      lessons: [
        { title: '牛顿第一定律', slides: 13, estSec: 450, done: false },
        { title: '实验：探究加速度与力、质量的关系', slides: 19, estSec: 720, done: false },
        { title: '牛顿第二定律', slides: 17, estSec: 630, done: false },
        { title: '牛顿运动定律的应用', slides: 18, estSec: 660, done: false },
      ],
    },
  ],
  createdAt: '2026-10-01',
  updatedAt: '2026-10-01',
  model: '示例课程 · 离线',
}

export const demoCourseSummary: CourseSummary = {
  id: physicsDemoCourse.id,
  title: physicsDemoCourse.title,
  subject: physicsDemoCourse.subject,
  source: 'demo',
  unit_count: physicsDemoCourse.chapters.reduce((total, chapter) => total + chapter.lessons.length, 0),
  unit_ready_count: physicsDemoCourse.chapters.reduce((total, chapter) => total + chapter.lessons.length, 0),
  progress: {
    started: physicsDemoCourse.chapters.reduce(
      (total, chapter) => total + chapter.lessons.filter((lesson) => lesson.done).length,
      0,
    ),
    completed: physicsDemoCourse.chapters.reduce(
      (total, chapter) => total + chapter.lessons.filter((lesson) => lesson.done).length,
      0,
    ),
    percent: 17.6,
  },
  updated_at: physicsDemoCourse.updatedAt,
}

export const demoCourseDetail: CourseDetail = {
  id: physicsDemoCourse.id,
  title: physicsDemoCourse.title,
  subject: physicsDemoCourse.subject,
  source: 'demo',
  chapters: physicsDemoCourse.chapters.map((chapter) => ({
    title: chapter.title,
    units: chapter.lessons.map((lesson, index) => ({
      id: `${physicsDemoCourse.id}-u${index}`,
      title: lesson.title,
      slides: lesson.slides,
      est_sec: lesson.estSec,
      ready: true,
      plan_source: 'demo',
      progress: {
        percent: lesson.done ? 100 : 0,
        last_slide: lesson.done ? lesson.slides - 1 : 0,
        status: lesson.done ? 'completed' : 'not_started',
      },
    })),
  })),
}
