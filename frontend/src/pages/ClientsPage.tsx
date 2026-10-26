import { useCallback, useState } from 'react';
import { Button, Card, ErrorBanner } from '../components';
import { copy } from '../copy';
import { answerClientQuestion, getClientQuestions } from '../api/clients';
import type { ClientQuestion } from '../api/clients';
import { displayDate } from '../api/period';
import { formatMoney } from '../components/Money';
import { useResource } from '../api/useResource';
import './Setup.css';

const labels = copy.clientsScreen;

function QuestionCard({ question, onAnswered }: { question: ClientQuestion; onAnswered: () => void }) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const number = value.trim() === '' ? NaN : Number(value);
  const valid = Number.isInteger(number) && number >= 0 && number <= question.leads_count;
  async function send(answer: number | 'unknown') {
    setBusy(true); setError(undefined);
    try { await answerClientQuestion(question.id, answer); onAnswered(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); setBusy(false); }
  }
  return <Card className="page-stack">
    <h3>{copy.clients.question(question.leads_count)}</h3>
    <p className="muted">{labels.week(displayDate(question.week_start), displayDate(question.week_end))}</p>
    <form className="page-stack" onSubmit={(event) => { event.preventDefault(); if (valid) void send(number); }}>
      <label className="field">{labels.countLabel}
        <input type="number" inputMode="numeric" min={0} max={question.leads_count} step={1} value={value} aria-describedby={`range-${question.id}`}
          aria-invalid={value !== '' && !valid} onChange={(event) => setValue(event.target.value)} /></label>
      <p className="muted" id={`range-${question.id}`}>{labels.range(question.leads_count)}</p>
      {error && <ErrorBanner message={error} onDismiss={() => setError(undefined)} />}
      <div className="actions"><Button type="submit" disabled={busy || !valid}>{busy ? labels.sending : labels.send}</Button>
        <Button variant="secondary" disabled={busy} onClick={() => void send('unknown')}>{copy.clients.unknown}</Button></div>
    </form>
  </Card>;
}

export function ClientsPage() {
  const questions = useResource(useCallback((signal: AbortSignal) => getClientQuestions(signal), []));
  const items = questions.data?.items ?? [];
  const open = items.filter((item) => !item.answered_at);
  const answered = items.filter((item) => item.answered_at);
  return <div className="page-stack launch-flow">
    <h1>{copy.clients.title}</h1>
    <p className="muted">{labels.intro}</p>
    {questions.loading && <p role="status">{copy.screens.loading}</p>}
    {questions.error && <ErrorBanner message={questions.error} onRetry={questions.reload} />}
    {questions.data && <>
      <Card className="page-stack"><h2>{labels.costPerClient}</h2>
        <p>{questions.data.cost_per_client_kop === null ? labels.costPerClientEmpty : formatMoney(questions.data.cost_per_client_kop)}</p></Card>
      <h2>{labels.open}</h2>
      {open.length ? open.map((item) => <QuestionCard key={item.id} question={item} onAnswered={questions.reload} />)
        : <p className="muted">{labels.nothingOpen}</p>}
      <h2>{labels.answered}</h2>
      {answered.length ? <ul className="page-stack">{answered.map((item) => <li key={item.id}><Card>
        <strong>{labels.week(displayDate(item.week_start), displayDate(item.week_end))}</strong>
        <p>{item.clients_count === null ? labels.answerUnknown(item.leads_count) : labels.answerLine(item.clients_count, item.leads_count)}</p>
      </Card></li>)}</ul> : <p className="muted">{labels.nothingAnswered}</p>}
    </>}
  </div>;
}
