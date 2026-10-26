import { Route, Routes } from 'react-router';
import { Layout } from './components/Layout';
import { copy } from './copy';
import { NotFoundPage, PlaceholderPage } from './pages/PlaceholderPage';
import { SetupPage } from './pages/SetupPage';
import { LoginPage } from './pages/LoginPage';
import { PlanPage } from './pages/PlanPage';
import { ServiceErrorPage, ServicePolicyPage, ServiceVkPage } from './pages/ServicePage';
import { HomePage } from './pages/HomePage';
import { AdsPage } from './pages/AdsPage';
import { StatsPage } from './pages/StatsPage';

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="login" element={<LoginPage />} />
        <Route path="setup" element={<SetupPage />} />
        <Route path="setup/:step" element={<SetupPage />} />
        <Route path="plan" element={<PlanPage />} />
        <Route path="ads" element={<AdsPage />} />
        <Route path="statistics" element={<StatsPage />} />
        <Route path="history" element={<PlaceholderPage title={copy.history.title} />} />
        <Route path="clients" element={<PlaceholderPage title={copy.clients.title} description={copy.clients.weekly} />} />
        <Route path="settings" element={<PlaceholderPage title={copy.settings.title} description={copy.settings.budgetHint} />} />
        <Route path="connect/vk" element={<ServiceVkPage />} />
        <Route path="platform-error" element={<ServiceErrorPage />} />
        <Route path="policy" element={<ServicePolicyPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
