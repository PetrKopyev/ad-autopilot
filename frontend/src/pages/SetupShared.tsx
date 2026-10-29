import { copy } from '../copy';
import type { FieldProps } from '../components/Field';
import type { StopResult } from '../api/setup';

/** Итог остановки рекламы перед отключением: без подтверждения площадки — предупреждение. */
export function StopNotice({ result }: { result: StopResult }) {
  return result.stopped ? <p role="status">{result.message}</p> : <div className="notice" role="alert"><p>{result.message}</p></div>;
}

export function MedicalNotice() {
  return <div className="notice"><p>{copy.launchFlow.medical}</p><p>{copy.launchFlow.medicalSteps}</p><p>{copy.launchFlow.medicalDisclaimer}</p></div>;
}
export function SiteInstructions() {
  return <div className="page-stack"><h3>{copy.setup.website.install}</h3>
    <ol>{copy.launchFlow.installSteps.map((text) => <li key={text}>{text}</li>)}</ol>
    <a href="https://metrica.yandex.ru/" target="_blank" rel="noreferrer">{copy.launchFlow.metricaLink}</a>
  </div>;
}

/** Поле мастера как Field, но в подсказке может быть несколько абзацев, а ошибка объявляется сразу. */
export function SetupField({ id, label, hint, error, children }: FieldProps) {
  return <div className={`field${error ? ' field--invalid' : ''}`}>
    <label htmlFor={id} className="field__label">{label}</label>
    {children}
    {hint && <div id={`${id}-hint`} className="field__hint">{hint}</div>}
    {error && <p id={`${id}-error`} className="field__error" role="alert">{error}</p>}
  </div>;
}
