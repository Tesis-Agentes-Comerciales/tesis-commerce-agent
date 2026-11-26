# Herramientas puntuales

Scripts que se ejecutan a mano y rara vez. No forman parte de la demo, del CI ni de la suite de tests.

| Script | Qué hace |
|---|---|
| `build_presentation_diagrams.py` | Genera los `.drawio` editables de los diagramas vigentes de [`docs/diagrams/`](../../docs/diagrams/README.md): contexto C4, componentes, secuencias de compra y máquina de estados. Sobrescribe los archivos. Los `.png` y `.svg` se exportan desde draw.io. |

```sh
uv run python scripts/tools/build_presentation_diagrams.py
```

El generador de los diagramas de gobernanza de octubre, `build_governance_diagrams.py`, salió con
esos diagramas al [archivo](../../docs/archive/diagrams/README.md) y sigue en la etiqueta
`pilotos-octubre`.
