import { copy } from '../copy';

export function MedicalNotice() {
  return <div className="notice"><p>{copy.launchFlow.medical}</p><p>{copy.launchFlow.medicalSteps}</p><p>{copy.launchFlow.medicalDisclaimer}</p></div>;
}
export function SiteInstructions() {
  return <div className="page-stack"><h2>{copy.setup.website.install}</h2>
    <ol>{copy.launchFlow.installSteps.map((text) => <li key={text}>{text}</li>)}</ol>
    <a href="https://metrica.yandex.ru/" target="_blank" rel="noreferrer">{copy.launchFlow.metricaLink}</a>
  </div>;
}
