import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** Что произойдёт после подтверждения: абзац или список шагов. */
  children: ReactNode;
  confirm: string;
  /** Подпись кнопки, пока действие выполняется. */
  confirming: string;
  cancel: string;
  busy: boolean;
  error?: string;
  /** danger — остановка и отключение, accent — главное действие («Запустить рекламу»), primary — безопасное
   * («Продолжить рекламу»). */
  tone?: 'danger' | 'accent' | 'primary';
  onConfirm: () => void;
  onClose: () => void;
}

/** Модальное подтверждение: фокус сначала на «Отмене», Esc и «Отмена» закрывают, пока действие не началось. */
export function ConfirmDialog({ open, title, children, confirm, confirming, cancel, busy, error, tone = 'danger', onConfirm, onClose }: ConfirmDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    if (open && !node.open) node.showModal();
    if (!open && node.open) node.close();
  }, [open]);
  return <dialog ref={dialog} className="confirm-dialog" aria-labelledby={`${id}-title`} aria-describedby={`${id}-text`}
    onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
    <div className="page-stack"><h2 id={`${id}-title`}>{title}</h2><div id={`${id}-text`} className="confirm-dialog__text">{children}</div>
      {error && <ErrorBanner message={error} />}
      <div className="actions"><Button autoFocus variant="secondary" disabled={busy} onClick={onClose}>{cancel}</Button>
        <Button variant={tone} disabled={busy} onClick={onConfirm}>{busy ? confirming : confirm}</Button></div>
    </div>
  </dialog>;
}
