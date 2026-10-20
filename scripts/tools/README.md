# Herramientas puntuales

Scripts que se ejecutan a mano y rara vez. No forman parte de la demo, del CI ni de la suite de tests.

| Script | Qué hace |
|---|---|
| `build_presentation_diagrams.py` | Genera los `.drawio` editables de las vistas B0 de detalle en `docs/diagrams/` (contexto C4, componentes, secuencias de compra y máquina de estados) con la paleta de [la convención visual](../../docs/b2-governance/visual-style.md). Sobrescribe los archivos. Los `.png` y `.svg` se exportan desde draw.io. |
| `build_governance_diagrams.py` | Genera los diagramas de la sustentación (B0, B2-HP, delta, secuencia gobernada y su versión en una diapositiva, mapa de políticas, HNP, diseño y resultados del piloto) reutilizando las primitivas del anterior. `pilot-results` lee el `.summary.json` congelado del piloto B2 y su `.jsonl`, y no los modifica; `policy-map` y `purchase-sequence-slide` toman de ese resumen los números del piloto. Con `--export` llama al CLI de draw.io y escribe `.svg` y `.png` de 1920 px de ancho. Busca el CLI en `DRAWIO`, en el `PATH` (`drawio`) o en `/Applications/draw.io.app`. |

```sh
uv run python scripts/tools/build_presentation_diagrams.py
uv run python scripts/tools/build_governance_diagrams.py --export          # los nueve
uv run python scripts/tools/build_governance_diagrams.py --export pilot-design
```
