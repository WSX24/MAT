import { useCallback, useEffect, useRef, useState } from 'react';
import type { ClientFrame, ServerFrame } from '../../api/protocol';
import type { Recap, SessionPosition } from '../../api/types';
import type { DeckSlide } from '../deck/types';
import { openClassroomTransport, type ClassroomTransport, type TransportStatus } from './transport';

export type Rate = 0.8 | 1 | 1.2;
export type Who = 'teacher' | 'me' | 'system';

export interface TranscriptEntry {
  id: number;
  who: Who;
  text: string;
}

export interface ClassroomSnapshot {
  status: TransportStatus | 'idle';
  phase: string;
  paused: boolean;
  slide: DeckSlide | null;
  slideIndex: number;
  slideCount: number;
  position: SessionPosition | null;
  authorName: string | null;
  comprehension: number | null;
  transcript: TranscriptEntry[];
  caption: string;
  rate: Rate;
  recap: Recap | null;
  autoplayBlocked: boolean;
  retries: number;
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
};

const MAX_TRANSCRIPT = 240;

export function useClassroom(options: { sessionId: string; startSlide: number; onRecap?: (recap: Recap) => void }) {
  const { sessionId, startSlide } = options;
  const [state, setState] = useState<ClassroomSnapshot>(INITIAL);
  const transportRef = useRef<ClassroomTransport | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const entryId = useRef(0);
  const pendingAudioSeq = useRef<number | null>(null);
  const rateRef = useRef<Rate>(1);
  const onRecapRef = useRef(options.onRecap);
  onRecapRef.current = options.onRecap;

  const appendEntry = useCallback((who: Who, text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    entryId.current += 1;
    const entry: TranscriptEntry = { id: entryId.current, who, text: trimmed };
    setState((current) => {
      const last = current.transcript[current.transcript.length - 1];
      if (last && last.text === entry.text && last.who === entry.who) return current;
      const transcript = [...current.transcript, entry];
      return { ...current, transcript: transcript.slice(-MAX_TRANSCRIPT) };
    });
  }, []);

  /* ------------------------------------------------------------------ 音频 */
  useEffect(() => {
    const audio = new Audio();
    audio.preload = 'auto';
    audioRef.current = audio;
    audio.onended = () => {
      const seq = pendingAudioSeq.current;
      pendingAudioSeq.current = null;
      if (seq != null) transportRef.current?.send({ t: 'speech_done', seq });
    };
    return () => {
      audio.onended = null;
      audio.pause();
      audio.src = '';
      audioRef.current = null;
    };
  }, []);

  /* ------------------------------------------------------------------ 连接 */
  useEffect(() => {
    const handleFrame = (frame: ServerFrame) => {
      switch (frame.kind) {
        case 'hello': {
          const unit = typeof frame.unit === 'object' ? frame.unit : undefined;
          setState((current) => ({
            ...current,
            authorName: unit?.title ?? current.authorName,
            position: frame.position ?? current.position,
          }));
          break;
        }
        case 'page':
          setState((current) => ({
            ...current,
            slide: frame.slide ?? null,
            slideIndex: frame.slide_index ?? frame.slide?.index ?? current.slideIndex,
            slideCount: frame.slide_count ?? current.slideCount,
          }));
          break;
        case 'segment_start':
          setState((current) => ({ ...current, caption: frame.text, phase: 'playing', paused: false }));
          break;
        case 'segment_end':
          setState((current) => ({ ...current, caption: '' }));
          appendEntry('teacher', frame.text);
          break;
        case 'audio': {
          const audio = audioRef.current;
          const src = frame.url || (frame.b64 ? `data:${frame.mime || 'audio/mpeg'};base64,${frame.b64}` : '');
          if (!audio || !src) break;
          audio.src = src;
          audio.playbackRate = rateRef.current;
          pendingAudioSeq.current = frame.seq;
          audio.play().then(
            () => setState((current) => ({ ...current, autoplayBlocked: false })),
            () => setState((current) => ({ ...current, autoplayBlocked: true })),
          );
          break;
        }
        case 'transcript':
          appendEntry(frame.speaker === '你' || frame.speaker === 'student' ? 'me' : 'teacher', frame.text);
          break;
        case 'state':
          setState((current) => ({
            ...current,
            phase: frame.phase ?? current.phase,
            paused: frame.phase === 'paused' ? true : frame.phase === 'playing' ? false : current.paused,
            comprehension: frame.comprehension ?? current.comprehension,
            position: frame.position ?? current.position,
            slideIndex: frame.position?.slide ?? current.slideIndex,
          }));
          break;
        case 'notify':
          appendEntry('system', frame.text);
          break;
        case 'recap':
          if (frame.recap) {
            setState((current) => ({ ...current, recap: frame.recap ?? null, phase: 'ended' }));
            onRecapRef.current?.(frame.recap);
          }
          break;
        default:
          break;
      }
    };

    const transport = openClassroomTransport(
      sessionId,
      {
        onFrame: handleFrame,
        onStatus: (status) => {
          setState((current) => ({
            ...current,
            status,
            phase: status === 'open' ? (current.phase === 'connecting' ? 'playing' : current.phase) : current.phase,
            retries: status === 'closed' ? current.retries + 1 : status === 'open' ? 0 : current.retries,
          }));
        },
      },
      { startSlide },
    );
    transportRef.current = transport;
    return () => {
      transport.close();
      transportRef.current = null;
    };
  }, [sessionId, startSlide, appendEntry]);

  /* ------------------------------------------------------------------ 动作 */
  const send = useCallback((frame: ClientFrame) => {
    transportRef.current?.send(frame);
  }, []);

  const control = useCallback(
    (command: 'pause' | 'resume' | 'next' | 'prev' | 'finish') => {
      if (command === 'pause') {
        audioRef.current?.pause();
        setState((current) => ({ ...current, paused: true, phase: 'paused' }));
      }
      if (command === 'resume') {
        audioRef.current?.play().catch(() => undefined);
        setState((current) => ({ ...current, paused: false, phase: 'playing' }));
      }
      if (command === 'next' || command === 'prev') {
        setState((current) => ({ ...current, paused: false, caption: '' }));
      }
      if (command === 'finish') {
        audioRef.current?.pause();
      }
      send({ t: 'cmd', cmd: command });
    },
    [send],
  );

  const setRate = useCallback(
    (value: Rate) => {
      rateRef.current = value;
      if (audioRef.current) audioRef.current.playbackRate = value;
      setState((current) => ({ ...current, rate: value }));
      send({ t: 'rate', value });
    },
    [send],
  );

  const ask = useCallback(
    (text: string) => {
      const question = text.trim();
      if (!question) return;
      appendEntry('me', question);
      send({ t: 'input', text: question });
    },
    [appendEntry, send],
  );

  const retryAudio = useCallback(() => {
    const audio = audioRef.current;
    if (!audio || !audio.src) return;
    audio.play().then(
      () => setState((current) => ({ ...current, autoplayBlocked: false })),
      () => setState((current) => ({ ...current, autoplayBlocked: true })),
    );
  }, []);

  return { ...state, control, setRate, ask, retryAudio };
}

export type ClassroomController = ReturnType<typeof useClassroom>;
