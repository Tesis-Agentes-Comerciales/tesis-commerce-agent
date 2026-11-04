# UI-01: recorridos de navegador históricos

Alcance autorizado: diagnosticar los cinco fallos de 41762122621 y 41653872780,
preservar esos resultados y corregir el recorrido o el producto según evidencia.
No ejecuta UX03/UX05 ni modelos; solo las compras sintéticas ya existentes.

La reproducción inicial mantiene las aserciones originales y añade trazas sanitizadas,
capturas, vídeo y estado de los controles. La corrección se contrasta con
`hnp-chat-delegate.md` (modo Delegar en chat, lista en Mis delegaciones) y las historias
de la actividad de compra. Se conservan consentimiento, presupuesto, vigencia,
revocación, D1/D2/D3, recibos y efectos; un defecto descubierto se registra como fallo.

La rama del PR parte de main. Una rama temporal de validación combina esta corrección
con P2 5900b0277f6c7d46c91fddb5923c1d145ecb69d3, sin modificar P2, para ejecutar
los 95 casos y su puerta de evidencia. El módulo afectado se reproduce primero con
`-vv --durations=20`. Merchant y PSP conservan los SHAs históricos aprobados.

Las trazas publicadas omiten contraseñas y credenciales de sesión; conservan estados
comerciales y eventos UI. Los archivos originales de resultados no se modifican.
