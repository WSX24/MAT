import { onBeforeUnmount, onMounted, reactive } from 'vue'
import type { ClientFrame, ServerFrame } from '@/api/protocol'
import type { Recap, SessionPosition } from '@/api/types'
import type { DeckSlide } from '@/features/deck/types'
import { openClassroomTransport } from './transport'
import type { ClassroomTransport, TransportStatus } from './transport'

export type Rate = 0.8 | 1 | 1.2
export type Who = 'teacher' | 'me' | 'system'

export interface TranscriptEntry {
  id: number
  who: Who
  text: string
}

export interface ClassroomSnapshot {
  status: TransportStatus | 'idle'
  phase: string
  paused: boolean
  slide: DeckSlide | null
  slideIndex: number
  slideCount: number
  position: SessionPosition | null
  authorName: string | null
  comprehension: number | null
  transcript: TranscriptEntry[]
  caption: string
  rate: Rate
  recap: Recap | null
  autoplayBlocked: boolean
  retries: number
}

const INITIAL: ClassroomSnapshot = {
  status: 'idle',
  phase: 'connecting',
  paused: false,
  slide: null,
  slideIndex: 0,
  slideCount: 0,
  position: null,
  authorName: null,
  comprehension: null,
  transcript: [],
  caption: '',
  rate: 1,
  recap: null,
  autoplayBlocked: false,
  retries: 0,
}

const MAX_TRANSCRIPT = 240

export function useClassroom(options: {
  sessionId: string
  startSlide: number
  onRecap?: (recap: Recap) => void
}): {
  state: ClassroomSnapshot
  control: (cmd: 'pause' | 'resume' | 'next' | 'prev' | 'finish') => void
  setRate: (value: Rate) => void
  ask: (text: string) => void
  retryAudio: () => void
} {
  const { sessionId, startSlide } = options
  const state = reactive<ClassroomSnapshot>({ ...INITIAL, transcript: [] })

  let transport: ClassroomTransport | null = null
  let audio: HTMLAudioElement | null = null
  let entryId = 0
  let pendingAudioSeq: number | null = null
  let rate: Rate = 1

  const send = (frame: ClientFrame) => {
    transport?.send(frame)
  }

  const appendEntry = (who: Who, text: string) => {
    const trimmed = text.trim()
    if (!trimmed) return
    entryId += 1
    const entry: TranscriptEntry = { id: entryId, who, text: trimmed }
    const last = state.transcript[state.transcript.length - 1]
    if (last && last.text === entry.text && last.who === entry.who) return
    state.transcript = [...state.transcript, entry].slice(-MAX_TRANSCRIPT)
  }

  const handleFrame = (frame: ServerFrame) => {
    switch (frame.kind) {
      case 'hello': {
        const unit = typeof frame.unit === 'object' ? frame.unit : undefined
        state.authorName = unit?.title ?? state.authorName
        state.position = frame.position ?? state.position
        break
      }
      case 'page':
        state.slide = frame.slide ?? null
        state.slideIndex = frame.slide_index ?? frame.slide?.index ?? state.slideIndex
        state.slideCount = frame.slide_count ?? state.slideCount
        break
      case 'segment_start':
        state.caption = frame.text
        state.phase = 'playing'
        state.paused = false
        break
      case 'segment_end':
        state.caption = ''
        appendEntry('teacher', frame.text)
        break
      case 'audio': {
        const element = audio
        const src = frame.url || (frame.b64 ? `data:${frame.mime || 'audio/mpeg'};base64,${frame.b64}` : '')
        if (!element || !src) break
        element.src = src
        element.playbackRate = rate
        pendingAudioSeq = frame.seq
        element.play().then(
          () => {
            state.autoplayBlocked = false
          },
          () => {
            state.autoplayBlocked = true
          },
        )
        break
      }
      case 'transcript':
        appendEntry(frame.speaker === '你' || frame.speaker === 'student' ? 'me' : 'teacher', frame.text)
        break
      case 'state':
        state.phase = frame.phase ?? state.phase
        state.paused = frame.phase === 'paused' ? true : frame.phase === 'playing' ? false : state.paused
        state.comprehension = frame.comprehension ?? state.comprehension
        state.position = frame.position ?? state.position
        state.slideIndex = frame.position?.slide ?? state.slideIndex
        break
      case 'notify':
        appendEntry('system', frame.text)
        break
      case 'recap':
        if (frame.recap) {
          state.recap = frame.recap ?? null
          state.phase = 'ended'
          options.onRecap?.(frame.recap)
        }
        break
      default:
        break
    }
  }

  onMounted(() => {
    /* ---------------------------------------------------------------- 音频 */
    const element = new Audio()
    element.preload = 'auto'
    audio = element
    element.onended = () => {
      const seq = pendingAudioSeq
      pendingAudioSeq = null
      if (seq != null) transport?.send({ t: 'speech_done', seq })
    }

    /* ---------------------------------------------------------------- 连接 */
    transport = openClassroomTransport(
      sessionId,
      {
        onFrame: handleFrame,
        onStatus: (status) => {
          state.status = status
          if (status === 'open' && state.phase === 'connecting') state.phase = 'playing'
          state.retries = status === 'closed' ? state.retries + 1 : status === 'open' ? 0 : state.retries
        },
      },
      { startSlide },
    )
  })

  onBeforeUnmount(() => {
    if (audio) {
      audio.onended = null
      audio.pause()
      audio.src = ''
      audio = null
    }
    transport?.close()
    transport = null
  })

  /* ---------------------------------------------------------------- 动作 */
  const control = (cmd: 'pause' | 'resume' | 'next' | 'prev' | 'finish') => {
    if (cmd === 'pause') {
      audio?.pause()
      state.paused = true
      state.phase = 'paused'
    }
    if (cmd === 'resume') {
      audio?.play().catch(() => undefined)
      state.paused = false
      state.phase = 'playing'
    }
    if (cmd === 'next' || cmd === 'prev') {
      state.paused = false
      state.caption = ''
    }
    if (cmd === 'finish') {
      audio?.pause()
    }
    send({ t: 'cmd', cmd })
  }

  const setRate = (value: Rate) => {
    rate = value
    if (audio) audio.playbackRate = value
    state.rate = value
    send({ t: 'rate', value })
  }

  const ask = (text: string) => {
    const question = text.trim()
    if (!question) return
    appendEntry('me', question)
    send({ t: 'input', text: question })
  }

  const retryAudio = () => {
    const element = audio
    if (!element || !element.src) return
    element.play().then(
      () => {
        state.autoplayBlocked = false
      },
      () => {
        state.autoplayBlocked = true
      },
    )
  }

  return { state, control, setRate, ask, retryAudio }
}
