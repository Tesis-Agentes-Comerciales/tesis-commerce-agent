# Herramientas puntuales

Scripts que se ejecutan a mano y rara vez. No forman parte de la demo, del CI ni de la suite de tests.

| Script | Qué hace |
|---|---|
| `build_presentation_diagrams.py` | Genera los `.drawio` editables de `docs/diagrams/` (C4, secuencias de compra y máquina de estados) con la paleta de [la convención visual](../../docs/b2-governance/visual-style.md). Sobrescribe los archivos: `agent-architecture-c4.drawio` se retocó después a mano en draw.io, así que regenerarlo pierde esos cambios. Los `.png` y `.svg` se exportan desde draw.io. |

```sh
uv run python scripts/tools/build_presentation_diagrams.py
```
