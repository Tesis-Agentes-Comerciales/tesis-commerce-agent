// Audit-only static preview. No development auth proxy; API returns local 503.
export default {
  plugins: [{ name: 'audit-no-api', configurePreviewServer(server) {
    server.middlewares.use((req, res, next) => {
      if (req.url?.startsWith('/api/')) {
        res.statusCode = 503;
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ error: 'AUDIT_BACKEND_DISABLED' }));
      } else next();
    });
  }}],
  preview: { host: '127.0.0.1', port: 5182, strictPort: true, proxy: {} },
};
