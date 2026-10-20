import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// Opt-in at build time: without VITE_GOVERNANCE_MONITOR=true the monitor is not bundled and /governance renders App.
// When enabled it sits behind App's Better Auth gate but never imports the commerce runtime or assistant SSE.
const isMonitor = import.meta.env.VITE_GOVERNANCE_MONITOR === 'true'
  && window.location.pathname.replace(/\/$/, '') === '/governance'
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
