# Flujo de trabajo

Todo cambio no trivial sigue estos pasos en orden. Un paso no empieza hasta que el anterior está
terminado; si uno falla, se vuelve al anterior en lugar de parchear encima.

| Paso | Qué se entrega | Dónde | Cuándo está terminado |
| --- | --- | --- | --- |
| 1. Spec | Problema, alcance y lo que queda fuera, contrato de datos, componentes y estados, criterios de aceptación, plan de PRs, riesgos | `docs/design/<tema>.md` | El dueño la aprueba |
| 2. Contrato | Tipos del lado UI y del backend, más fixtures JSON que cubren cada estado | `ui/src/lib/*-contract.ts`, modelos Pydantic en `src/`, fixtures en `ui/src/storybook/` | Los tests de ambos lados validan las mismas fixtures |
| 3. UI en Storybook | Componentes de presentación sin red ni runtime, con una historia por estado, `play` para la interacción y axe en modo error | `ui/src/components/<área>/` | `npm run test-storybook` en verde y el dueño revisa las historias |
| 4. Backend con TDD | Primero el test que falla, después el código. SQLite real en `tmp_path`, sin mocks del dominio | `src/`, `tests/` | `uv run pytest` en verde; el test falla sin el cambio |
| 5. Integración | Cableado de la UI con la API, E2E de Playwright con el backend simulado y, si cruza servicios, una prueba en `tests/integration` | `ui/src/features/`, `ui/e2e/`, `tests/integration/` | E2E en verde |
| 6. PR | Un tema por PR, commits pequeños, CI en verde, checklist completa y revisión de código | GitHub | El dueño aprueba la fusión |

## Definición de terminado

Cada PR la copia en su descripción y la marca:

- [ ] La spec refleja lo que se construyó.
- [ ] Cada test nuevo falla sin el cambio y pasa con él.
- [ ] `ruff format --check`, `ruff check`, `pyright` y `pytest` en verde.
- [ ] `npm run lint`, `npm run build`, `npm run test-storybook` y `npm run e2e` en verde.
- [ ] Sin secretos, sin rutas absolutas, sin código muerto y sin TODO sin dueño.
- [ ] Docs del área al día (README, `docs/testing.md` y el contrato).
- [ ] UI con el mismo sistema visual (ver abajo).

## Sistema visual de la UI

No se inventan estilos por componente:

- **Tokens:** los de `ui/src/index.css`, como `--primary`, `--border` y `--radius`.
- **Superficies del chat:** tinta `#0d0d0d`, grafito `#6b6b6b` y bordes `#ececec`, como en
  `ui/src/components/agent/agent-styles.ts`.
- **Botones, etiquetas y foco:** `pillButton`, `chip` y `focusRing` de ese mismo archivo. Los
  estados usan los tonos de `chip`: `success`, `warning` y `danger`.
- **Primitivas:** las de `ui/src/components/ui/` (Button, Card, Dialog, Input, Label).
- **Iconos:** `lucide-react`.

Un color nuevo entra solo como token, con contraste AA verificado por axe en Storybook.

## Reglas de código

- **Responsabilidades separadas:**
  - `components/` muestra datos que recibe por props;
  - `features/` cablea el runtime y la API;
  - `lib/` define contratos y clientes.
- **Nombres:** del dominio (compra, traza, decisión), no genéricos (`data`, `info`, `handler2`).
- **Funciones pequeñas con un propósito.** Si una necesita un comentario para explicar *qué* hace,
  se divide. Los comentarios explican *por qué*.
- **Errores:** se tratan donde se pueden resolver. Al usuario llega un mensaje en español sin
  detalles internos.
- **Idiomas:** documentación y textos de UI en español; código, identificadores y mensajes de
  commit en inglés (`feat(ui): ...`, `fix(api): ...`).
