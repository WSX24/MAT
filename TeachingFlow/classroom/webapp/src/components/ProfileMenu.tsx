import { useEffect, useRef, useState } from 'react';
import { Button } from './Button';
import { Field } from './Field';
import { IconCheck, IconChevronDown } from './icons';
import { Modal } from './Modal';
import { useToast } from './Toast';
import { useApp } from '../state/AppState';
import { errorText } from '../lib/format';

export function ProfileMenu() {
  const { profiles, activeProfile, switchProfile, createProfile } = useApp();
  const [open, setOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const toast = useToast();

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const initial = (activeProfile?.name || '学').slice(0, 1);

  const handleSwitch = async (id: string) => {
    setOpen(false);
    try {
      await switchProfile(id);
      toast.ok('已切换档案，进度按档案各自记录');
    } catch (error) {
      toast.error(errorText(error));
    }
  };

  const handleCreate = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      await createProfile(trimmed);
      toast.ok(`已创建档案「${trimmed}」`);
      setCreateOpen(false);
      setName('');
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="profile" ref={containerRef}>
      <button
        type="button"
        className="profile__button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`当前档案：${activeProfile?.name || '加载中'}，切换档案`}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="profile__avatar" aria-hidden="true">
          {initial}
        </span>
        <span>{activeProfile?.name || '档案'}</span>
        <IconChevronDown size={16} />
      </button>
      {open && (
        <div className="menu" role="menu" aria-label="切换档案">
          <div className="menu__label">本地档案</div>
          {profiles.map((profile) => (
            <button
              key={profile.id}
              type="button"
              role="menuitemradio"
              aria-checked={profile.id === activeProfile?.id}
              aria-current={profile.id === activeProfile?.id}
              className="menu__item"
              onClick={() => void handleSwitch(profile.id)}
            >
              <span style={{ flex: 1 }}>{profile.name}</span>
              {profile.id === activeProfile?.id && <IconCheck size={16} />}
            </button>
          ))}
          <div className="menu__divider" />
          <button
            type="button"
            role="menuitem"
            className="menu__item"
            onClick={() => {
              setOpen(false);
              setCreateOpen(true);
            }}
          >
            新建档案…
          </button>
        </div>
      )}

      <Modal
        open={createOpen}
        title="新建档案"
        description="档案之间进度互不干扰，适合家里多个孩子共用一台机器。"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>
              取消
            </Button>
            <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={() => void handleCreate()}>
              创建并切换
            </Button>
          </>
        }
      >
        <Field label="档案名" htmlFor="profile-name" hint="例如：小宇 / 姐姐 / 试听">
          <input
            id="profile-name"
            className="field__control"
            value={name}
            maxLength={24}
            autoFocus
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void handleCreate();
            }}
          />
        </Field>
      </Modal>
    </div>
  );
}
