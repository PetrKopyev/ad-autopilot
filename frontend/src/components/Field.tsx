import type { ReactNode } from 'react';

export interface FieldProps {
  /** id поля ввода внутри; подсказка и ошибка получают id `${id}-hint` и `${id}-error`. */
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string;
  /** input / select / textarea с className="input", тем же id и aria-describedby={describedBy(id, hint, error)}. */
  children: ReactNode;
}

export function describedBy(id: string, hint?: unknown, error?: unknown) {
  return [hint ? `${id}-hint` : '', error ? `${id}-error` : ''].filter(Boolean).join(' ') || undefined;
}

export function Field({ id, label, hint, error, children }: FieldProps) {
  return <div className={`field${error ? ' field--invalid' : ''}`}>
    <label htmlFor={id} className="field__label">{label}</label>
    {children}
    {hint && <p id={`${id}-hint`} className="field__hint">{hint}</p>}
    {error && <p id={`${id}-error`} className="field__error">{error}</p>}
  </div>;
}
