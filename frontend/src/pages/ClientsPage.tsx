import { useCallback, useState } from 'react';
import { Button, ErrorBanner, Field, Icon, MetricTile, PageHeader, PanelCard, describedBy } from '../components';
import { copy } from '../copy';
import { answerClientQuestion, getClientQuestions, parseClients } from '../api/clients';
import type { ClientQuestion } from '../api/clients';
import { getPeriod } from '../api/period';
import { getSummary } from '../api/screens';
import { displayDate } from '../format';
import { formatMoney } from '../components/Money';
import { useResource } from '../api/useResource';
import { PrelaunchEmpty, usePrelaunch } from './Prelaunch';
import { clientWindowPrice } from './StatsShared';
import './Clients.css';

const labels = copy.clientsScreen;

function QuestionRow({ question, onAnswered }: { question: ClientQuestion; onAnswered: () => void }) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const { number, error: invalid } = parseClients(value, question.leads_count);
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
    <form className="clients-question__form" onSubmit={(event) => { event.preventDefault(); if (number !== undefined) void send(number); }}>
      <Field id={id} label={labels.countLabel} hint={range} error={invalid}>
        {/* Текстовое поле, а не числовое: браузер не выбрасывает молча запятую из «1,5», и ошибку видно. */}
        <input id={id} className="input mono" inputMode="numeric" autoComplete="off" value={value}
          aria-describedby={describedBy(id, range, invalid)} aria-invalid={!!invalid} onChange={(event) => setValue(event.target.value)} />
      </Field>
      <div className="actions"><Button type="submit" disabled={busy || number === undefined}>{busy ? labels.sending : labels.send}</Button>
        <Button variant="secondary" disabled={busy} onClick={() => void send('unknown')}>{copy.clients.unknown}</Button></div>
    </form>
    {error && <div className="clients-question__error"><ErrorBanner message={error} onDismiss={() => setError(undefined)} /></div>}
  </li>;
}

export function ClientsPage() {
  const questions = useResource(useCallback((signal: AbortSignal) => getClientQuestions(signal), []));
  // Плитка «Цена клиента» — из того же окна клиентов, что на «Статистике»: один порог ответов на обоих экранах.
  const summary = useResource(useCallback(async (signal: AbortSignal) => getSummary(await getPeriod('month', signal), signal), []));
  const items = questions.data?.items ?? [];
  const open = items.filter((item) => !item.answered_at);
  const answered = items.filter((item) => item.answered_at);
  const window = summary.data?.clients_window;
  const cost = clientWindowPrice(window);
  const prelaunch = usePrelaunch();
  return <div className="page-stack">
    <PageHeader title={copy.clients.title} description={labels.intro} />
    {questions.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {questions.error && <ErrorBanner message={questions.error} onRetry={questions.reload} />}
    {questions.data && !items.length && prelaunch && <div className="panel">
      <PrelaunchEmpty hasPlan={prelaunch.hasPlan} description={copy.prelaunch.clients} /></div>}
    {questions.data && !(items.length === 0 && prelaunch) && <>
      <div className="metric-tiles clients-tiles">
        <MetricTile label={labels.costPerClient} value={cost === null ? '—' : formatMoney(cost)}
          hint={cost === null ? labels.costPerClientEmpty : copy.home.clientsHint(window!.answered_weeks, window!.answered_weeks + window!.unknown_weeks)} />
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
