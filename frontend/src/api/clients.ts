import { mockEnabled } from './screens';
import { setupGet, setupRequest } from './setup';

export interface ClientQuestion {
  id: string; week_start: string; week_end: string; leads_count: number;
  clients_count: number | null; asked_at: string; answered_at: string | null;
}
export interface ClientQuestions { items: ClientQuestion[]; cost_per_client_kop: number | null }

export async function getClientQuestions(signal?: AbortSignal): Promise<ClientQuestions> {
  if (mockEnabled) return (await import('./mocks')).mockClientQuestions();
  return setupGet('/api/clients/questions', signal);
}
export async function answerClientQuestion(id: string, clients: number | 'unknown') {
  const clients_count = clients === 'unknown' ? 'не знаю' : clients;
  if (mockEnabled) { (await import('./mocks')).mockClientAnswer(id, clients === 'unknown' ? null : clients); return; }
  await setupRequest(`/api/clients/questions/${encodeURIComponent(id)}/answer`, { method: 'POST', body: { clients_count } });
}
