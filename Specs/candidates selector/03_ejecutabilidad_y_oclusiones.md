# CS03 Ejecutabilidad de candidatos y oclusiones

**Fecha:** 2026-10-16  
**Estado:** Propuesta sin implementar  
**Ámbito:** Selección de candidatos antes de inferencia y validación antes de entrada.

**Índice:** [Propuestas del selector](README.md)

## Problema funcional y evidencia

El modelo puede elegir un control que aparece en la observación estructurada, pero que el navegador no puede accionar porque otro elemento cubre su centro. El rechazo llega después de pagar la decisión y, para `TYPE_TEXT`, posiblemente también la generación de texto.

Ejemplo deducido: una pantalla conserva un botón «Continuar» detrás de un panel de carga sin texto. El botón conserva tamaño, etiqueta y visibilidad CSS; Jev lo recibe, puede seleccionarlo y el ejecutor lo rechaza. Una nueva observación puede volver a ofrecer el mismo botón. Es una consecuencia posible del código, no un incidente medido en una web concreta.

La extracción filtra visibilidad, deshabilitación y centro dentro del viewport, pero no cobertura: [snapshot.js, líneas 9–11 y 56–79](../../AgentBrowser/snapshot.js). El hit test `e.contains(document.elementFromPoint(x,y))` se realiza al ejecutar: [browser.py, líneas 144–164](../../AgentBrowser/browser.py). La regresión local de overlay demuestra el rechazo antes de introducir eventos, aunque la frescura semántica siga siendo válida: [check_guards.py, líneas 54–68](../../scripts/check_guards.py).

## Comportamiento actual y esperado

| Caso | Actual | Esperado propuesto |
| --- | --- | --- |
| Centro cubierto al observar | Se ofrece el candidato | Se excluyen sus operaciones de entrada y se registra el motivo |
| Icono decorativo dentro del botón | El descendiente puede recibir el hit test | Se conserva la aceptación por pertenencia al objetivo |
| Overlay con `pointer-events:none` | El ejecutor puede alcanzar el control | El filtro conserva el control si el hit test lo alcanza |
| Cobertura que aparece tras elegir | El ejecutor rechaza | Se mantiene ese rechazo, sin entrada ni replay |
| Cobertura que desaparece | El candidato puede seguir presente | Reaparece en la siguiente observación; no hay veto permanente |
| Centro cubierto, esquina libre | El ejecutor rechaza | La primera fase también rechaza; multipunto queda separado |

«Ejecutable» significará que se cumplen las precondiciones comprobables de la operación en un instante. No significará que la acción es correcta para el objetivo, que el sitio responderá ni que el efecto de negocio está garantizado.

## Objetivos y no alcance

Reducir decisiones sobre objetivos inaccesibles, alinear extracción y ejecución, explicar exclusiones y preservar la última comprobación inmediatamente antes de entrada. El filtro debe ser determinista, independiente del sitio y de solo lectura.

Quedan fuera resolver captchas, cerrar overlays automáticamente, mover elementos, forzar clics mediante JavaScript, desplazar la página desde `observe`, aprobar acciones irreversibles o incorporar capturas al modelo. Tampoco se modifica la política de éxito: `DONE` sigue necesitando verificación independiente.

## Alternativas

| Alternativa | Ventaja | Coste o limitación |
| --- | --- | --- |
| Mantener solo validación final | Cambio mínimo | Conserva decisiones desperdiciadas y repetición de candidatos cubiertos |
| Predecir cobertura desde CSS/z-index | Evita hit tests por candidato | No representa fielmente composición, clipping ni recepción de eventos |
| Hit test del centro antes y después | Coincide con la estrategia ejecutora actual | Rechaza controles utilizables por otra zona; añade lecturas de layout |
| Buscar varios puntos desde el inicio | Mayor cobertura de controles parciales | Exige cambiar ejecutor, geometría y contrato de frescura conjuntamente |

Se propone la tercera alternativa como primera entrega. La cuarta requiere una fase independiente y evidencia adicional; no se habilitará un candidato por una esquina para después clicar su centro.

## Solución técnica

### Fase 1: contrato común con centro exclusivo

Definir un evaluador de solo lectura compartido por extracción y ejecutor, propiedad del código. Recibe nodo y operación admitida; devuelve elegibilidad, motivo normalizado y punto interno. El modelo no recibe coordenadas, selectores ni expresiones.

Orden propuesto:

1. Resolver el nodo real y comprobar conexión, visibilidad, `inert`, deshabilitación y requisitos de edición u opción.
2. Leer un rectángulo actual y obtener su centro; rechazar dimensiones nulas o centro fuera del viewport.
3. Consultar `elementFromPoint` y exigir la misma pertenencia que el ejecutor: objetivo o descendiente aceptado.
4. Emitir únicamente operaciones elegibles. Las opciones de un mismo `select` comparten el hit test del control, pero conservan su validación individual.
5. Continuar con supresión temporal de [CS06](06_memoria_de_intentos_rechazados.md), contexto de [CS02](02_contexto_semantico.md) y presupuesto/agrupación de [CS01](01_presupuesto_y_recorte.md), conservando estadísticas de exclusión. Tras Jev y la validación estructural se aplica el gate de [CS05](05_incertidumbre_en_la_seleccion.md), antes de ejecutar.

Un esquema interno orientativo, todavía inexistente, sería:

```json
{
  "node": 42,
  "operation": "CLICK",
  "eligible": false,
  "reason": "covered",
  "point_policy": "center_v1"
}
```

Los motivos incluirán `covered`, `outside_viewport`, `hidden`, `disabled`, `readonly`, `detached` y `option_unavailable`. No se guardará texto del overlay por defecto. Los datos detallados quedarán en diagnósticos locales acotados; las cabezas TypeSafe contendrán exclusivamente objetivos admitidos. Una exclusión no se añadirá a `history` como acción ejecutada.

La nueva observación deberá reflejar cambios de disponibilidad en su representación semántica. Las coordenadas no entrarán en `marker` solo por introducir este filtro. `fingerprint`, que actualmente incluye rectángulos, conservará su finalidad documentada; un cambio de disponibilidad o de huella no se interpretará automáticamente como progreso de negocio.

### Fase 2: validación final y recuperación

Justo antes de entrada, volver a resolver identidad, frescura y el centro actual, y repetir el mismo evaluador. No se reutilizará una coordenada vieja. Si falla antes de la primera mutación, se propagará el rechazo seguro; LangGraph seguirá siendo propietario de recuperar y observar de nuevo en los modos correspondientes.

La decisión continuará consumiéndose antes del auxiliar y de la mutación, como en [agent.py, líneas 97–154](../../AgentBrowser/agent.py). Una vez iniciados eventos CDP o la asignación de un `select`, un error ambiguo no autorizará reintentar. El registro de una ejecución confirmada seguirá antecediendo a su observación. [CS06](06_memoria_de_intentos_rechazados.md) podrá utilizar rechazos seguros sin convertirlos en replay ni veto indefinido.

Los overlays transitorios no crearán listas negras. Si no quedan objetivos de entrada, permanecen las operaciones globales realmente disponibles, incluido `WAIT`; Jev sigue eligiendo operación y cabeza correspondiente en una sola solicitud. No se impondrá una espera ni se cerrará una ventana desde el observador. La caché del auxiliar conservará su condición actual: igualdad de toda su entrada.

### Fase futura: puntos alternativos

Una ampliación multipunto deberá definir una regla determinista acotada, identificar la zona elegible y volver a validar el punto que efectivamente se enviará a CDP. Debe compartir el algoritmo entre selección y ejecución y descartar la decisión si la política ya no puede resolver una zona admitida. Se requieren pruebas específicas de clipping, transformaciones y descendientes interactivos; no forma parte de la aceptación inicial.

## Módulos y riesgos

Cambiarían [snapshot.js](../../AgentBrowser/snapshot.js), [browser.py](../../AgentBrowser/browser.py) y las estructuras diagnósticas de [agent.py](../../AgentBrowser/agent.py). [model.py](../../AgentBrowser/model.py) seguiría construyendo una sola solicitud y consumiendo únicamente el target seleccionado. El grafo de [workflow.py](../../AgentBrowser/workflow.py) no recibirá un bucle alternativo de ejecución.

Los principales riesgos son falsos negativos con centros cubiertos, diferencias entre hit test y recepción real de eventos, coste de layout, descendientes que tienen acción propia y carreras posteriores a la comprobación. La primera fase conserva la semántica de pertenencia vigente; no afirma resolver todos los widgets. La futura cobertura de shadow DOM e iframes de [CS04](04_cobertura_y_descubrimiento.md) deberá extender también el hit test, no solo la enumeración.

## Aceptación y pruebas locales

La aceptación propuesta exige:

- En fixtures estables, todos los candidatos ofrecidos satisfacen el mismo evaluador final sin mutaciones intermedias.
- Ningún objetivo totalmente cubierto aparece en las cabezas TypeSafe simuladas; un icono descendiente y un overlay con `pointer-events:none` no causan exclusiones indebidas.
- Un overlay introducido después de `predict` produce cero clics, cero escritura y ningún retry; su desaparición permite redescubrimiento.
- La observación no modifica foco, scroll, valores, DOM de aplicación ni contadores de eventos de entrada.
- Una prueba de fallo parcial tras `mousePressed` o durante `select` confirma ausencia de segunda mutación.

Se ampliarán fixtures de `check_guards.py` y pruebas de contratos con proveedores sustituidos por dobles. Se medirán 30 observaciones de calentamiento y 100 registradas sobre 250 candidatos, en el mismo Chrome/equipo: objetivo provisional de incremento p95 no superior a 10 ms, revisable mediante evidencia antes de aceptar la implementación. Las pruebas no llamarán a APIs de pago.

El filtro se aplica antes del recorte, por lo que también se medirá una fixture con los 5000 controles inspeccionables propuestos en CS01 y mezcla de controles cubiertos y selectores. Se registrará el tiempo conjunto de extracción, hit tests y empaquetado, además del coste aislado del filtro. Antes de habilitar esa capacidad se fijará un presupuesto de tiempo medido en el entorno de referencia; si se interrumpe el recorrido por alcanzarlo, la observación declarará cobertura incompleta. El caso de 250 elementos no acredita por sí solo el comportamiento bajo la carga máxima.

## Métricas, dependencias y decisiones abiertas

Registrar número enumerado, elegible y excluido por motivo, duración del filtro, rechazos posteriores a inferencia y llamadas lógicas desperdiciadas por cobertura. No mezclar exclusiones, decisiones y acciones; las métricas HTTP reales necesitan instrumentación propia.

Dependencias: orden de recorte de CS01, contexto de [CS02](02_contexto_semantico.md), identidad futura de CS04 y memoria de CS06. Quedan abiertos el lugar exacto del evaluador compartido, el límite de diagnósticos y la política de descendientes interactivos. Deben resolverse antes del código; ningún cambio propuesto está implementado en este documento.
