import { useEffect, useId, useRef, type ReactNode } from 'react';
import { Button } from './Button';
import { IconClose } from './icons';

export interface ModalProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  onClose: () => void;
  children?: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  labelledBy?: string;
}

export function Modal({ open, title, description, onClose, children, footer, wide = false }: ModalProps) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreFocus = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreFocus.current = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (restoreFocus.current instanceof HTMLElement) restoreFocus.current.focus();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div
        className={'modal' + (wide ? ' modal--wide' : '')}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={dialogRef}
      >
        <div className="modal__head">
          <div style={{ flex: 1 }}>
            <h2 className="modal__title" id={titleId}>
              {title}
            </h2>
            {description && <p className="modal__desc">{description}</p>}
          </div>
          <Button variant="ghost" size="sm" iconOnly aria-label="关闭对话框" onClick={onClose} iconLeft={<IconClose size={18} />} />
        </div>
        {children && <div className="modal__body">{children}</div>}
        {footer && <div className="modal__foot">{footer}</div>}
      </div>
    </div>
  );
}
