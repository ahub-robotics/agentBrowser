# CS06 Memoria de intentos rechazados

**Fecha:** 2026-10-16 · **Estado:** Propuesta sin implementar · **Ámbito:** resultados de decisiones e intentos de ejecución dentro de una tarea.

Esta propuesta añade memoria acotada de intentos y rechazos, sin reintentar mutaciones. Complementa el [índice](README.md) y preserva [AGENTS.md](../../AGENTS.md). Estos contratos todavía no existen.

## Problema funcional y ejemplo

La política ve acciones ejecutadas recientes, pero no dispone de una representación explícita de decisiones rechazadas por precondiciones. Un objetivo observado puede seguir apareciendo después de que una guarda haya impedido actuar. La siguiente decisión puede repetir una selección que continúa siendo inviable y gastar presupuesto sin aportar progreso.

Ejemplo deducido: una capa sin texto cubre un botón. Jev lo selecciona; el hit test impide el clic; la observación conserva el mismo contenido semántico. El modelo puede escogerlo otra vez porque no recibe el resultado estructurado del rechazo. No es un fallo medido en producción; puede reproducirse con HTML local y proveedor falso.

En [Agent._execute](../../AgentBrowser/agent.py), la decisión se consume antes de actuar. `history` se amplía después de que `Browser.act` retorna y antes de observar el resultado. Esto protege frente a repetir una acción tras una observación obsoleta, pero un rechazo previo no entra en `history`. [model.choose](../../AgentBrowser/model.py) recibe solo las últimas diez entradas de ese historial. `decisions` conserva respuestas aceptadas, sin un resultado explícito por intento. [recover](../../AgentBrowser/workflow.py) borra la decisión y vuelve a observar; no recuerda por qué el candidato fue rechazado.

## Objetivos y límites

Se propone distinguir decisión, intento, efecto confirmado y evidencia posterior. La memoria debe evitar selecciones inviables repetidas únicamente cuando una comprobación actual confirma que la misma precondición sigue incumplida. También debe hacer visible la incertidumbre de una ejecución parcial.

No se pretende aprender preferencias entre tareas, crear una lista negra global, persistir sesiones, adivinar efectos por similitud textual o reintentar operaciones. Tampoco se ampliará `history` con acciones que no ocurrieron. Un error de modelo o helper no demostrará que el elemento sea inutilizable.

| Alternativa | Ventaja | Limitación |
| --- | --- | --- |
| Añadir rechazos a `history` | Reutiliza datos existentes | Falsea acciones ejecutadas y detección de progreso |
| Excluir siempre el último índice | Simple | Los índices cambian; puede excluir otro elemento |
| Lista negra por etiqueta | Fácil de compartir | Confunde elementos y penaliza contextos posteriores |
| Repetir tras cualquier error | Puede ocultar fallos transitorios | Puede duplicar efectos externos |
| Intentos separados y supresión revalidada | Evidencia precisa y contextual | Exige clasificar la frontera de input |

Se elige la última alternativa.

## Contratos propuestos

`decision_outcomes` vincularía cada `decision_id` con un resultado: `withheld_by_policy`, `discarded_stale`, `rejected_precondition`, `failed_before_input`, `terminal_selected`, `executed` o `ambiguous`, más referencia opcional a un intento. La pausa de [CS05](05_incertidumbre_en_la_seleccion.md) no sería una acción ejecutada. Un fallo del helper se clasificaría como `failed_before_input` tanto en la decisión como en su intento; las dos variantes de ejecución completada se agruparían como `executed` en la decisión, conservando el detalle en el intento.

`execution_attempts` sería un registro separado:

```text
ExecutionAttempt:
  attempt_id, decision_id, observation_id
  document_epoch, action_key, context_digest
  operation, phase, outcome, reason_code
  input_started, history_step | null
  started_monotonic, finished_monotonic | null
  resolution_reference | null
```

Los identificadores los genera el código. `action_key` combinaría la referencia completa de [CS04](04_cobertura_y_descubrimiento.md) —sesión, frame, época del documento y nodo—, la operación y, para `SELECT`, la identidad de la opción observada. La opción requeriría una referencia local vigente: un valor repetido o una posición reutilizada no bastan. Antes de habilitar frames, la misma estructura identifica explícitamente el documento principal. No usaría el índice enviado al modelo como identidad duradera. Ni etiquetas similares ni igualdad de URL permitirían asociar nodos diferentes.

| Resultado terminal del intento | Evidencia requerida | Consecuencia |
| --- | --- | --- |
| `failed_precondition` | Guarda fallida antes de cualquier input | Puede originar supresión contextual revalidada |
| `failed_before_input` | Error de helper o preparación, sin input | No suprime el candidato por sí mismo |
| `ambiguous` | Input iniciado o confirmación insuficiente | Suspender nuevas mutaciones; inspeccionar |
| `executed_result_unknown` | Ejecución retornó, observación posterior falló | Mantener `history`; reconciliar mediante lectura |
| `executed_observed` | Ejecución retornó y observación obtenida | Registrar cambio; no afirmar éxito del objetivo |

`prepared` e `input_started` serían fases transitorias, no resultados finales. Un intento tendría exactamente un resultado terminal. Una observación posterior añadiría evidencia de resolución enlazada, sin devolver el intento a `prepared` ni ejecutar de nuevo su acción.

## Orden de ejecución y clasificación

Las decisiones `DONE` y `BLOCKED` siguen su ruta terminal con comprobación de frescura, sin `Browser.act` ni `execution_attempts`. Se registran como `terminal_selected` solo después de validar esa frescura; si falla, quedan descartadas y el grafo recupera. Su selección no acredita el éxito real del objetivo. El procedimiento siguiente corresponde únicamente a operaciones de navegador, incluida la espera; una espera completada no marca `input_started` porque no envía entrada.

1. Validar que exista una decisión utilizable y que el comando la identifique. Una petición inválida no inventaría un intento de navegador.
2. Crear `attempt_id` antes de comprobar precondiciones, llamar al helper o emitir input; consumir la decisión una sola vez.
3. Registrar la fase y comprobar frescura y ejecutabilidad. Si el helper falla antes de input, finalizar como `failed_before_input` y propagar el error.
4. Marcar `input_started` inmediatamente antes de la primera llamada capaz de producir un efecto, incluso el clic que enfoca un campo.
5. Si `act` retorna, conservar el registro de ejecución actual en `history` antes de observar. Finalizar según se obtenga o no la observación.
6. Si falla después de iniciar input y no se confirma ejecución completa, finalizar como `ambiguous`. Nunca reenviar automáticamente la llamada.

La clasificación dependería de fase y resultado estructurado, no del texto de la excepción. [browser.py](../../AgentBrowser/browser.py) necesita informar esa frontera de forma verificable. `SELECT` combina validación y mutación en una evaluación: una interrupción o falta de confirmación seguirá siendo ambigua salvo evidencia explícita de que el rechazo ocurrió antes de modificar el control. No basta con renombrar su excepción como `StalePage`.

La finalización se realizaría también al propagar excepciones, sin ocultarlas. Como la memoria es volátil, una terminación abrupta del proceso puede impedir registrar el final; no se promete recuperación durable.

## Supresión temporal de candidatos

La supresión seguiría a la elegibilidad de CS03 y precedería al contexto y presupuesto de CS02/CS01. Solo admitiría `failed_precondition` con razón estructurada y comprobación repetible de lectura. Se localizaría el mismo nodo y operación en la observación actual. Solo si época, identidad y contexto coinciden y la precondición sigue fallando, el candidato quedaría temporalmente fuera de la lista.

El contexto incluiría la semántica pertinente del objetivo, formulario o contenedor y las propiedades que motivaron el rechazo. La época del documento distinguiría navegación y sustitución de documento; una época contextual cambiaría únicamente ante diferencias relevantes verificadas. Reobservar con otra marca de tiempo no crea contexto nuevo. La igualdad de `fingerprint` tampoco sustituye una comprobación de oclusión, porque una capa sin texto puede no modificarlo.

Se propone una ventana máxima de tres rondas de decisión o cinco segundos monotónicos, lo que ocurra antes; son límites operativos configurables, no evidencia de seguridad. El rechazo repetido no renovaría indefinidamente su ventana. Al expirar se exigiría revalidación antes de readmitir: el vencimiento no autoriza un clic. Cambios relevantes invalidarían la supresión, pero seguirían aplicándose todas las guardas ordinarias.

Si desaparece o se reemplaza el nodo, no se transferiría la supresión al nuevo índice. Si la comprobación falla, se informaría del error sin asumir ejecutabilidad. La ausencia de objetivos de entrada no causaría bloqueo mientras existan operaciones de descubrimiento admitidas, como scroll observado o `WAIT` dentro del presupuesto; Jev seguiría eligiendo entre ellas y los terminales. Si solo quedan terminales, el grafo permitiría como máximo una nueva observación por contexto antes de publicar ausencia de alternativas. Esa observación no restablecería presupuestos ni reemplazaría la decisión de Jev sobre `DONE`/`BLOCKED`; al agotar un presupuesto, el controlador detendría la tarea con esa causa explícita. Un timestamp nuevo no reiniciaría el permiso ni se fabricaría una alternativa.

## Integración y memoria enviada al modelo

[agent.py](../../AgentBrowser/agent.py) gestionaría identificadores y registros. [browser.py](../../AgentBrowser/browser.py) proporcionaría resultados tipados. [workflow.py](../../AgentBrowser/workflow.py) decidiría recuperación, observación y suspensión; ninguna rama de memoria llamaría de nuevo a `execute` con el mismo intento.

[model.py](../../AgentBrowser/model.py) recibiría un resumen acotado de hasta cinco rechazos todavía pertinentes, dentro de la misma solicitud TypeSafe y del presupuesto total de [CS01](01_presupuesto_y_recorte.md). Cada entrada incluiría operación, razón normalizada y una referencia opaca al evento, explícitamente no seleccionable. Solo añadiría un `current_target` si el objetivo vuelve a estar admitido en la solicitud actual; sería `null` para candidatos excluidos. Así, retirar un candidato no elimina toda evidencia de su rechazo ni introduce objetivos fantasma. Los contadores de exclusión complementan el resumen. No se enviaría un índice antiguo ni una recomendación de reintentar. Esta ampliación requiere comprobar que el contrato del proveedor admite el campo adicional.

Un resultado ambiguo o ejecutado sin observación produciría `needs_review` con `review_kind=execution`, publicación y fin del recorrido automático. Compartiría la presentación de pausa con CS05, pero no su continuación: la decisión ya está consumida y `act` sobre ella se rechazaría. Habilitaría únicamente lecturas para reconciliar y un resultado de resolución enlazado al intento; una observación nueva no demuestra que el efecto remoto no ocurrió. Continuar exigiría evidencia independiente o decisión explícita del operador para resolver la pausa y obtener una decisión nueva dentro del presupuesto restante, nunca reejecutar el intento. La operación de resolución no enviaría entrada ni aprobaría por sí sola un nuevo objetivo.

La caché de texto seguiría comparando la entrada completa de `field_context`. Añadir rechazos a ese contexto obligaría a incluirlos también en su identidad; no se permitiría reutilizar texto comparando solamente campo o objetivo. La propuesta inicial mantiene el contexto del helper actual.

## Retención y privacidad

Los registros existirían solo en la instancia, con máximo propuesto de 120 intentos; al alcanzarlo se detendría antes de crear otro. La supresión tendría además su TTL corto. Cerrar o reiniciar la tarea eliminaría esta memoria, sin trasladarla a otra pestaña.

Se almacenarían códigos y referencias, evitando copiar textos introducidos, mensajes de excepción o páginas completas. El inspector y la CLI mostrarían resúmenes; la exportación detallada sería explícita y documentaría sus datos. Esto no anonimiza las trazas existentes ni introduce almacenamiento remoto.

## Aceptación, métricas y dependencias

Pruebas offline con reloj falso, proveedores sustituidos y navegador controlado comprobarían: registro anterior al primer input; resultado terminal único; rechazo sin ampliar `history`; error tras `mousePressed` como ambiguo sin segundo evento reenviado; observación fallida preservando ejecución; cero reintentos por memoria; sustitución de nodo e índices reordenados sin supresión cruzada; timestamp nuevo sin contexto nuevo; TTL sin readmisión automática; y caché invalidada ante cualquier cambio de entrada del helper. También verificarían el límite de memoria y que errores de proveedor no bloqueen candidatos.

Las métricas separarían intentos rechazados, decisiones repetidas, supresiones confirmadas, expiraciones, ambigüedades y resultados desconocidos. `history`, decisiones y peticiones HTTP seguirían siendo contadores distintos. El beneficio se mediría como menor repetición de precondiciones incumplidas, acompañado de comprobación de que no se pierden candidatos válidos.

La identificación contextual depende de [CS02](02_contexto_semantico.md), la razón ejecutable de [CS03](03_ejecutabilidad_y_oclusiones.md) y la recuperación ante ausencia de candidatos de [CS04](04_cobertura_y_descubrimiento.md). Quedan abiertos el protocolo de reconciliación, los valores operativos definitivos del TTL y la representación del resumen compatible con TypeSafe.
