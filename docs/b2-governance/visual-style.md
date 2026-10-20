# Convención visual de los diagramas B2

Los diagramas B2 reutilizan el lenguaje definido por `scripts/tools/build_presentation_diagrams.py` y por
las vistas C4, secuencia y estados bajo `docs/diagrams/`. No se introduce una segunda identidad
visual para la gobernanza.

## Tokens

| Uso | Valor |
|---|---|
| Tipografía | Arial |
| Texto principal | `#333333` |
| Texto secundario | `#686868` o `#555555` |
| Relaciones | `#767676` |
| Fondo | `#FFFFFF` |
| Fondo auxiliar excepcional | `#F7F7F7` |
| Agente y componentes internos | `#336C96` |
| Comprador | `#3C7549` |
| Comercio y aprobación | `#A96628` |
| Sistema externo/modelo | `#A14E5C` |
| Fallo | `#A84E4E` |
| Estado incierto | `#756392` |

## Reglas

- cajas blancas, sin degradados ni sombras;
- bordes de componente de 2–2,5 px y radio corto;
- fronteras de sistema punteadas en azul;
- relaciones grises, preferiblemente punteadas, con punta de bloque;
- títulos de componentes en negrita y descripciones breves en gris;
- el color comunica responsabilidad o estado, no decoración;
- evitar badges, pills, fondos azules extensos e iconos que no existan en las vistas C4 actuales;
- evitar que una relación atraviese nombres, descripciones o etiquetas;
- conservar espacio en blanco amplio y densidad comparable con los diagramas de presentación.

## Aplicación en B2

Los controles B2 (gate D1, frescura de la oferta, continuación y recuperación gobernadas, ledgers
de evidencia) usan el mismo azul del agente porque pertenecen a su frontera, y se dibujan dentro
del componente B0 donde actúan. La aprobación AP2 conserva el ámbar usado para el punto de
autorización. B0 y B2 se distinguen mediante el texto de tipo (`[Componente B0]` o
`[B2 · hecho]`), no mediante rellenos o una paleta adicional.

La propuesta original P1–P5 (`Trusted State`, `Policy Gateway`, OPA, `OfferFreshnessGuard`,
`Decision Evidence`) quedó como histórica en
[`docs/diagrams/historico/b2-architecture-p1-p5.svg`](../diagrams/historico/b2-architecture-p1-p5.svg).

## Estado de implementación

Cuando un diagrama mezcla trabajo hecho y pendiente, el borde lo indica y el texto de tipo lo
repite, para que se lea igual en gris o impreso:

| Estado | Borde | Texto de tipo |
|---|---|---|
| Hecho (en `org/main` y conectado al flujo) | continuo, color de su responsabilidad | `[B2 · hecho]` |
| Planificado (piezas en `org/main` que el flujo todavía no usa) | discontinuo largo, mismo color | `[B2 · planificado]` |
| Futuro (sin implementar) | punteado corto, gris `#8C8C8C`, nombre en gris | `[B2 · futuro]` |

Todas las tarjetas y mensajes con estado usan ese texto de tipo, también en la secuencia y en el
delta. Un elemento de la línea base que aparece en un diagrama B2 lleva `[B0]` o
`[Componente B0]`; los del experimento HNP usan el prefijo `HNP` (`[HNP · hecho]`). La leyenda
muestra solo los estados que aparecen en el diagrama.

Las fronteras de sistema siguen siendo punteadas en azul o gris; no llevan estado. En la secuencia
gobernada, los mensajes que agrega B2 van en azul del agente y los rechazos sin efecto comercial en
el rojo de fallo. Las líneas de vida quedan detrás de las tarjetas y de las etiquetas.

## Resultados

La figura de resultados del piloto (`pilot-results`) se genera desde el `.summary.json` congelado
y nunca lo modifica. Cada repetición es un cuadro: lleno si cumple el oráculo, vacío con borde gris
`#BBBBBB` si no. B0' usa el gris `#A3A3A3` y B2 el azul del agente; los conteos van siempre como
texto (`n/5`) junto a los cuadros, así que el color no es la única pista.
