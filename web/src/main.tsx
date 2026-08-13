import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import '@fontsource-variable/space-grotesk';
import '@fontsource-variable/inter';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import './index.css';

import App from './App';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The console reads a long-running batch job; refetching on every window
      // focus would hammer Postgres for data that changes on the order of
      // minutes. Pages that need freshness set their own refetchInterval.
      refetchOnWindowFocus: false,
      staleTime: 5_000,
      retry: 1,
    },
  },
});

// Dark is the default for a monitoring console; the toggle in the rail
// persists an explicit choice, and the tokens honour prefers-color-scheme
// when nothing has been chosen.
if (!localStorage.getItem('niche-harness-theme')) {
  document.documentElement.setAttribute('data-theme', 'dark');
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
