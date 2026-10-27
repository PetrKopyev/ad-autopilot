import { copy } from '../copy';
import type { StopResult } from '../api/setup';

/** Итог остановки рекламы перед отключением: без подтверждения площадки — предупреждение. */
export function StopNotice({ result }: { result: StopResult }) {
  return result.stopped ? <p role="status">{result.message}</p> : <div className="notice" role="alert"><p>{result.message}</p></div>;
}

export function MedicalNotice() {
  return <div className="notice"><p>{copy.launchFlow.medical}</p><p>{copy.launchFlow.medicalSteps}</p><p>{copy.launchFlow.medicalDisclaimer}</p></div>;
}
export function SiteInstructions() {
  return <div className="page-stack"><h2>{copy.setup.website.install}</h2>
    <ol>{copy.launchFlow.installSteps.map((text) => <li key={text}>{text}</li>)}</ol>
    <a href="https://metrica.yandex.ru/" target="_blank" rel="noreferrer">{copy.launchFlow.metricaLink}</a>
  </div>;
}
