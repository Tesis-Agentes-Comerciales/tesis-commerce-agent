# AP2 v0.2 Human Present: perfil sintético

Referencia fijada: [especificación AP2 v0.2, commit `e1ea56d`](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/specification.md). AP2 autoriza el pago; ACP sigue definiendo el checkout comercial. Este perfil no mueve dinero real ni implementa Human Not Present.

## Fronteras de confianza

1. Better Auth identifica al comprador con correo y contraseña. La API verifica su JWT y la propiedad de la compra en cada ruta. Ese JWT **no** es un mandato AP2.
2. El merchant emite un Checkout JWT ES256 con la instantánea ACP y su revisión interna. El agente verifica emisor, audiencia, firma, vencimiento y coincidencia con el checkout actual. Esta ruta (`GET /checkout_sessions/{id}/ap2/checkout-jwt`) es un puente específico de la demo, no una ruta ACP estándar.
3. La UI muestra el total, líneas, envío y datos de entrega. Solo el clic explícito de ese comprador autenticado llama a la ruta determinista de aprobación. El LLM no puede invocarla ni recibe las claves. La ruta vuelve a leer el checkout del merchant y compara firma, términos y fingerprint antes de que el servidor firme Checkout Mandate y Payment Mandate con SD-JWT del SDK AP2.
4. El proveedor sandbox verifica el Payment Mandate contra la clave pública fijada del proveedor del agente, el Checkout JWT contra la clave del merchant fijada, y comprueba comercio, checkout, moneda, monto y vencimiento **antes** de emitir un token opaco. Solo el merchant puede confirmar ese token, con una credencial distinta.
5. El merchant verifica el Checkout Mandate y su `checkout_hash` antes de `POST /checkout_sessions/{id}/ap2/complete`. La revisión se comprueba atómicamente dentro de la transacción de inventario/orden. Con `MERCHANT_AP2_REQUIRED=true`, la antigua ruta `/complete` responde 403.
6. Merchant y proveedor firman Checkout Receipt y Payment Receipt. El agente verifica ambos y los conserva con la orden. Los mandatos y el JWT de checkout también se conservan en la SQLite del agente para reintentos idempotentes; proteja ese volumen y no registre su contenido en logs.

La clave privada AP2 pertenece al **proveedor del agente** y firma solo después del consentimiento, no a cada comprador. Es el modelo de confianza elegido para este Human Present: merchant y sandbox confían explícitamente en esa clave pública. Better Auth prueba qué comprador pulsó, pero por sí mismo no constituye firma criptográfica individual. Una futura versión podría añadir passkeys, sin cambiar la separación de responsabilidades del LLM.

## Activación coordinada

En el agente, configure `AGENT_AUTH_MODE=jwt`, `AGENT_AUTH_ISSUER`, `AGENT_AP2_REQUIRED=true` y `AGENT_AP2_PRIVATE_KEY_PEM` (EC P-256). Publique la clave verificadora en `GET /.well-known/ap2/jwks.json`.

En el merchant, configure `MERCHANT_AP2_REQUIRED=true`, `MERCHANT_AP2_PRIVATE_KEY_PEM` (otra clave EC P-256) y `MERCHANT_AP2_AGENT_JWK_JSON` con la JWK pública exacta del agente. El merchant publica su propia clave en `GET /.well-known/ap2/jwks.json`.

En el proveedor, configure `PAYMENT_AP2_REQUIRED=true`, `PAYMENT_AP2_AGENT_JWK_JSON`, `PAYMENT_AP2_MERCHANT_JWK_JSON`, `PAYMENT_AP2_MERCHANT_ISSUER` igual a `ACP_API_BASE_URL`, y `PAYMENT_AP2_PRIVATE_KEY_PEM` (tercera clave EC P-256). Mantenga los tres secretos privados fuera de Git. Las JWK de confianza se fijan por configuración; una rotación requiere actualizar los servicios y probar el flujo antes de retirar una clave.

Los tres flags deben activarse juntos. Si se habilita solo uno, la compra falla cerrado. El reloj comercial del episodio P0 es sintético; las firmas, JWT y mandatos usan el reloj real con vigencia de cinco minutos. Nunca configure un reloj fijo para simular vigencia criptográfica en una demo pública.

## Alcance que no afirmamos

Es una implementación verificable del camino Human Present sobre ACP y un proveedor de pago **sintético**, no una certificación de conformidad AP2 completa. No hay Human Not Present, mandatos de intención anticipada, passkeys por comprador, 3DS, credenciales reales, liquidación, reembolsos ni rotación automática de claves. `ap2/complete` y `ap2/checkout-jwt` son endpoints de integración locales, no extensiones normativas de ACP.
