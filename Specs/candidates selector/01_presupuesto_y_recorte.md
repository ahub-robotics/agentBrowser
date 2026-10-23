# CS01 Presupuesto y recorte de candidatos

**Fecha:** 2026-10-16  
**Actualización:** 2026-10-19 · Reconsulta de candidatos omitidos  
**Estado:** Implementada para el inventario del lector actual; ampliaciones de CS02–CS06 pendientes  
**Área:** Observación y preparación de la solicitud Jev  
**Índice:** [Propuestas del selector](README.md)

## Problema y evidencia de partida (16-10-2026)

Esta sección describe el comportamiento anterior al cambio. La implementación y sus límites actuales se detallan al final del documento.

Un objetivo como «abre el resultado identificado en la página» puede requerir un botón situado después de un selector nativo con muchas opciones. Si ese selector aporta 240 alternativas y varios campos anteriores generan dos acciones cada uno, el botón podría quedar fuera de los candidatos enviados. Es un caso deducido de la implementación, no un fallo medido ni una afirmación sobre la precisión actual de Jev.

[snapshot.js](../../AgentBrowser/snapshot.js#L55) recorre controles en orden DOM. Expande cada opción habilitada y no seleccionada de un `select` en una acción; un campo editable genera `fill` y `click`. Después calcula guardas y `marker`, aplica `actions.splice(250)` y añade hasta tres controles globales: desplazamiento arriba, abajo y espera. Por tanto, 250 limita acciones previas al agrupamiento, no nodos únicos, opciones por selector, bytes ni acciones finales.

[action_space](../../AgentBrowser/model.py#L48) agrupa los nodos después de ese recorte. Lo eliminado ya no puede recuperarse allí. `omitted_actions` aparece en la observación, pero [choose](../../AgentBrowser/model.py#L107) no lo incluye en la solicitud. Jev recibe una lista parcial sin una declaración explícita de cobertura. Además, etiquetas, valores y opciones se repiten entre `elements` y las cabezas de objetivos, aumentando el tamaño real del cuerpo.

## Comportamiento esperado y alcance

La propuesta debe limitar recursos, hacer interpretable la cobertura parcial y permitir consultar los candidatos que quedaron fuera. Si ninguna acción ofrecida permite avanzar hacia el objetivo y quedan candidatos pendientes, Jev podrá pedir otra ventana y continuar hasta encontrar uno adecuado, agotar el inventario consultable o alcanzar un límite explícito. Un nodo con muchas opciones no debe consumir por sí solo la representación de otros controles. Todo objetivo ofrecido debe corresponder a una acción observada, y ninguna solicitud puede superar sus límites configurados.

Se conserva un único objetivo natural, Jev decide operación y cabezas en una solicitud lógica por ventana y solo se consume la cabeza seleccionada. LangGraph conserva el enrutamiento. No se añaden planes por sitio, valores de negocio, selectores generados, capturas para modelos ni otro modelo de selección. Consultar otra ventana sí requiere una nueva decisión Jev, contabilizada como tal. Los errores no reintentan mutaciones; el registro previo a observar y la igualdad completa de entrada de la caché textual permanecen vigentes.

Se incluye el paginado explícito del inventario observado, tanto de elementos como de operaciones y opciones omitidas. [CS04](04_cobertura_y_descubrimiento.md) conserva el descubrimiento de elementos aún no observados y define cuándo un recorrido de extracción puede reanudarse. Quedan fuera el descubrimiento exhaustivo de cualquier página, la clasificación semántica por relevancia y garantizar que todas las opciones caben en una sola solicitud. Una ventana incompleta no equivale a ausencia de controles.

## Alternativas

| Alternativa | Ventaja | Limitación |
| --- | --- | --- |
| Subir 250 | Cambio pequeño | Mantiene sesgo DOM y no acota bytes ni expansión |
| Limitar solo nodos | Protege diversidad de elementos | Un `select` todavía puede producir miles de objetivos |
| Elegir candidatos con otro modelo | Puede usar relevancia del objetivo | Añade inferencia, coste y otro punto de decisión; se descarta aquí |
| Presupuestos combinados con reparto determinista y ventanas sucesivas | Acota recursos y permite consultar omisiones | Requiere cobertura, cursores válidos y límites acumulados; añade decisiones por ventana |

Se recomienda la última alternativa. El reparto expresa capacidad y diversidad estructural, no preferencias sobre negocios o sitios. La secuencia común es observación/normalización CS04, elegibilidad CS03, supresión CS06, contexto CS02 y presupuesto CS01, llamada Jev, validación, gate CS05 y ejecución. Esta propuesta no reincorpora candidatos excluidos por etapas anteriores ni los elimina basándose en su etiqueta.

## Diseño recomendado

### Límites y representación

Como valores iniciales de diseño, sujetos a pruebas locales, se proponen por ventana: **80 nodos**, **12 opciones por selector**, **250 acciones de navegador totales incluyendo controles globales** y **96 KiB para el cuerpo JSON completo**. Son parámetros propuestos, no capacidades verificadas de TypeSafe. Los terminales `DONE` y `BLOCKED` y el control propuesto `MORE_CANDIDATES` no son acciones DOM, pero sus preguntas y metadatos cuentan en bytes.

La extracción conserva primero un registro compacto por nodo y sus operaciones. Para `select`, recopila opciones dentro de un presupuesto de inspección y solo materializa acciones para las admitidas. El trabajo de extracción también necesita límites: inicialmente 5000 controles inspeccionados y 5000 opciones inspeccionadas en total. Un corte del recorrido marca el total como desconocido; no se publica una cifra exacta inventada.

Reservar espacio para los controles globales realmente disponibles antes de repartir acciones de elementos. Agrupar nodos por región estructural —diálogo, formulario, fila, artículo o región raíz— con un máximo de 32 grupos y un grupo residual explícito. Alternar regiones y familias de operación mediante rondas deterministas, con orden DOM como desempate. Admitir primero una representación mínima por nodo; repartir opciones adicionales en rondas posteriores. Los nodos editables mantienen ambas operaciones cuando hay capacidad; omitir una operación también debe contabilizarse.

La reserva se limita a controles sin objetivo, como los desplazamientos globales actuales y `WAIT`. Cuando CS04 introduzca scroll con objetivos de región, cada región y dirección consumirá las cuotas comunes de nodos, acciones y bytes. Sus objetivos participarán en el reparto; no recibirán una reserva ilimitada por conservar el nombre de operación `SCROLL_UP` o `SCROLL_DOWN`.

### Empaquetado y cobertura

```text
inventory = collect_bounded_nodes_and_options()
coverage = describe_inventory_limits(inventory)
selected = reserve_available_global_controls()
for allocation in deterministic_rounds(inventory):
    if node_option_and_action_limits_allow(allocation):
        try_add(allocation)
body = build_jev_request(goal, selected, coverage)
while serialized_utf8_bytes(body) > request_limit:
    remove_last_optional_allocation_or_fail()
    rebuild_targets_and_coverage()
assert every_target_maps_to_one_retained_observed_action()
```

El límite se comprueba sobre los bytes que efectivamente se enviarán, incluyendo objetivo, reglas, historial, metadatos y todas las cabezas. Definir una serialización JSON única y enviar exactamente ese cuerpo evita medir una variante y transmitir otra. Si objetivo y estructura mínima ya superan el presupuesto, devolver `CandidateBudgetExceeded` antes de inferencia; no truncar silenciosamente el objetivo. El transporte conserva autenticación y política HTTP existentes.

La solicitud añade `state.candidate_coverage`: versión de política, ámbito de extracción, nodos observados/admitidos, acciones admitidas, opciones admitidas/omitidas por selector, totales conocidos o límites inferiores y motivos de recorte. Distinguir `extraction_complete` de `budget_complete`: un frame omitido o un recorrido interrumpido hace incompleta la extracción; perder objetivos elegibles durante el empaquetado hace incompleto el presupuesto. Si se mantiene un campo agregado `complete`, será la conjunción de ambos, siempre dentro del ámbito declarado, nunca una promesa de cobertura universal. Los contextos no soportados o desconocidos se declararán aparte y no permitirán afirmar cobertura total. No envía etiquetas ni valores de candidatos excluidos como supuesto sustituto de sus objetivos. Registrar bytes, límites y digest de la selección para reproducibilidad local. Un selector incompleto declara expresamente que existen alternativas no ofrecidas.

No se rota la ventana automáticamente en cada lectura: las lecturas de frescura no deben modificar el conjunto candidato. Cada selección pertenece a una observación y una ventana explícita; `marker` mantiene su semántica de frescura y el digest de selección se compara por separado. Solo una decisión de reconsulta aceptada puede avanzar el cursor según el contrato siguiente.

Las instrucciones indicarán que ausencia en una lista parcial no prueba ausencia en la página. `DONE` sigue necesitando verificación externa; la propuesta no declara éxito por haber agotado candidatos ni exige bloquear todo objetivo cuando exista cualquier recorte.

### Reconsulta explícita de candidatos faltantes

Ejemplo: un desplegable ofrece 12 opciones en la primera ventana y la opción solicitada no está entre ellas. Jev puede pedir más candidatos y recibir opciones pendientes en ventanas posteriores. Al aparecer la opción adecuada, elige `SELECT` sobre su índice vigente. El mismo mecanismo permite alcanzar un botón omitido por el límite de nodos, acciones o bytes; no está restringido a desplegables.

1. **Pedir más dentro de la decisión normal.** Añadir la operación de control `MORE_CANDIDATES`, sin cabeza de objetivo, cuando exista una continuación utilizable y quede presupuesto. Jev la elige si la ventana actual no ofrece una acción adecuada y necesita examinar las restantes. No devuelve un selector, una etiqueta a buscar ni un cursor. El cursor pertenece al controlador. Las instrucciones deben distinguir esta petición de `WAIT`, que sigue representando una espera de la página.
2. **Avanzar mediante LangGraph.** Validar la operación, aplicar CS05 y comprobar frescura; después consumir la decisión una sola vez, registrar el avance y preparar la siguiente ventana. No llamar a `Browser.act` ni al auxiliar de texto. En `run`, continuar con una nueva petición Jev; en `tick` o `act`, publicar la nueva ventana y finalizar ese turno. `predict` solo publica la decisión pendiente. La nueva operación no convierte una lectura en scroll, apertura de menús ni otra mutación.
3. **Ofrecer candidatos nuevos.** Mantener un recorrido determinista con `inventory_id`, versión, `window_id`, cursor local y conjunto acotado de acciones ya presentadas. La identidad combina referencia del elemento, operación y, para `SELECT`, identidad de opción; las etiquetas y los índices de presentación no sirven para deduplicar. Tras `MORE_CANDIDATES`, priorizar únicamente acciones pendientes con el mismo reparto por regiones y operaciones. Un selector puede reaparecer con opciones nuevas; los controles globales disponibles pueden repetirse. Los metadatos y el contexto mínimo repetido también consumen presupuesto. Solo marcar como presentado lo realmente incluido en una petición enviada; no lo retirado durante el ajuste de bytes.
4. **Mantener selección y frescura separadas.** Cada nueva ventana reconstruye índices, cabezas y mapa local. Una decisión queda vinculada a `decision_id`, versión de inventario, `window_id`, digest de selección y frescura DOM. `act` debe validar esa vinculación completa: el mismo fingerprint DOM puede corresponder a ventanas distintas. Una acción solo puede ejecutarse desde su ventana vigente; no se aceptan índices ni decisiones de ventanas anteriores. Repetir una lectura o una comprobación de frescura conserva la misma ventana. Cambios relevantes de documento, nodos, opciones, contexto, elegibilidad o supresión invalidan el recorrido afectado y obligan a reobservar. La validación del inventario debe cubrir también los candidatos pendientes, no solo los incluidos en la ventana. Tras una acción de navegador se vuelve a observar y se inicia un recorrido acorde al estado resultante, sin reutilizar decisiones consumidas ni reiniciar el presupuesto global de la tarea.
5. **Distinguir paginado de descubrimiento.** Primero consultar lo ya observado y elegible que se omitió por presupuesto. Si la extracción se interrumpió, solo reanudarla cuando el adaptador de CS04 ofrezca una continuación válida, de solo lectura y acotada. Los candidatos nuevos pasan por elegibilidad, supresión y contexto antes del reparto. Si para descubrirlos hace falta scroll o abrir un menú, Jev debe elegir esa acción de navegador por separado y después reobservar. Un contexto no soportado no se convierte en consultable por pedir más.
6. **Terminar con una causa explícita.** Detener la reconsulta cuando Jev seleccione una acción adecuada, no queden candidatos consultables, se alcance el máximo configurado de ventanas, decisiones, inspección o tiempo, o no haya progreso. Cada avance debe aportar al menos una acción pendiente que quepa, o completar una ampliación de extracción acotada que permita preparar una ventana útil. Un cursor repetido, una ventana idéntica sin candidatos nuevos o candidatos que no caben ni solos producen una causa de límite; no otra consulta idéntica. No aumentar cuotas automáticamente ni emitir `DONE` por agotamiento. Publicar la causa distinguiendo inventario agotado, cobertura incompleta, presupuesto insuficiente y falta de progreso. Agotar esta vía no bloquea automáticamente la tarea: si todavía existe una acción observada que permite descubrir más, como scroll o abrir un menú, Jev puede elegirla en una decisión nueva dentro del presupuesto global. Solo si no queda una vía válida de continuación se publica el bloqueo; no prueba que el elemento no exista en la página.

El presupuesto de inspección de 5000 controles y 5000 opciones es acumulado para el recorrido, no se renueva en cada ventana. Los máximos de ventanas y decisiones se configuran al inicio y quedan subordinados a los límites globales de pasos y tiempo; cambios de página no permiten eludirlos. No se garantiza encontrar cualquier opción con cualquier cuota, pero un inventario estable, finito y representable debe poder recorrerse sin saltos cuando el presupuesto alcance. No se necesita revisar el resto si ya existe un candidato adecuado para avanzar.

`state.candidate_coverage` añade `inventory_id`, versión, `window_id`, número de acciones únicas presentadas, pendientes conocidas o límite inferior, `more_available` y causa de finalización cuando proceda. El cursor y las referencias completas permanecen locales. `more_available` describe una continuación disponible dentro de los límites; su valor falso no demuestra cobertura completa. `budget_complete` sigue describiendo la ventana actual. El estado acumulado del recorrido se informa por separado: haber presentado todas las acciones elegibles del inventario no equivale a tenerlas simultáneamente en la petición ni a haber descubierto toda la página.

Los candidatos presentados en ventanas anteriores no se registran como acciones ejecutadas o rechazadas en CS06. La reconsulta tiene un registro de control separado: su progreso se mide por acciones nuevas ofrecidas, no por cambios del DOM, para no activar falsamente el detector de acciones de navegador sin efecto. Conserva su propio límite de falta de progreso y los límites globales. El historial enviado conserva un resumen acotado del recorrido, sin concatenar todas las ventanas ni etiquetas omitidas. Cada nueva decisión sobre una ventana cuenta como una llamada lógica Jev adicional; los intentos HTTP se contabilizan aparte. La reconsulta no añade un selector LLM intermedio y nunca llama al auxiliar salvo que posteriormente se elija `TYPE_TEXT`.

## Cambios por módulo y contratos

| Módulo | Cambio propuesto |
| --- | --- |
| [snapshot.js](../../AgentBrowser/snapshot.js) | Inventario compacto, identidad de opciones, contadores acumulados y reparto antes de expandir |
| [model.py](../../AgentBrowser/model.py) | Empaquetado por ventana, cobertura, serialización exacta y operación de control sin target |
| [questions.py](../../AgentBrowser/questions.py) | Regla sobre listas parciales y cuándo pedir más candidatos |
| [agent.py](../../AgentBrowser/agent.py) | Conservar inventario, cursor, selección/cobertura y decisión, sin alterar consumo único |
| [browser.py](../../AgentBrowser/browser.py) | Validar vigencia del inventario y separar selección de frescura DOM; no paginar al comprobar |
| [workflow.py](../../AgentBrowser/workflow.py) | Enrutar reconsulta, aplicar límites acumulados y publicar progreso o causa de fin |
| [static/app.js](../../AgentBrowser/static/app.js), [demo.py](../../AgentBrowser/demo.py) y [cli.py](../../agentrun/cli.py) | Mostrar ventanas, pendientes, llamadas adicionales y causas de agotamiento o límite |

Se mantiene la forma de la respuesta TypeSafe, ampliando el vocabulario de operaciones con `MORE_CANDIDATES`. Su compatibilidad debe verificarse offline antes de habilitarla; no se presupone soporte ya disponible. Al elegirla no se consume ninguna cabeza de objetivo, aunque otras operaciones hayan solicitado cabezas en esa misma petición. Mantener índices de objetivos dentro de la selección vigente y un mapa local a nodos reales. Los índices de presentación pueden cambiar entre ventanas y observaciones; nunca son identidad persistente.

## Riesgos, errores y límites

Cuotas demasiado pequeñas reducen utilidad; cuotas por regiones pueden favorecer páginas con muchas regiones artificiales. El grupo residual y los límites de grupos deben ser visibles en métricas. Las etiquetas enormes pueden expulsar otros candidatos; cualquier abreviación debe marcarse y conservar la acción local íntegra. El límite en bytes no garantiza un límite de tokens ni latencia del proveedor.

Una página puede cambiar durante la extracción: se aplican las guardas y recuperación existentes. Un presupuesto inválido o insuficiente falla antes de llamar al modelo; no desencadena espera infinita, aumento automático de límites ni repetición de una acción anterior.

El paginado añade latencia y llamadas Jev; una página muy cambiante puede invalidar recorridos repetidamente. Los reinicios conservan el gasto acumulado y terminan al alcanzar los límites. Haber ofrecido un candidato no demuestra que Jev lo haya interpretado correctamente: el recorrido evita omisiones permanentes por recorte, pero no garantiza la selección correcta ni el éxito final.

## Aceptación, pruebas y métricas

Las pruebas serán locales, con HTML sintético, Browser Harness y proveedores simulados; ninguna utilizará APIs pagadas.

1. Un `select` con 1000 opciones seguido de botones deja representación para otros nodos cuando caben en sus cuotas.
2. Ningún resultado supera nodos, opciones por nodo, acciones globales o bytes configurados, incluso con Unicode y etiquetas extensas.
3. Todas las cabezas apuntan únicamente a acciones admitidas; una respuesta hacia un índice excluido se rechaza.
4. Contadores exactos coinciden con inventarios pequeños; recorridos interrumpidos indican total desconocido.
5. Misma observación, ventana y configuración generan la misma selección; comprobar frescura no avanza ventanas.
6. Un cuerpo mínimo sobredimensionado falla sin llamada de modelo ni mutación; elegir `TYPE_TEXT` mantiene su contrato auxiliar.
7. Un elemento o una opción correctos presentes solo en una ventana posterior se alcanzan mediante `MORE_CANDIDATES` simulado. Verificar por separado el clic o valor final; la reconsulta por sí sola no emite entradas de navegador.
8. Un inventario estable se recorre sin saltos ni repetición de acciones de elemento cuando el presupuesto alcanza. Cubrir límites de nodos, operaciones, opciones y bytes, incluidas opciones distintas con la misma etiqueta y candidatos retirados por el empaquetado final.
9. Ventanas sucesivas respetan todas las cuotas y los límites acumulados. Un inventario agotado, un cursor repetido, una acción imposible de empaquetar o un límite de tiempo/consultas termina con causa explícita, sin bucle ni `DONE` automático.
10. Cambiar o reemplazar un candidato pendiente invalida la continuación; referencias y decisiones de ventanas viejas nunca ejecutan, incluso si coincide el fingerprint DOM. Reobservar por frescura o reiniciar por cambios no renueva los presupuestos globales.
11. `predict` no avanza, `tick`/`act` consumen una sola reconsulta y `run` puede continuar. Cambiar cabezas no seleccionadas no afecta a `MORE_CANDIDATES`; no hay llamadas al auxiliar ni intentos de navegador en esa ruta.
12. Cada ventana evaluada suma exactamente una llamada lógica Jev en el proveedor simulado; el inspector distingue recorte de la ventana, progreso acumulado y extracción incompleta. Una continuación CS04 no soportada mantiene esa incompletitud visible.

Medir cobertura por nodo/región/operación, fracción de opciones representadas por ventana y acumulada, ventanas hasta encontrar el candidato, acciones únicas presentadas, motivos de exclusión y fin, reinicios, bytes reales, tiempo de extracción y llamadas lógicas frente a intentos HTTP. Conservar conteos de entrada/salida por etapa para distinguir pérdida por observación, inelegibilidad, supresión y presupuesto. No interpretar cobertura como éxito de negocio. Comparar fixtures con y sin selectores extensos y verificar el destino final independientemente.

## Dependencias y decisiones pendientes

[CS02](02_contexto_semantico.md) consume parte del mismo presupuesto; [CS03](03_ejecutabilidad_y_oclusiones.md) define elegibilidad; CS04 define descubrimiento y continuaciones de extracción; [CS05](05_incertidumbre_en_la_seleccion.md) interpreta incertidumbre con cobertura parcial y trata la reconsulta como inspección sin mutación; [CS06](06_memoria_de_intentos_rechazados.md) evita confundir exclusión presupuestaria o ventana ya presentada con rechazo ejecutado.

La implementación siguiente concreta los límites iniciales, la autoridad de presupuesto, las regiones y el avance de ventanas, y verifica el contrato nativo con un proveedor simulado. Quedan pendientes su calibración en tareas reales y las integraciones con las otras especificaciones. Cada ampliación de extracción requiere además el contrato de continuación de su adaptador CS04 y debe consumir la misma autoridad de presupuesto.

## Implementación del 19-10-2026

La autoridad de cuotas es `CandidateLimits`, exportada por `agentrun`; el agente la recibe mediante `candidate_limits`. Se implementan los límites de 80 nodos, 12 opciones por selector, 250 acciones y 96 KiB por petición, con 5000 controles y 5000 opciones inspeccionados por inventario. El reparto admite 32 regiones estructurales y un grupo residual. Los valores iniciales de ejecución son 120 ventanas consultadas, 120 decisiones intentadas y 300 segundos, además del límite previo de 60 acciones de navegador. Son configurables al crear el agente. Los cambios de documento no renuevan los límites globales; consultar de nuevo la misma ventana consume otra decisión, pero no otra ventana.

`candidates.py` conserva el inventario, prepara las ventanas, declara cobertura y valida el cuerpo JSON exacto. `MORE_CANDIDATES` se ofrece en la misma pregunta nativa de operación, sin target. El nodo `advance_candidates` de LangGraph consume la decisión, valida frescura y publica la siguiente ventana; no genera un intento de navegador ni llama al auxiliar. El inspector envía con `act` la identidad de decisión, inventario, ventana, selección y fingerprint. Para consumidores Python, `agent.decision_body()` obtiene ese cuerpo.

El lector mantiene identidades de opciones independientes de sus etiquetas y valores. La ejecución de `SELECT` utiliza la opción observada concreta, incluso con valores repetidos. Las guardas incluyen los candidatos pendientes, y comprobar frescura no avanza ventanas. Las métricas separan llamadas lógicas intentadas, intentos HTTP, ventanas y reconsultas; los candidatos únicamente presentados no se añaden al historial de acciones.

Se ha comprobado el contrato nativo con proveedores simulados, sin llamadas a APIs de pago. La verificación no acredita precisión ni capacidades del servicio remoto. La evidencia, los comandos y sus resultados están en [docs/validation.md](../../docs/validation.md). Los números anteriores se validan como cuotas de funcionamiento en fixtures, no como valores óptimos para cualquier sitio.

La implementación pagina el inventario observado en el documento principal y diagnostica límites de inspección y contextos excluidos. No implementa todavía las continuaciones de extracción, frames o shadow DOM de CS04, el contexto semántico adicional de CS02, la nueva política de elegibilidad de CS03, el gate de incertidumbre de CS05 ni la memoria de CS06. Se conservan las guardas de ejecución existentes. Estos contratos de integración siguen siendo diseño futuro, y ninguna declaración de cobertura ni `DONE` sustituye la verificación del resultado final.
