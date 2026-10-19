# Bitácora del monitor

- Primer incremento: cuatro recorridos desde artefactos cerrados; ruta React aislada y exportador sin ejecución del harness. La inspección HNP-01 y sus imports compartidos corresponde al corte histórico del 18 de octubre de 2026.
- MON-02A: corrige confirmación atribuida a operaciones bloqueadas/no iniciadas, separa creación y recuperación y expone budget pass del validador como discrepante del deny backend. 43 tests en ese corte; originales preservados.
- MON-02B: agrega fixture HTTP local fija, integridad, contrato, límites, rechazo de métodos y estados de fuente explícitos. 69 tests distintos acumulados; no se suman repeticiones ni tests históricos HNP.
- Preparación de publicación 19 de octubre de 2026: documentación portable, copia publicable de los ocho tests herméticos de navegación y del preview con API bloqueada. Atributos limitados a rutas del monitor evitan conversión de bytes y mantienen verificable su manifiesto en clones Windows. No se cambia la semántica de los cuatro recorridos ni el runtime comercial.

Los archivos locales de auditoría, paquetes y logs no se incorporan. Se excluyen del PR las notas antiguas de costos HNP-02, inspección remota y el verificador de custodia dependiente de rutas de un equipo; se conservan localmente. Las propuestas pendientes se explican en HANDOFF-YAIR.md, sin habilitar proveedores. Ningún corte cierra PARTIAL ni AUDIT-MERCHANT-CANCEL-01.
