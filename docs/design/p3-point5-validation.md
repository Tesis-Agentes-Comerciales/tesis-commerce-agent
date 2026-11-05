# P3 punto 5: validación del protocolo

Incremento de arnés sobre #160 (`49c4b1c`). El [plan de pruebas](p3-ux03-validation.md)
define PV-01–10. El incremento separa los validadores offline de los controles live.
Los casos de UX03 y la comprensión de UX05 quedan pendientes.

`main` observado en `95ecf71685c6e551aaac3ebcdb3d6fe4224680d4` incluye #171. La
simulación con `git merge-tree` encontró conflictos de #157 y #160 en `nightly.yml` y
`live_merchant_control.py`. #158 conserva el checkpoint P2 `ad86bdd`, anterior a
la corrección de nightly `5900b02` de #157. El incremento usa el arnés fijado de #160
para reproducir el contrato sin llevar esos conflictos a main. No integra #174.

Para integrar después, revisar #157 → #158 → #160 → esta unidad mediante candidatos sin
reescritura. Conservar los perfiles y gates de main, el gate P2 y los perfiles manuales P3.
Unir las operaciones permitidas de los workers por nombre y conservar el aislamiento de
PostgreSQL. Comparar árboles, validar el CI y repetir los perfiles live afectados sobre el
candidato exacto. El commit offline puede portarse por separado a main, pues solo necesita
la matriz y el oráculo P0 ya presentes. La implementación condicional de Yair se integra
por separado y requiere su propia revisión.

Las cifras ACP se vinculan a sesión, run, actor, oferta, revisión y contexto. Los intentos
sin respuesta requieren reconciliación antes de contar sesiones. La puerta exige
equivalencia de clones, cancelación observada, ausencia de efectos en E y un sello antes
del primer resultado Agent. El scorer recibe una propuesta ya emitida. El doble offline
puede dar una respuesta incorrecta para probar que el scorer la conserva.

PV-05–08 reutilizan el fixture, clientes, snapshots y reconciliación de #160. Se agrega
evidencia de la composición de dos alternativas y de un E intacto. PV-10 usa la ruta HP
de producción con autenticación sintética real y un router fijo para preparar una compra
simple. El perfil live usa solo servicios de laboratorio y no habilita pagos.
