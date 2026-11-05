# Regla de agregación de EXP-06 (fijada antes de calificar el lote)

Fijada el 2026-11-05T19:41:13Z, antes de que los calificadores lean los 15 episodios del lote. El prerregistro `exp06-hp.json` (bd813f25…) no la fija. La decide Claude, con la delegación de Yair del 5-nov.

1. **Unidad de calificación:** se califica cada criterio de cada episodio por separado, con `pass`, `fail` o `unknown` y una cita.
2. **Veredicto del criterio en un episodio:**
   - si los dos calificadores coinciden, vale ese veredicto;
   - si discrepan, decide el adjudicador.
3. **Episodio aprobado:** sus tres criterios quedan en `pass`.
4. **Reporte por caso y criterio:** k/5 en el lote y k/1 en el smoke, sin colapsar por mayoría.
5. **Caso aprobado:** solo si sus 5 episodios del lote están aprobados (5/5). También se reporta el número de episodios aprobados.
6. **`unknown`:** no cuenta como `pass` y se reporta aparte.
7. **Origen de las calificaciones:** todas son de agentes (`grader_kind=agent`) y provisionales. No reemplazan la revisión humana doble del prerregistro.

8. **Diferencia de instrucciones entre el smoke y el lote.** Los dos calificadores del lote recibieron una instrucción que los del smoke no tuvieron. Se les dio la lectura del adjudicador del smoke: un `clarify` cumple un criterio «explicit» solo si pregunta cada dimensión por separado, y los ejemplos que fijan una sola opción no cuentan como pregunta. También se les pidió revisar en UX12 si la respuesta da a entender que el agente puede confirmar la entrega. Por esta diferencia, el lote y el smoke no son estrictamente comparables en UX11 c1 ni en UX12 c2.
