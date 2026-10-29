import type { ReactNode } from 'react';

export interface SegmentedOption<T extends string> {
  value: T;
  label: ReactNode;
  /** Короче для телефона: показывается вместо label уже 420 px. */
  shortLabel?: ReactNode;
}

export interface SegmentedControlProps<T extends string> {
  /** Название группы для чтения с экрана: «Период», «Режим автопилота». */
  label: string;
  options: readonly SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
  /** На тёмной строке состояния. */
  dark?: boolean;
  disabled?: boolean;
}

/** Переключатель из кнопок с aria-pressed: каждая доступна Tab, выбранная объявляется «нажата». */
export function SegmentedControl<T extends string>({ label, options, value, onChange, dark, disabled }: SegmentedControlProps<T>) {
  return <div className={`segmented${dark ? ' segmented--dark' : ''}`} role="group" aria-label={label}>
    {options.map((option) => <button key={option.value} type="button" className="segmented__option" aria-pressed={option.value === value}
      disabled={disabled} onClick={() => onChange(option.value)}>
      {option.shortLabel ? <><span className="segmented__full">{option.label}</span><span className="segmented__short">{option.shortLabel}</span></> : option.label}
    </button>)}
  </div>;
}
