# Diseño final HP + HNP

Vista estable en Notion: [19 Diseño final HP y HNP — roadmap y protocolos](https://app.notion.com/p/3f965dca59e781f393ebd77838dba95b).

Lectura completa: [roadmap.md](roadmap.md). Copia independiente: [roadmap.html](roadmap.html).
Fuente nativa: [plan.mdx](plan.mdx). Registro: [decisions.json](decisions.json).
Investigación: [protocol-research.md](../protocol-research.md).

Alcance confirmado por Yair el 2026-10-11: Human Present y Human Not Present. Incluye
46 decisiones del núcleo y cuatro B2B condicionadas. El diseño continúa abierto: las
recomendaciones y spikes no son capacidades implementadas.

Desde la raíz del repositorio, abrir la vista nativa con:

```sh
npx -y @agent-native/core@latest plan local serve --dir docs/b2-governance/system-design-v1 --kind plan
```

El servidor debe seguir ejecutándose mientras se consulta. La vista Markdown y el HTML
se pueden consultar sin servidor. No versionar `.plan-url` ni `.plan-state.json`.
La copia local del workspace en `plans/system-design-v1` se conserva para la sesión de lectura.

Validación del 2026-10-14: agente 203 passed / 3 skipped; Ruff format/check y Pyright
correctos; DAG acíclico de 50 decisiones y enlaces locales válidos. Las tres E2E
omitidas requieren servicios/PostgreSQL; no acreditan nueva ejecución integrada.
