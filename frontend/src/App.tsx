import { Route, Routes } from 'react-router';
import { Layout } from './components/Layout';
import { NotFoundPage } from './pages/PlaceholderPage';
import { SetupPage } from './pages/SetupPage';
import { LoginPage } from './pages/LoginPage';
import { PlanPage } from './pages/PlanPage';
import { ServiceErrorPage, ServicePolicyPage, ServiceVkPage } from './pages/ServicePage';
import { HomePage } from './pages/HomePage';
import { AdsPage } from './pages/AdsPage';
import { StatsPage } from './pages/StatsPage';
import { HistoryPage } from './pages/HistoryPage';
import { ClientsPage } from './pages/ClientsPage';
import { SettingsPage } from './pages/SettingsPage';

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
        <Route path="history" element={<HistoryPage />} />
        <Route path="clients" element={<ClientsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="connect/vk" element={<ServiceVkPage />} />
        <Route path="platform-error" element={<ServiceErrorPage />} />
        <Route path="policy" element={<ServicePolicyPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
