# Compra delegada (HNP) dentro del chat

Estado: **aprobada por el líder** · 22 de octubre de 2026 · sigue a `hnp-governed.md` (#142).

## Problema

En `main` (`d953b1f`), «Delegar» abre un diálogo aparte que exige buscar antes en el chat. La única
ruta del chat es `action=purchase`, que crea un checkout HP, así que delegar obliga a cancelar ese
checkout y pasar al diálogo: cinco pasos en dos superficies. Yair pidió un modo del chat: «quiero
comprar un iPhone 11» y el agente lo compra solo, dentro del chat y con la gobernanza a la vista.

## Alcance

1. El selector «Modo de compra» cambia el **modo del chat** («Comprar conmigo» o «Delegar») y ya
   no abre un diálogo. Sigue visible solo con `/health.hnp_enabled`.
2. En «Delegar», el turno corre `interpret_request` y `search_catalog` y termina **antes** de
   elegir producto: sin checkout, sin compra HP y sin llamadas de escritura al comercio.
3. Debajo de la respuesta aparece, en el hilo, el `DelegationForm` existente con las opciones de la
   lista corta marcadas (hasta 5), el total prellenado con el presupuesto del mensaje (editable;
   vacío y obligatorio si el mensaje no lo trae), la preferencia prellenada con el mensaje
   (recortada a `MAX_PREFERENCE_CHARS`) y la vigencia.
4. «Preparar la autorización» → `DelegationConsentCard` en el hilo → «Autorizar».
5. Tras autorizar, `DelegationStatusCard` queda en el hilo y se relee cada 2 s mientras la tarea
   espera el disparo o compra, con la gobernanza en vivo (`ControlTimeline` de la traza HNP).
6. «Mis delegaciones» (botón pequeño en la cabecera) abre el diálogo con la lista, el estado y la
   revocación. Se quita la tarjeta «Primero busca en el chat» y el formulario sale del diálogo.

Queda fuera: la lógica de gobernanza (D1/D2/D3, políticas, mandatos, `hnp_execution.py`,
`delegation/`), la traza (`trace/`, `trace-copy.ts`), las fixtures HNP de la traza, `evaluation/` y
`demo_stack.py`.

**Decisión del líder (revisa #142).** En el chat el total **sí** se prellena con el presupuesto que
escribió el comprador: lo escribió él, el campo es editable y la tarjeta de consentimiento, que no
genera el modelo, muestra el total exacto antes de firmar.

## Contrato de datos

`POST /assistant` acepta `mode: "present" | "delegate"` (por defecto `"present"`).

- `present`: idéntico a hoy. El estado sigue siendo `{messages, purchase}`.
- `delegate` con HNP apagado: 422 `{"code": "delegation_disabled", "message": "La compra delegada
  no está habilitada en este entorno."}` antes de abrir el stream.
- `delegate` con HNP encendido: el estado termina con `purchase: null` y una vista nueva:

```json
"delegation_draft": {
  "offers": [ProductCard, ...],
  "budget_minor": 200000000,
  "currency": "COP",
  "preference_text": "Quiero audífonos negros por menos de 2 millones"
}
```

- `offers`: las `ProductCard` de la lista corta (máximo 6), disponibles y en COP, del feed
  validado; nunca del texto del modelo.
- `budget_minor`: `max_total_amount_minor` de la intención o, si falta, `max_unit_amount_minor`
  (el comprador dice «máximo X» sin distinguir). `null` si el mensaje no trae presupuesto.
- `preference_text`: el último mensaje del comprador, tal cual.
- Sin opciones, `offers: []`, un texto fijo pide ajustar la búsqueda y la UI no muestra tarjeta.
- El borrador no es autoritativo: el servidor revalida cada variante y el total en
  `POST /delegations/proposals`. El estado que devuelve el cliente nunca reenvía un borrador: el
  backend reconstruye el estado con `messages` y `purchase` y solo un turno `delegate` lo publica.
- La idempotencia es la de siempre (`turn_id` por comprador); la huella incluye el modo.

En la UI, `purchaseMode: "present" | "delegated"` vive en `App` y lo controla `PurchaseModeSwitch`;
el runtime lo lee por referencia al enviar (`mode: "delegate"` con «Delegar»).

## Componentes y estados

- `features/delegation-in-chat.tsx` cablea el borrador del transporte con la API de delegaciones y
  reutiliza `delegationsReducer`. Pasa por `DelegationInChat` (presentación), que solo elige cuál de
  las tarjetas existentes mostrar: `form`, `consent`, `status` o el botón para reabrir el
  formulario si se canceló.
- `Thread` acepta un componente `AfterMessages` que va al pie de los mensajes, dentro del hilo.
- Un escalamiento («Comprar desde el chat») vuelve a «Comprar conmigo» y prellena el compositor.

## Criterios de aceptación

- Python: `delegate` no crea checkout ni compra, no llama a `choose_candidate` ni al comercio y
  devuelve `delegation_draft`; `present` queda idéntico; `delegate` sin HNP se rechaza; dos
  compradores no ven el borrador del otro; un borrador que manda el cliente no se reenvía.
- UI: el selector cambia el modo; el borrador lleva al formulario prellenado, al consentimiento y
  al estado con la gobernanza; historias por estado de la tarjeta en el hilo; E2E con mocks.
- Compuerta local completa y CI en verde.

## Riesgos

- El presupuesto lo extrae el modelo (`interpret_request`). Si lo interpreta como precio unitario
  se usa igual; si no lo extrae, el total queda vacío y es obligatorio.
- Una compra HP pendiente sigue bloqueando cualquier turno, también en «Delegar».
- Sin candidatos, el modo «Delegar» no llama a la recuperación conversacional del modelo.

## Plan de PRs

Un solo PR (`feat/hnp-chat-delegate`): spec, contrato y tipos, backend con TDD, historias, cableado
y E2E.
