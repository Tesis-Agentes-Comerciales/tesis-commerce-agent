import app from './vite.config.ts';

// Audit-only static preview. Only Better Auth is proxied, exactly as in development, because the
// monitor shares App's session gate; every other API path returns a local 503.
export default {
  plugins: [{ name: 'audit-no-api', configurePreviewServer(server) {
    server.middlewares.use((req, res, next) => {
      if (req.url?.startsWith('/api/') && !req.url.startsWith('/api/auth/')) {
        res.statusCode = 503;
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ error: 'AUDIT_BACKEND_DISABLED' }));
      } else next();
    });
  }}],
  preview: { host: '127.0.0.1', port: 5182, strictPort: true, proxy: { '/api/auth': app.server.proxy['/api/auth'] } },
};
