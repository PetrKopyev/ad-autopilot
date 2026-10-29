import { useCallback, useState } from 'react';
import { Button, ErrorBanner, Field, Icon, MetricTile, PageHeader, PanelCard, describedBy } from '../components';
import { copy } from '../copy';
import { answerClientQuestion, getClientQuestions } from '../api/clients';
import type { ClientQuestion } from '../api/clients';
import { displayDate } from '../format';
import { formatMoney } from '../components/Money';
import { useResource } from '../api/useResource';
import './Clients.css';

const labels = copy.clientsScreen;

function QuestionRow({ question, onAnswered }: { question: ClientQuestion; onAnswered: () => void }) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const number = value.trim() === '' ? NaN : Number(value);
  const valid = Number.isInteger(number) && number >= 0 && number <= question.leads_count;
  const id = `answer-${question.id}`;
  const range = labels.range(question.leads_count);
  async function send(answer: number | 'unknown') {
    setBusy(true); setError(undefined);
    try { await answerClientQuestion(question.id, answer); onAnswered(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); setBusy(false); }
  }
  return <li className="clients-question">
    <div className="clients-question__text">
      <h3>{copy.clients.question(question.leads_count)}</h3>
      <p className="muted mono">{labels.week(displayDate(question.week_start), displayDate(question.week_end))}</p>
    </div>
    <form className="clients-question__form" onSubmit={(event) => { event.preventDefault(); if (valid) void send(number); }}>
      <Field id={id} label={labels.countLabel} hint={range}>
        <input id={id} className="input mono" type="number" inputMode="numeric" min={0} max={question.leads_count} step={1} value={value}
          aria-describedby={describedBy(id, range)} aria-invalid={value !== '' && !valid} onChange={(event) => setValue(event.target.value)} />
      </Field>
      <div className="actions"><Button type="submit" disabled={busy || !valid}>{busy ? labels.sending : labels.send}</Button>
        <Button variant="secondary" disabled={busy} onClick={() => void send('unknown')}>{copy.clients.unknown}</Button></div>
    </form>
    {error && <div className="clients-question__error"><ErrorBanner message={error} onDismiss={() => setError(undefined)} /></div>}
  </li>;
}

export function ClientsPage() {
  const questions = useResource(useCallback((signal: AbortSignal) => getClientQuestions(signal), []));
  const items = questions.data?.items ?? [];
  const open = items.filter((item) => !item.answered_at);
  const answered = items.filter((item) => item.answered_at);
  const cost = questions.data?.cost_per_client_kop ?? null;
  return <div className="page-stack">
    <PageHeader title={copy.clients.title} description={labels.intro} />
    {questions.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {questions.error && <ErrorBanner message={questions.error} onRetry={questions.reload} />}
    {questions.data && <>
      <div className="metric-tiles clients-tiles">
        <MetricTile label={labels.costPerClient} value={cost === null ? '—' : formatMoney(cost)} hint={cost === null ? labels.costPerClientEmpty : undefined} />
        <MetricTile label={labels.openCount} value={open.length.toLocaleString('ru-RU')} hint={labels.openCountHint} />
      </div>
      <PanelCard title={labels.open} titleId="clients-open" flush>
        {open.length ? <ul className="clients-list">{open.map((item) => <QuestionRow key={item.id} question={item} onAnswered={questions.reload} />)}</ul>
          : <p className="clients-empty"><Icon name="check" />{labels.nothingOpen}</p>}
      </PanelCard>
      <PanelCard title={labels.answered} titleId="clients-answered" flush>
        {answered.length ? <ul className="clients-list">{answered.map((item) => <li key={item.id} className="clients-answer">
          <span className="mono">{labels.week(displayDate(item.week_start), displayDate(item.week_end))}</span>
          <strong className={item.clients_count === null ? 'muted' : undefined}>
            {item.clients_count === null ? labels.answerUnknown(item.leads_count) : labels.answerLine(item.clients_count, item.leads_count)}</strong>
        </li>)}</ul> : <p className="clients-empty">{labels.nothingAnswered}</p>}
      </PanelCard>
    </>}
  </div>;
}
