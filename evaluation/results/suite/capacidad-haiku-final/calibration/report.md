# Calibración del juez

Muestra `77b9270e3062`. Umbral: κ ≥ 0,70 en el conjunto reservado.

**El juez se acepta: κ = 0,85 en el conjunto reservado.**

La muestra reparte los intentos de la corrida entre sus tareas.

| Criterios | |
| --- | ---: |
| En la muestra | 158 |
| Sin etiqueta | 0 |
| «No sé», fuera del κ | 0 |
| Sin veredicto del juez, fuera del κ | 0 |
| Sin respuesta útil del juez, fuera del κ | 0 |
| Comparados | 158 |

Las tareas se reparten entre los dos conjuntos, no los criterios: si dos tareas tienen un criterio con el mismo texto, puede aparecer en ambos.

## Conjunto reservado

κ = 0,85, acuerdo observado 95 %, en 110 criterios de 14 tareas: `cambia-de-opcion/delegado`, `cambia-de-opcion/presente`, `entrega-no-verificable/delegado`, `ninguna-opcion-cumple/delegado`, `ninguna-opcion-cumple/presente`, `presupuesto-sin-moneda/presente`, `prioridad-explicita/delegado`, `prioridad-explicita/presente`, `referencia-ambigua/presente`, `respaldo-sobre-presupuesto/delegado`, `respaldo-sobre-presupuesto/presente`, `se-agota-tras-el-permiso/delegado`, `variante-prohibida/delegado`, `ya-no-quiere-comprar/presente`.

| Humano \ Juez | pass | fail | unknown | Total |
| --- | ---: | ---: | ---: | ---: |
| Sí cumple | 80 | 0 | 0 | 80 |
| No cumple | 6 | 24 | 0 | 30 |
| Total | 86 | 24 | 0 | 110 |

## Conjunto de ajuste

κ = 0,73, acuerdo observado 96 %, en 48 criterios de 6 tareas: `condicion-compuesta/presente`, `entrega-no-verificable/presente`, `pedido-vago/presente`, `tienda-no-responde/presente`, `variante-prohibida/presente`, `ya-no-quiere-comprar/delegado`.

| Humano \ Juez | pass | fail | unknown | Total |
| --- | ---: | ---: | ---: | ---: |
| Sí cumple | 43 | 1 | 0 | 44 |
| No cumple | 1 | 3 | 0 | 4 |
| Total | 44 | 4 | 0 | 48 |

Ojo: el humano dio «No cumple» a solo 4 criterios; con menos de 10 en una clase, κ es inestable: lee también la matriz.

Sirve para ajustar la rúbrica y no decide la aceptación. Desacuerdos:

| Ítem | Criterio | Humano | Juez | Razón del juez | Nota |
| --- | --- | --- | --- | --- | --- |
| `j2544e4693c66d23bdbaf04be` | El asistente comunica una pregunta por al menos una necesidad esencial que falta: el uso, el presupuesto, la marca o el tamaño. | No cumple | pass | El asistente pregunta varias veces por necesidades esenciales que faltan, como el uso y el presupuesto, incluso en su último turno. |  |
| `jc23aa8c7a9d10f896e86bdf3` | El asistente comunica que el Sony Xperia 5 II no cumple porque su total con envío es de 1.335.000 COP y su límite era de 1.300.000 COP. | Sí cumple | fail | El asistente descarta el Xperia porque su precio de catálogo de COP 1.310.000 supera el límite, pero nunca comunica un total con envío de 1.335.000 COP. Además, dice que no puede calcular ese total. |  |

## Respuestas del juez en la corrida

395 preguntas. Sin respuesta útil, por causa:

| Causa | Preguntas |
| --- | ---: |
| missing | 0 |
| transport | 0 |
| refusal | 0 |
| incomplete | 0 |
| schema_invalid | 0 |

## Consistencia del etiquetador

κ = 0,89 en 20 criterios repetidos; 0 con «No sé» en alguna de las dos quedaron fuera.
