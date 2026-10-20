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

Los componentes nuevos (`Trusted State`, `Policy Gateway`, OPA, `OfferFreshnessGuard` y
`Decision Evidence`) usan el mismo azul del agente porque pertenecen a su frontera. La aprobación
AP2 conserva el ámbar usado para el punto de autorización. B0 y B2 se distinguen mediante el texto
de tipo (`[Componente B0]` o `[Componente B2]`), no mediante rellenos o una paleta adicional.
