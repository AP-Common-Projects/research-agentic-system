import { Navigate, Route, Routes } from 'react-router-dom';
import { Shell } from './components/Shell';
import { RunsPage } from './pages/RunsPage';
import { RunDetailPage } from './pages/RunDetailPage';
import { ReportsPage } from './pages/ReportsPage';
import { GraphPage } from './pages/GraphPage';
import { ChannelsPage } from './pages/ChannelsPage';
import { SpendPage } from './pages/SpendPage';

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/runs" replace />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/:runId" element={<RunDetailPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/graph" element={<GraphPage />} />
        <Route path="/channels" element={<ChannelsPage />} />
        <Route path="/spend" element={<SpendPage />} />
        <Route path="*" element={<Navigate to="/runs" replace />} />
      </Route>
    </Routes>
  );
}
