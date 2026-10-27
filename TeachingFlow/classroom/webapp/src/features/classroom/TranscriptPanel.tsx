import { useEffect, useRef, useState } from 'react';
import { Button } from '../../components/Button';
import { IconSend } from '../../components/icons';
import type { TranscriptEntry } from './useClassroom';

const QUICK_ASKS = ['这一页的重点是什么？', '能再举个例子吗？', '刚才那句没听懂', '这和上一节有什么关系？'];

export interface TranscriptPanelProps {
  entries: TranscriptEntry[];
  caption: string;
  connected: boolean;
  onAsk: (text: string) => void;
}

export function TranscriptPanel({ entries, caption, connected, onAsk }: TranscriptPanelProps) {
  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [entries.length, caption]);

  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    onAsk(text);
    setDraft('');
  };

  return (
    <>
      <div className="sidebar-head">
        <span>字幕与提问</span>
        <span className="sidebar-head__count">{entries.length} 条</span>
      </div>
      <div className="transcript" ref={listRef} role="log" aria-live="polite" aria-label="课堂字幕">
        {entries.length === 0 && !caption && (
          <div className="line">
            <span className="line__text">课堂开始后，老师的讲解会逐句出现在这里。</span>
          </div>
        )}
        {entries.map((entry) => (
          <div key={entry.id} className={'line' + (entry.who === 'me' ? ' line--me' : '')}>
            <span className="line__who">{entry.who === 'me' ? '你' : entry.who === 'system' ? '提示' : '老师'}</span>
            <span className="line__text">{entry.text}</span>
          </div>
        ))}
        {caption && (
          <div className="line line--live" aria-live="off">
            <span className="line__who">正在讲…</span>
            <span className="line__text">{caption}</span>
          </div>
        )}
      </div>
      <div className="ask-box">
        <div className="quick-asks" aria-label="快捷提问">
          {QUICK_ASKS.map((text) => (
            <button key={text} type="button" className="quick-ask" onClick={() => onAsk(text)} disabled={!connected}>
              {text}
            </button>
          ))}
        </div>
        <div className="ask-box__row">
          <label className="visually-hidden" htmlFor="ask-input">
            输入你的问题
          </label>
          <textarea
            id="ask-input"
            className="field__control"
            placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
            rows={2}
          />
          <Button
            variant="primary"
            iconOnly
            aria-label="发送问题"
            title="发送问题"
            disabled={!draft.trim()}
            onClick={submit}
            iconLeft={<IconSend size={18} />}
          />
        </div>
      </div>
    </>
  );
}
