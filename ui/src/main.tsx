import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// The observer route never imports commerce runtime, authentication or assistant SSE.
const isMonitor = window.location.pathname.replace(/\/$/, '') === '/governance'
if (isMonitor) document.title = 'Monitor de gobernanza · PARTIAL'
const Entry = isMonitor
  ? lazy(() => import('./features/governance-monitor/route.tsx'))
  : lazy(() => import('./App.tsx'))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Suspense fallback={<p className="p-6">Cargando vista…</p>}>
      <Entry />
    </Suspense>
  </StrictMode>,
)
