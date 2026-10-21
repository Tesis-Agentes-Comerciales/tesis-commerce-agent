# AP2 v0.2: perfil sintético (Human Present y Human Not Present)

Referencia fijada: [especificación AP2 v0.2, commit `e1ea56d`](https://github.com/google-agentic-commerce/AP2/blob/e1ea56db72a6385bce3e5c1112b3a56ce60acb43/docs/ap2/specification.md). AP2 autoriza el pago; ACP sigue definiendo el checkout comercial. Este perfil no mueve dinero real. La sección [Human Not Present](#human-not-present-perfil-ap2-hnpv1) es el contrato de HNP-04 (agente), HNP-M1 (comercio) y HNP-P1 (PSP); hasta que esos PRs se fusionen, solo Human Present está implementado.

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

## Human Not Present (perfil `ap2-hnp/v1`)

Contrato compartido por el agente (HNP-04), el comercio (HNP-M1) y el PSP (HNP-P1). El flujo, los
estados y las decisiones de política están en [la spec de HNP](design/hnp-governed.md). Nada de esta
sección está implementado al fusionar HNP-00.

La columna «Fuente» dice de dónde sale cada afirmación:

- **spec:** especificación AP2 v0.2 en `e1ea56d` (`agent_authorization.md`, `checkout_mandate.md`,
  `payment_mandate.md`, `specification.md`).
- **SDK:** el paquete `ap2` del mismo commit, instalado en el `.venv` del repo; leído y, donde dice
  «probado», ejercitado con un script contra el paquete instalado.
- **perfil:** decisión nuestra; no se puede verificar contra AP2.

### Mandatos y cadenas

| Elemento | Valor | Fuente |
| --- | --- | --- |
| `vct` de los abiertos | `mandate.checkout.open.1` y `mandate.payment.open.1` | SDK (`OpenCheckoutMandate`, `OpenPaymentMandate`) |
| `vct` de los cerrados | `mandate.checkout.1` y `mandate.payment.1` | SDK |
| Forma | `<abierto SD-JWT>~~<cerrado KB-SD-JWT>`, exactamente dos segmentos | SDK (`MandateClient.present` y `verify`; `CheckoutMandateChain.parse` y `PaymentMandateChain.parse` exigen dos) |
| Raíz de confianza | La clave del proveedor del agente (`AGENT_AP2_PRIVATE_KEY_PEM`) firma los abiertos. El comercio (`MERCHANT_AP2_AGENT_JWK_JSON`) y el PSP (`PAYMENT_AP2_AGENT_JWK_JSON`) ya la fijan para HP, así que la confianza entre servicios no cambia | perfil (el mismo modelo de confianza de HP) |
| Resolución de la clave raíz | La cabecera del SD-JWT raíz lleva `kid` = thumbprint RFC 7638 de la clave del proveedor: jwcrypto lo calcula al cargar el PEM y el SDK lo copia (cabecera `{"alg":"ES256","typ":"example+sd-jwt","kid":…}`). Los verificadores usan un proveedor propio que exige `kid` = thumbprint de la JWK fijada y rechaza `x5c`; si no, `invalid_credential`. No usan `X5cOrKidPublicKeyProvider` tal cual: sin `trusted_roots` acepta cualquier cadena `x5c` | SDK (probado y leído); el proveedor es perfil |
| `cnf` | `{"jwk": <Agent Key pública>}` en los dos abiertos. Agent Key: `AGENT_HNP_AGENT_KEY_PEM`, ES256, distinta de la del proveedor | spec (`cnf` es obligatorio mientras el mandato está abierto, RFC 7800); SDK (`ParsedToken.cnf_jwk` lee `cnf.jwk`) |
| Cierre | KB-SD-JWT terminal (`typ` `kb+sd-jwt`) firmado con la Agent Key, sin `cnf` | SDK (`kb_sd_jwt`: un salto terminal no lleva `cnf`; probado) |
| Vínculo cerrado → abierto | `sd_hash` (modo por defecto de `present`): hash del abierto tal como se presenta, con sus divulgaciones | SDK |
| Algoritmo de hash | SHA-256, base64url sin relleno; los tokens no llevan `_sd_alg` | SDK (`_hash_for_alg(None)`); spec (sha-256 por defecto) |
| Vigencia del abierto | `iat` = consentimiento; `exp` = `expires_at` del grant | perfil |
| Vigencia del cerrado | `exp = iat + 300` | perfil (igual que HP) |
| Tolerancia de reloj | Los verificadores pasan `clock_skew_seconds=60` | SDK: el valor por defecto es 300 s y con él un abierto vencido hace 200 s todavía verifica (probado). perfil: 60 s, la misma holgura de `iat` que HP |
| Divulgación selectiva | Los abiertos se presentan completos | perfil (no llevan datos personales) |

### Restricciones soportadas

| `type` | Lo que emite el agente | La evalúa |
| --- | --- | --- |
| `checkout.allowed_merchants` | `allowed: [{id: "tesis_merchant", name: "Tesis Commerce Merchant", website: <issuer del comercio>}]` | Comercio |
| `checkout.line_items` | `items: [{id: "line_1", quantity: 1, acceptable_items: [{id: <variant_id>, title: <título del feed>}, …]}]` | Comercio |
| `payment.amount_range` | `{currency: "COP", max: <total_max_minor>}`, sin `min` | PSP |
| `payment.allowed_payees` | El mismo comercio de `allowed_merchants` | PSP |
| `payment.allowed_payment_instruments` | `allowed: [{id: "tesis_sandbox", type: "sandbox_token"}]` | PSP |
| `payment.reference` | `{conditional_transaction_id: <hash del checkout abierto>}` | PSP |

- El SDK compara comercios por `id` cuando ambos lo tienen (`merchant_matches`) y exige `title` en
  cada `acceptable_items` (SDK).
- `acceptable_items` vacío es un comodín en el SDK: no produce violaciones (SDK, probado). El agente
  nunca lo emite y los verificadores lo tratan como restricción fuera del perfil (perfil).
- La spec exige tratar una restricción desconocida como fallida (`agent_authorization.md`, «Any
  unknown Constraints MUST be treated as failing evaluation»). El SDK falla con `ValidationError` al
  parsear un `type` desconocido, sin devolver una violación, e **ignora** en silencio una propiedad
  desconocida dentro de una restricción conocida (SDK, probado). Por eso los verificadores revisan el
  JSON antes de `model_validate`: cualquier `type` fuera de esta tabla, incluidos los de AP2 que el
  perfil no usa (`payment.agent_recurrence`, `payment.budget`, `payment.execution_date`,
  `payment.allowed_pisps`), o cualquier propiedad que no esté en el modelo del SDK, da
  `unresolved_constraint` (perfil).
- Los abiertos no fijan `payee`, `payment_amount`, `payment_instrument`, `pisp` ni `execution_date`.
  Un abierto que los traiga da `unresolved_constraint` (perfil).
- **Hash de `payment.reference`:** `conditional_transaction_id` = base64url(SHA-256 del JWT del
  emisor del checkout abierto), es decir `compute_issuer_jwt_hash`. **No verificable:** la spec pide
  que la cadena de checkout contenga un abierto «con un hash que coincida», con `_sd_alg` o sha-256,
  sin decir si el hash cubre las divulgaciones, y el SDK no lo calcula (solo compara el valor que
  recibe). Se elige el JWT del emisor porque no cambia con las divulgaciones presentadas (perfil).

### Saltos KB

| Cadena | `aud` | `nonce` |
| --- | --- | --- |
| Checkout | Issuer del comercio: `MERCHANT_BASE_URL` en el agente, `ACP_API_BASE_URL` en el comercio (el mismo `iss` del Checkout JWT) | `checkout_hash` |
| Pago | `tesis_payment_provider` (el `iss` de los recibos del PSP) | `transaction_id` (= `checkout_hash`) |

- El SDK exige `aud` y `nonce` al crear un salto, pero solo los compara si el verificador los pasa:
  sin `expected_aud` una cadena con otro `aud` verifica igual (SDK, probado). En este perfil los dos
  son obligatorios en la verificación.
- **Adaptación:** en AP2 el `nonce` lo entrega el verificador. Ni el comercio ni el PSP tienen una
  ruta para emitirlo, así que se usa `checkout_hash`. La frescura viene del Checkout JWT (`jti`, `exp`
  de cinco minutos, revisión) y del cierre de 300 s, y el uso único, de la reserva del agente y del
  checkout del comercio (perfil).

### Cierres

- Checkout: `CheckoutMandate{checkout_jwt, checkout_hash, iat, exp}`, con `checkout_hash` =
  base64url(SHA-256 del `checkout_jwt`) (spec `checkout_mandate.md`; igual que HP).
- Pago: `PaymentMandate{transaction_id = checkout_hash, payee = tesis_merchant, payment_amount =
  {amount: <total autoritativo>, currency: "COP"}, payment_instrument = {id: "tesis_sandbox", type:
  "sandbox_token"}, iat, exp}` (SDK; igual que HP).

### Envío

- **PSP primero:** `delegate_payment` con `ap2_checkout_jwt` (como HP), `ap2_payment_mandate` = la
  cadena de pago y el campo nuevo `ap2_checkout_mandate` = la cadena de checkout, para que el PSP
  calcule el hash del abierto de checkout que exige `payment.reference` (spec `payment_mandate.md`;
  el campo es perfil).
- **Comercio después:** `POST /checkout_sessions/{id}/ap2/complete` con `checkout_mandate` = la
  cadena de checkout (el comercio la reconoce por `~~`) y `payment_data` como HP (perfil).

### Verificación en el comercio (HNP-M1)

1. La cadena tiene exactamente dos segmentos.
2. `MandateClient.verify` con el proveedor de la raíz de confianza (una cadena exige un proveedor,
   no una JWK), `expected_aud` = `ACP_API_BASE_URL`, `expected_nonce` = hash del `checkout_jwt`
   del cerrado y `clock_skew_seconds=60`.
3. Las restricciones del abierto se revisan en JSON contra este perfil antes de parsear.
4. El `checkout_jwt` del cerrado pasa las comprobaciones de HP (firma propia, `aud`, revisión y
   coincidencia con el checkout actual) y `checkout_hash` es su hash.
5. El cerrado tiene `iat` y `exp`, `exp − iat ≤ 300` e `iat ≤ now + 60`. El abierto no tiene ese
   tope: vence con su `exp`.
6. `check_checkout_constraints(abierto, project_acp_checkout(checkout))`. No se usa
   `CheckoutMandateChain.verify(checkout_jwt=…)`, porque valida el payload del JWT directamente como
   `Checkout` del SDK y nuestro JWT es el envoltorio `{iss, aud, iat, exp, jti, checkout: <ACP>,
   checkout_revision}` (SDK).
7. Un reintento idempotente sobre un checkout ya `completed` acepta el mismo cierre aunque haya
   vencido, como HP.

### Verificación en el PSP (HNP-P1)

1. La cadena de pago se verifica con el proveedor de la raíz de confianza,
   `expected_aud="tesis_payment_provider"`, `expected_nonce` = hash de `ap2_checkout_jwt` y
   `clock_skew_seconds=60`.
2. La cadena de checkout de `ap2_checkout_mandate` se verifica igual, con el `aud` del comercio
   (`PAYMENT_AP2_MERCHANT_ISSUER`);
   el hash del JWT del emisor de su abierto debe ser `conditional_transaction_id` y el
   `checkout_hash` de su cerrado, `transaction_id`. Sin ese campo: `unresolved_constraint`.
3. `PaymentMandateChain.verify(expected_transaction_id=<hash de ap2_checkout_jwt>,
   expected_open_checkout_hash=<hash del abierto de checkout>)` (SDK).
4. Las comprobaciones de HP sobre el cerrado y la `allowance` (comercio, instrumento, monto =
   `max_amount`, moneda, checkout `ready_for_payment` y un solo total). El tope `exp − iat ≤ 300`
   aplica solo al cerrado.

### Recibos

- **`reference`:** `compute_sd_hash(parse_token(<último segmento de la cadena>))`, el mismo cálculo
  que HP aplica a su mandato único. La spec dice que, con una cadena, es el hash del último SD-JWT
  «calculado igual que `sd_hash`» (`agent_authorization.md`). **Divergencia:** la docstring de
  `MandateClient.get_closed_mandate_jwt` sugiere el SHA-256 del JWT cerrado sin divulgaciones, y los
  dos valores no coinciden (SDK, probado). El perfil sigue la spec y HP.
- **Éxito:** los mismos campos que HP: `order_id` en el comercio; `payment_id`,
  `psp_confirmation_id` y `network_confirmation_id` en el PSP.
- **Error:** `status: "Error"`, `iss`, `iat`, `reference`, `error` (código AP2) y
  `error_description` (SDK, `CheckoutReceiptError` y `PaymentReceiptError`). Si el último segmento no
  se puede parsear, `reference` = base64url(SHA-256 del segmento tal como llegó) (perfil).
- **HTTP:** el comercio responde 422 `{"code": <código AP2>, "checkout_receipt_jwt": <recibo>}` y el
  PSP, 422 `{"code": <código AP2>, "payment_receipt_jwt": <recibo>}`. `mandates_not_supported` es 503
  sin recibo cuando el verificador no tiene AP2 configurado, porque no tiene con qué firmar. El
  camino HP conserva sus respuestas (perfil). La spec pide recibo de error también en HP; HP no lo
  emite hoy y no cambia en esta ola.

### Errores

La spec define los cuatro códigos y dice que `invalid_credential` e `invalid_mandate` son terminales,
y que `unresolved_constraint` y `mandates_not_supported` pueden usarse para volver a un flujo no
agéntico (`agent_authorization.md`). El reparto de causas es del perfil.

| Código | Causa | Qué hace el agente |
| --- | --- | --- |
| `invalid_credential` | Firma o raíz inválidas; salto firmado con una clave distinta del `cnf`; `sd_hash` que no coincide; `aud` o `nonce` distintos; `typ` inesperado; `exp` vencido o `iat` futuro; token mal formado o número de segmentos distinto de dos | Terminal: `failed` y alerta al operador |
| `invalid_mandate` | El cerrado viola una restricción soportada (monto, payee, instrumento, comercio, variante); `checkout_hash` o `transaction_id` distintos del JWT presentado; `payment.reference` que no coincide; cerrado con `exp − iat > 300`; checkout con más de una línea o cantidad distinta de 1 | Terminal: `failed` y alerta |
| `unresolved_constraint` | `type` o propiedad fuera del perfil; `acceptable_items` vacío; abierto con claims prefijados; checkout no proyectable; falta `ap2_checkout_mandate` en el PSP | Escala (si el PSP ya emitió el token, la compra queda `uncertain`) |
| `mandates_not_supported` | El verificador no tiene AP2 configurado | Escala |

### Adaptación ACP→SDK (no es conformidad)

`check_checkout_constraints` espera un `Checkout` del SDK (UCP `dev.ucp.shopping.checkout`); nuestro
comercio firma un envoltorio con el checkout ACP. `project_acp_checkout` es una proyección
determinista que corre igual en el agente y en el comercio:

| Campo del `Checkout` del SDK | Desde ACP |
| --- | --- |
| `id` | `checkout.id` |
| `merchant` | Configuración del verificador: `{id: "tesis_merchant", name: "Tesis Commerce Merchant"}` (ACP no trae comercio) |
| `line_items` | Por línea: `{id: line.id, item: {id: line.item.id, title: line.item.name, price: line.item.unit_amount}, quantity: line.quantity, totals: []}` |
| `status` | `ready_for_payment` → `ready_for_complete`; `completed` → `completed`; cualquier otro estado no es proyectable |
| `currency` | En mayúsculas |
| `totals` | `[{type, amount}]` de los totales ACP |
| `links` | `[]` |

El SDK exige `links` y su enum de estados no tiene `ready_for_payment` (SDK, probado). Los datos del
comprador y los mensajes no se proyectan: ninguna restricción los usa. Es una adaptación declarada:
no implementamos UCP ni afirmamos conformidad AP2 de HNP.

### Lo que HNP no afirma

No es una implementación conforme ni certificada de AP2 Human Not Present: el `nonce` no lo emite el
verificador, el checkout se proyecta desde ACP, no hay credencial del usuario (passkeys u OpenID4VP),
ni revocación remota en el comercio o el PSP, ni recurrencia, ni divulgación selectiva, ni rotación
de claves. Hay un solo comercio y un PSP sintético.

## Alcance que no afirmamos

Es una implementación verificable del camino Human Present sobre ACP y un proveedor de pago **sintético**, no una certificación de conformidad AP2 completa. Human Not Present se limita al perfil de una compra de la sección HNP: sin recurrencia, presupuesto acumulado ni subdelegación. No hay passkeys por comprador, 3DS, credenciales reales, liquidación, reembolsos ni rotación automática de claves. `ap2/complete` y `ap2/checkout-jwt` son endpoints de integración locales, no extensiones normativas de ACP.
