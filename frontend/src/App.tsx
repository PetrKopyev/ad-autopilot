import { Link, Route, Routes } from 'react-router-dom';
import { Button, Card } from './components';
import { Layout } from './components/Layout';
import { copy } from './copy';
import { NotFoundPage, PlaceholderPage } from './pages/PlaceholderPage';
import { SetupPage } from './pages/SetupPage';

function LoginPage() {
  return (
    <PlaceholderPage title={copy.login.title} description={copy.login.description}>
      <Card className="page-stack">
        <p>{copy.login.description}</p>
        <Button disabled>{copy.login.action}</Button>
        <p className="muted">{copy.login.consent} <Link to="/policy">{copy.login.policy}</Link></p>
      </Card>
    </PlaceholderPage>
  );
}

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<PlaceholderPage title={copy.home.title} />} />
        <Route path="login" element={<LoginPage />} />
        <Route path="setup" element={<SetupPage />} />
        <Route path="setup/:step" element={<SetupPage />} />
        <Route path="plan" element={<PlaceholderPage title={copy.plan.title} description={copy.plan.promise} />} />
        <Route path="ads" element={<PlaceholderPage title={copy.ads.title} />} />
        <Route path="statistics" element={<PlaceholderPage title={copy.statistics.title} description={copy.statistics.provisional} />} />
        <Route path="history" element={<PlaceholderPage title={copy.history.title} />} />
        <Route path="clients" element={<PlaceholderPage title={copy.clients.title} description={copy.clients.weekly} />} />
        <Route path="settings" element={<PlaceholderPage title={copy.settings.title} description={copy.settings.budgetHint} />} />
        <Route path="connect/vk" element={<PlaceholderPage title={copy.service.vkTitle} description={copy.service.vkInstructions} />} />
        <Route path="platform-error" element={<PlaceholderPage title={copy.service.errorsTitle} />} />
        <Route path="policy" element={<PlaceholderPage title={copy.service.policyTitle} description={copy.service.policyPending} />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
