import { useEffect, useId, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { copy } from '../copy';
import { describedBy } from './Field';

export const cityKey = (value: string) => value.toLocaleLowerCase('ru').replace(/ё/g, 'е').split(/\s+/).filter(Boolean).join(' ');

/** Сначала города, которые начинаются с введённого, потом те, где оно встречается внутри названия. */
export function cityMatches(cities: string[], query: string): string[] {
  const key = cityKey(query);
  const starts = cities.filter((city) => cityKey(city).startsWith(key));
  return [...starts, ...cities.filter((city) => !starts.includes(city) && cityKey(city).includes(key))];
}

export interface CityComboboxProps {
  label: string; cities: string[]; value: string; onChange: (value: string) => void; required?: boolean;
  /** id поля; подсказка и ошибка получают `${id}-hint` и `${id}-error`, как у Field. */
  id?: string; hint?: ReactNode; error?: string;
}

/** Поле с подсказками по списку городов; что выбрано, проверяет форма при отправке. */
export function CityCombobox({ label, cities, value, onChange, required, id, hint, error }: CityComboboxProps) {
  const generated = useId();
  const inputId = id ?? `${generated}-input`;
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const options = open ? cityMatches(cities, value) : [];
  const listId = `${inputId}-list`;
  const optionId = (index: number) => `${inputId}-option-${index}`;
  const shown = open && cities.length > 0;
  useEffect(() => { if (active >= 0) document.getElementById(optionId(active))?.scrollIntoView?.({ block: 'nearest' }); }, [active]);
  function close() { setOpen(false); setActive(-1); }
  function choose(city: string) { onChange(city); close(); }
  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const count = cityMatches(cities, value).length;
      if (!count) return;
      const down = event.key === 'ArrowDown';
      setOpen(true);
      setActive((index) => !open || index < 0 ? (down ? 0 : count - 1) : (index + (down ? 1 : -1) + count) % count);
    } else if (event.key === 'Enter' && open && options[active]) {
      event.preventDefault();
      choose(options[active]);
    } else if (event.key === 'Escape' && open) {
      event.preventDefault();
      close();
    }
  }
  return <div className={`field combobox${error ? ' field--invalid' : ''}`}>
    <label htmlFor={inputId} className="field__label">{label}</label>
    <div className="combobox__control">
      <input id={inputId} className="input combobox__input" role="combobox" required={required} autoComplete="off" value={value}
        aria-autocomplete="list" aria-expanded={shown} aria-controls={listId} aria-activedescendant={open && active >= 0 ? optionId(active) : undefined}
        aria-describedby={describedBy(inputId, hint, error)} aria-invalid={error ? true : undefined}
        onChange={(event) => { onChange(event.target.value); setOpen(true); setActive(-1); }}
        onClick={() => setOpen(true)} onBlur={close} onKeyDown={keyDown} />
      <ul id={listId} role="listbox" aria-label={copy.setup.business.cityOptions} className="combobox__list" hidden={!shown || !options.length}>
        {options.map((city, index) => <li key={city} id={optionId(index)} role="option" aria-selected={index === active}
          className={index === active ? 'combobox__option combobox__option--active' : 'combobox__option'}
          onMouseDown={(event) => event.preventDefault()} onClick={() => choose(city)}>{city}</li>)}
      </ul>
    </div>
    {shown && !options.length && <p className="combobox__empty muted" role="status">{copy.setup.business.cityNotFound}</p>}
    {hint && <p id={`${inputId}-hint`} className="field__hint">{hint}</p>}
    {error && <p id={`${inputId}-error`} className="field__error" role="alert">{error}</p>}
  </div>;
}
