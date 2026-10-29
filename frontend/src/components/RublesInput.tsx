import { useRef, type ChangeEvent, type InputHTMLAttributes } from 'react';

export const groupDigits = (digits: string) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
// Длиннее суммы не бывает: больше предела поле принимает, а ошибку показывает форма.
const maxWholeDigits = 12;

/** Рубли и копейки из набранного: точка и запятая — разделитель, лишние нули слева убираются. */
// Разделитель — точка или запятая сразу после цифры: точка в «567 руб.» копейками не считается.
// Минус в начале остаётся: «-100» не должно молча стать 100 ₽, форма покажет ошибку.
// Точки делят разряды, только если до первой 1–3 цифры и после каждой ровно три: «50.000», «1.500.000», «1.500,50».
// Иначе точка — копейки. Знаки после копеечного разделителя не обрезаются: «1500.555» и «1 500,555» — ошибка формы,
// а не 1 500 555 ₽ или молча отрезанная цифра.
// grouped — рубли, уже собранные из разрядов через точку: набранное по клавише «1.500» стало «1 500», и «.000» за ним — тоже разряд.
// Набранная точка остаётся точкой, пока не ясно, копейки ли за ней: иначе «1.50» при наборе «1.500,50» становилось «1,50».
// Точка и запятая вместе — «1.500,50» или «1,500.50»: копейки после последнего, первый делит разряды.
function normalize(raw: string, grouped = ''): [value: string, grouping: boolean] {
  const sign = /^\s*[-−–]/.test(raw) ? '-' : '';
  const groups = /^(\s*[-−–]?\s*)([\d\s]+?)((?:\.\d{3})+)(?![\d.])/.exec(raw);
  const text = groups && (/^[1-9]\d{0,2}$/.test(groups[2]) || (grouped && groups[2] === groupDigits(grouped)))
    ? groups[1] + groups[2] + groups[3].replace(/\./g, '') + raw.slice(groups[0].length) : raw;
  const separators = [...text.matchAll(/(?:^|\d)([.,])/g)].map((match) => match.index + match[0].length - 1);
  const mixed = new Set(separators.map((index) => text[index])).size > 1;
  const at = separators.length ? separators[mixed ? separators.length - 1 : 0] : -1;
  const whole = (at < 0 ? text : text.slice(0, at)).replace(/\D/g, '').replace(/^0+/, '').slice(0, maxWholeDigits);
  if (at < 0) return [sign + whole, text !== raw];
  const fraction = text.slice(at + 1).replace(/\D/g, '');
  return [`${sign}${whole || '0'}${mixed ? ',' : text[at]}${fraction}`, text !== raw];
}
const wholeOf = (value: string) => value.replace(/^-/, '').split(/[.,]/)[0];
/** Значение поля из копеек: 5 000 050 → «50000,50», без округления. */
export const rublesValue = (kopecks: number) => {
  const sign = kopecks < 0 ? '-' : '';
  const amount = Math.abs(kopecks);
  return amount % 100 ? `${sign}${Math.trunc(amount / 100)},${String(amount % 100).padStart(2, '0')}` : String(kopecks / 100);
};
const display = (value: string) => {
  const sign = value.startsWith('-') ? '-' : '';
  const rest = value.slice(sign.length);
  const at = rest.search(/[.,]/);
  return sign + (at < 0 ? groupDigits(rest) : groupDigits(rest.slice(0, at)) + rest.slice(at));
};

export interface RublesInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'inputMode'> {
  /** Рубли без пробелов, копейки через запятую или набранную точку: «1500», «1500,5», «1500.5». */
  value: string;
  onChange: (value: string) => void;
}

/** Сумма в рублях: разряды через пробел, копейки через запятую или точку, курсор остаётся у того же знака. */
export function RublesInput({ value, onChange, ...props }: RublesInputProps) {
  const grouped = useRef('');
  function change(event: ChangeEvent<HTMLInputElement>) {
    const input = event.target;
    const [next, grouping] = normalize(input.value, grouped.current);
    const [typed] = normalize(input.value.slice(0, input.selectionStart ?? input.value.length), grouped.current);
    if (grouping || wholeOf(next) !== grouped.current) grouped.current = grouping ? wholeOf(next) : '';
    const before = Math.min(next.length, typed.length);
    const shown = display(next);
    let caret = 0;
    for (let seen = 0; caret < shown.length && seen < before; caret += 1) if (shown[caret] !== ' ') seen += 1;
    input.value = shown;
    input.setSelectionRange(caret, caret);
    if (next !== value) onChange(next);
  }
  return <input {...props} inputMode="decimal" autoComplete="off" value={display(value)} onChange={change} />;
}
