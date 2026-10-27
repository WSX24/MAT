/** mock 课堂：用同一套 WS 帧协议把课堂跑起来（不连后端）。 */
import { mockSessionTouch } from '../../api/mock';
import type { ClientFrame } from '../../api/protocol';
import { MOCK_SLIDES, MOCK_SPEECH } from '../../api/mockDeck';
import type { ClassroomTransport, TransportHandlers } from './transport';

const ANSWERS: string[] = [
  '好问题。回到课件：判断能不能看成质点，关键看物体的大小和形状对所研究的问题有没有影响。',
  '我们看刚才那一页的表格：位移只看起点和终点，路程才看走过的路径。',
  '这个先记住结论——加速度描述的是速度变化的快慢，方向和速度方向不一定相同。',
  '换个例子：你从家走到学校再绕去商店，路程变长了，位移却只跟最后的位置有关。',
  '这正是下一页要讲的。先把它标在手边，等讲到例题时我们再一起过一遍。',
];

function answerFor(question: string): string {
  let hash = 0;
  for (let index = 0; index < question.length; index += 1) hash = (hash * 31 + question.charCodeAt(index)) % 997;
  return ANSWERS[hash % ANSWERS.length];
}

export function createMockTransport(
  sessionId: string,
  handlers: TransportHandlers,
  options: { startSlide: number },
): ClassroomTransport {
  let slideIndex = Math.max(0, Math.min(MOCK_SLIDES.length - 1, options.startSlide || 0));
  let lineIndex = 0;
  let seq = 0;
  let rate = 1;
  let paused = false;
  let disposed = false;
  let timer: number | undefined;
  const slidesSeen = new Set<number>([slideIndex]);
  const startedAt = Date.now();

  const later = (fn: () => void, ms: number) => {
    if (disposed) return;
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(fn, Math.max(120, ms / rate));
  };

  const emitPage = () => {
    slidesSeen.add(slideIndex);
    mockSessionTouch(sessionId, { slide: slideIndex });
    handlers.onFrame({
      kind: 'page',
      slide_index: slideIndex,
      slide_count: MOCK_SLIDES.length,
      slide: MOCK_SLIDES[slideIndex],
    });
  };

  const emitState = (phase: string) => {
    handlers.onFrame({ kind: 'state', phase, comprehension: null, position: { slide: slideIndex, unit_index: 0 } });
  };

  const step = () => {
    if (disposed || paused) return;
    const lines = MOCK_SPEECH[slideIndex] ?? [];
    if (lineIndex >= lines.length) {
      if (slideIndex < MOCK_SLIDES.length - 1) {
        slideIndex += 1;
        lineIndex = 0;
        emitPage();
        later(step, 700);
      } else {
        emitState('ended');
      }
      return;
    }

    const text = lines[lineIndex];
    seq += 1;
    const currentSeq = seq;
    handlers.onFrame({ kind: 'segment_start', seq: currentSeq, text });

    later(() => {
      if (disposed) return;
      handlers.onFrame({ kind: 'segment_end', seq: currentSeq, text });
      handlers.onFrame({ kind: 'transcript', speaker: '老师', text });
      lineIndex += 1;
      later(step, 420);
    }, 900 + text.length * 55);
  };

  const send = (frame: ClientFrame) => {
    if (disposed) return;
    if (frame.t === 'cmd') {
      switch (frame.cmd) {
        case 'pause':
          paused = true;
          if (timer) window.clearTimeout(timer);
          emitState('paused');
          return;
        case 'resume':
          paused = false;
          emitState('playing');
          step();
          return;
        case 'next':
          if (slideIndex < MOCK_SLIDES.length - 1) {
            slideIndex += 1;
            lineIndex = 0;
            paused = false;
            if (timer) window.clearTimeout(timer);
            emitPage();
            later(step, 400);
          }
          return;
        case 'prev':
          if (slideIndex > 0) {
            slideIndex -= 1;
            lineIndex = 0;
            paused = false;
            if (timer) window.clearTimeout(timer);
            emitPage();
            later(step, 400);
          }
          return;
        case 'finish':
          paused = true;
          if (timer) window.clearTimeout(timer);
          emitState('ended');
          return;
        default:
          return;
      }
    }

    if (frame.t === 'rate') {
      rate = frame.value;
      return;
    }

    if (frame.t === 'input') {
      mockSessionTouch(sessionId, { qa: true });
      handlers.onFrame({ kind: 'transcript', speaker: '你', text: frame.text });
      const wasPaused = paused;
      paused = true;
      later(() => {
        if (disposed) return;
        handlers.onFrame({ kind: 'transcript', speaker: '老师', text: answerFor(frame.text) });
        paused = wasPaused;
      }, 900);
      return;
    }
  };

  handlers.onStatus('open');
  later(() => {
    handlers.onFrame({ kind: 'hello', unit: { title: '质点与参考系' }, position: { slide: slideIndex, unit_index: 0 } });
    emitPage();
    later(step, 500);
  }, 300);

  return {
    send,
    close() {
      disposed = true;
      if (timer) window.clearTimeout(timer);
      void startedAt;
    },
  };
}
