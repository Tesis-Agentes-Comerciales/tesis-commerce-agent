# Commerce Agent UI

Interfaz React + Vite para el buyer agent. Usa el runtime local y los componentes de
[`assistant-ui`](https://www.assistant-ui.com/) sobre la API FastAPI existente.

## Desarrollo

Desde este directorio:

```sh
cp -n .env.example .env.local
npm install
npm run dev
```

La aplicación queda en `http://127.0.0.1:5173` y espera la API del agente en
`http://127.0.0.1:43120`. `VITE_AGENT_API_URL` permite cambiar ese origen.

## Verificación

```sh
npm run build
npm run lint
```

El smoke de navegador está en `e2e/smoke.py`. Intercepta la API con una respuesta determinista,
recorre solicitud, revisión, aprobación y completion, y guarda capturas bajo `.local/ui-e2e/`.

La UI no contiene llaves ni tokens. Los datos de entrega se envían a FastAPI, y el único control
que autoriza la compra usa el fingerprint de términos que devuelve el backend.
