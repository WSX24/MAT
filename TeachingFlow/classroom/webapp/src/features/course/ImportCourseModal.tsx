import { useRef, useState, type DragEvent } from 'react';
import { api } from '../../api/client';
import type { ImportResult } from '../../api/types';
import { Button } from '../../components/Button';
import { IconUpload } from '../../components/icons';
import { Modal } from '../../components/Modal';
import { useToast } from '../../components/Toast';
import { errorText } from '../../lib/format';

export interface ImportCourseModalProps {
  open: boolean;
  onClose: () => void;
  onImported: (result: ImportResult) => void;
}

export function ImportCourseModal({ open, onClose, onImported }: ImportCourseModalProps) {
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const toast = useToast();

  const pick = (candidate: File | null | undefined) => {
    if (!candidate) return;
    if (!/\.pptx$/i.test(candidate.name)) {
      setError('只支持 .pptx 文件（老格式 .ppt 请先在 PowerPoint 里另存为 pptx）');
      setFile(null);
      return;
    }
    setError(null);
    setFile(candidate);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    pick(event.dataTransfer.files?.[0]);
  };

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.importCourse(file);
      toast.ok(`已导入 ${result.slides} 页`);
      setFile(null);
      onImported(result);
      onClose();
    } catch (caught) {
      setError(errorText(caught));
    } finally {
      setBusy(false);
    }
  };

  const close = () => {
    if (busy) return;
    setFile(null);
    setError(null);
    onClose();
  };

  return (
    <Modal
      open={open}
      title="导入课件"
      description="把一份 .pptx 直接变成一门课：解析 → 备课 → 可以马上进课堂。"
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>
            取消
          </Button>
          <Button variant="primary" loading={busy} disabled={!file} onClick={() => void submit()}>
            {busy ? '正在导入…' : '开始导入'}
          </Button>
        </>
      }
    >
      <div className="stack">
        <div
          className={'dropzone' + (dragging ? ' dropzone--active' : '')}
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              inputRef.current?.click();
            }
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          aria-label="选择或拖入 .pptx 文件"
        >
          <IconUpload size={34} />
          <div style={{ fontWeight: 700 }}>把 .pptx 拖到这里，或点击选择</div>
          <div className="dropzone__hint">支持 2007 之后版本的 PowerPoint（.pptx）</div>
          <input
            ref={inputRef}
            type="file"
            accept=".pptx"
            className="visually-hidden"
            onChange={(event) => pick(event.target.files?.[0])}
          />
        </div>

        {file && (
          <div className="file-chip">
            <IconUpload size={18} />
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.name}</span>
            <span className="muted tabular">{(file.size / 1024 / 1024).toFixed(1)} MB</span>
            <Button variant="ghost" size="sm" onClick={() => setFile(null)} disabled={busy}>
              换一个
            </Button>
          </div>
        )}

        {error && (
          <div className="banner banner--warn" role="alert">
            {error}
          </div>
        )}
      </div>
    </Modal>
  );
}
