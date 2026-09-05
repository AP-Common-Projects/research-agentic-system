import { Navigate, Route, Routes } from 'react-router-dom';
import { Shell } from './components/Shell';
import { NewRunPage } from './pages/NewRunPage';
import { WorkbooksPage } from './pages/WorkbooksPage';
import { BalancesPage } from './pages/BalancesPage';
import { GraphPage } from './pages/GraphPage';
import { SpendPage } from './pages/SpendPage';
import { ChannelsPage } from './pages/ChannelsPage';

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/new" replace />} />
        <Route path="/new" element={<NewRunPage />} />
        <Route path="/workbooks" element={<WorkbooksPage />} />
        <Route path="/graph" element={<GraphPage />} />
        <Route path="/spend" element={<SpendPage />} />
        <Route path="/balances" element={<BalancesPage />} />
        <Route path="/channels" element={<ChannelsPage />} />
        <Route path="*" element={<Navigate to="/new" replace />} />
      </Route>
    </Routes>
  );
}
