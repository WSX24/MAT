/**
 * 课堂 WS 传输层。
 * - 真实后端：/ws/classroom/{session_id}，断线按指数退避重连（0.5s→8s），
 *   重连后由后端补一个 state 帧恢复现场（前端不猜进度）。
 * - VITE_MOCK=1：内置模拟器按同一套帧协议播报，后端没起也能把 UI 跑通。
 */
import { mockMode } from '../../api/client';
import { parseServerFrame, type ClientFrame, type ServerFrame } from '../../api/protocol';
import { createMockTransport } from './mockTransport';

export type TransportStatus = 'connecting' | 'open' | 'closed';

export interface TransportHandlers {
  onFrame: (frame: ServerFrame) => void;
  onStatus: (status: TransportStatus, detail?: string) => void;
}

export interface ClassroomTransport {
  send: (frame: ClientFrame) => void;
  close: () => void;
}

const MAX_DELAY = 8000;

export function openClassroomTransport(
  sessionId: string,
  handlers: TransportHandlers,
  options: { startSlide: number },
): ClassroomTransport {
  if (mockMode) {
    handlers.onStatus('connecting');
    return createMockTransport(sessionId, handlers, options);
  }

  let socket: WebSocket | null = null;
  let closedByUser = false;
  let attempt = 0;
  let reconnectTimer: number | undefined;
  const queue: ClientFrame[] = [];

  const scheduleReconnect = () => {
    const delay = Math.min(MAX_DELAY, 500 * 2 ** attempt);
    attempt += 1;
    reconnectTimer = window.setTimeout(connect, delay + Math.random() * 250);
  };

  function connect() {
    handlers.onStatus('connecting');
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${scheme}://${window.location.host}/ws/classroom/${encodeURIComponent(sessionId)}`;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      handlers.onStatus('closed', error instanceof Error ? error.message : '建连失败');
      scheduleReconnect();
      return;
    }

    socket.onopen = () => {
      attempt = 0;
      handlers.onStatus('open');
      const pending = queue.splice(0, queue.length);
      pending.forEach((frame) => socket?.send(JSON.stringify(frame)));
    };

    socket.onmessage = (event) => {
      let payload: unknown;
      try {
        payload = JSON.parse(String(event.data));
      } catch {
        return;
      }
      const frame = parseServerFrame(payload);
      if (frame) handlers.onFrame(frame);
    };

    socket.onclose = () => {
      handlers.onStatus('closed');
      if (!closedByUser) scheduleReconnect();
    };

    socket.onerror = () => {
      /* onclose 随后触发，重连逻辑只放在 onclose */
    };
  }

  connect();

  return {
    send(frame: ClientFrame) {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(frame));
      } else {
        queue.push(frame);
      }
    },
    close() {
      closedByUser = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
      socket = null;
    },
  };
}
