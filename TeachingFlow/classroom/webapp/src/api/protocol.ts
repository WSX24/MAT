/** §4 WS 协议：/ws/classroom/{session_id} */
import type { DeckSlide } from '../features/deck/types';
import type { Recap, SessionPosition } from './types';

export type ClientFrame =
  | { t: 'input'; text: string; via?: string }
  | { t: 'cmd'; cmd: 'pause' | 'resume' | 'next' | 'prev' | 'finish' }
  | { t: 'rate'; value: 0.8 | 1 | 1.2 }
  | { t: 'speech_done'; seq: number };

export interface HelloUnit {
  id?: string;
  title?: string;
  slides?: number;
}

export interface HelloProfile {
  id?: string;
  name?: string;
}

export interface ClassroomState {
  phase: string;
  comprehension: number | null;
  position: SessionPosition;
}

export type ServerFrame =
  | { kind: 'hello'; unit?: HelloUnit | string; profile?: HelloProfile | string; position?: SessionPosition }
  | { kind: 'page'; slide_index?: number; slide_count?: number; slide: DeckSlide }
  | { kind: 'segment_start'; seq: number; text: string }
  | { kind: 'audio'; seq: number; url?: string; b64?: string; mime?: string }
  | { kind: 'segment_end'; seq: number; text: string }
  | { kind: 'transcript'; speaker?: string; text: string }
  | { kind: 'state'; phase?: string; comprehension?: number | null; position?: SessionPosition }
  | { kind: 'notify'; level?: string; text: string }
  | { kind: 'recap'; recap?: Recap };

const KINDS = ['hello', 'page', 'segment_start', 'audio', 'segment_end', 'transcript', 'state', 'notify', 'recap'] as const;

/** 宽容解析：未知 kind 一律忽略（后端加帧不会让前端崩）。 */
export function parseServerFrame(raw: unknown): ServerFrame | null {
  if (!raw || typeof raw !== 'object') return null;
  const obj = raw as Record<string, unknown>;
  const kind = obj.kind;
  if (typeof kind !== 'string' || !(KINDS as readonly string[]).includes(kind)) return null;
  return obj as unknown as ServerFrame;
}
