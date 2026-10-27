import { useState } from 'react';
import { Button } from '../../components/Button';
import { IconCheck } from '../../components/icons';
import { Modal } from '../../components/Modal';
import { useToast } from '../../components/Toast';

const COMMAND = 'cd TeachingFlow\npython flow.py "我想简单学一下《祥林嫂》这篇课文"';

export function ProduceModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const produceUrl = import.meta.env.VITE_PRODUCE_URL;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(COMMAND);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.warn('复制失败，请手动选中命令行文本');
    }
  };

  return (
    <Modal
      open={open}
      title="去出片"
      description="出片在 MAT 主流程里完成：一句话需求 → 学习方案 → 课件大纲 → pptx，完成后默认自动备课。"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            关闭
          </Button>
          <Button variant="primary" onClick={() => void copy()} iconLeft={copied ? <IconCheck size={16} /> : undefined}>
            {copied ? '已复制' : '复制命令'}
          </Button>
        </>
      }
    >
      <div className="stack">
        <pre
          style={{
            margin: 0,
            padding: 'var(--space-4)',
            borderRadius: 'var(--radius-md)',
            background: 'var(--surface-2)',
            border: '1px solid var(--border)',
            fontFamily: 'var(--font-mono)',
            fontSize: 'var(--text-sm)',
            whiteSpace: 'pre-wrap',
            lineHeight: 1.7,
          }}
        >
          {COMMAND}
        </pre>
        <p className="muted" style={{ fontSize: 'var(--text-sm)' }}>
          出片完成后回到这里点「重新扫描」，成品会以「课程 → 章 → 单元」出现在课程库。
          {produceUrl ? ' 也可以直接打开桌面端的出片页面。' : ''}
        </p>
        {produceUrl && (
          <div>
            <a className="btn btn--secondary btn--sm" href={produceUrl} target="_blank" rel="noreferrer">
              打开出片页面
            </a>
          </div>
        )}
      </div>
    </Modal>
  );
}
