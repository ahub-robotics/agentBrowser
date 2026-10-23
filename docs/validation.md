# Validación de AgentRun

## Coordinador v2, 21 de octubre de 2026

**Aceptación con modelos reales pendiente.** El [registro acotado de evidencia](evidence/coordinator-v2-2026-10-21.json)
mantiene `validation_pending=true`. Las ejecuciones siguientes incluyen cambios de implementación entre
intentos; no constituyen una serie de aceptación sobre una versión fija. La aceptación completa con
modelos reales queda bloqueada por el rechazo HTTP 402 del proveedor registrado más abajo.

Las comprobaciones automatizadas finalizaron con **872 pruebas aprobadas en 12,00 s**, sin APIs de
pago. Ruff, la sintaxis JavaScript del inspector, lector DOM y fixture, `git diff --check` y `uv build`
también pasaron. Estas comprobaciones no sustituyen la aceptación con modelos reales.

El contrato v2 separa la interpretación y revisión del objetivo de la programación del trabajo.
LangChain produce un plan inicial y una auditoría independiente antes de las entradas de navegador;
después devuelve cambios de requisitos, evidencia de pasos y enlaces relevantes. El coordinador
registra las identidades observadas, conserva el orden, selecciona el trabajo pendiente y evita repetir
pasos verificados. Jev mantiene el control de las operaciones y sus objetivos observados.
El orden de primera observación de los enlaces se guarda junto con la evidencia, incluso antes de
registrarlos como trabajo. Una reanudación mantiene ese orden aunque cambie la disposición del listado.

Las regresiones offline comprueban rechazo y sustitución de planes antes de actuar, procedencia de
las referencias tras recortar el contexto, redirecciones observadas y conflictos de identidad,
reanudación sin repetir efectos, revisión atómica y recuperación mediante `observe` sin entradas.
También comprueban que una referencia antigua del destino junto con evidencia nueva del listado
no completa un paso que exige evidencia nueva en su destino.
Los fallos técnicos tienen estados `blocked` o `unverified`; `waiting_feedback` conserva su uso para
información o intervención imprescindible del usuario. La revisión semántica sigue siendo un juicio
del modelo: un esquema válido y referencias existentes no prueban por sí solos un resultado.

| Escenario | Modelos | Resultado independiente | Estado final |
| --- | --- | --- | --- |
| Chrome real y servidor local, recorrido completo | Simulados, herramientas v2 estrictas | 14 entregas únicas, 14 confirmaciones y 14 vueltas; dos páginas y final de resultados observado | `done`, verificado |
| Desarrollo local 01 | Reales configurados | 2 entregas únicas, 2 confirmaciones y 2 vueltas; objetivo de 14 incompleto | `unverified`, `recovery_exhausted` |
| Desarrollo local 02 | Reales configurados | 7 entregas únicas y confirmadas, 6 vueltas; objetivo de 14 incompleto | `blocked`, `supervisor_provider_error` |
| Desarrollo local 02, después de reanudar la misma ejecución | Reales configurados | **Acumulado:** 8 entregas únicas y confirmadas, 7 vueltas; objetivo de 14 incompleto | `blocked`, `supervisor_provider_error` |
| coches.net, diagnóstico desde listado general | Simulados, navegador real | 1 ficha y 1 vuelta verificadas de las 3 solicitadas | `unverified`, `step_not_verified` |
| coches.net, búsqueda filtrada | Simulados, navegador real | 3 fichas distintas y 3 vueltas verificadas; filtros comprobados desde la URL inicial | `done`, verificado |

El [recorrido local completo](../artifacts/coordinator-v2-offline-browser/local-1/report.json) utiliza
un servidor de prueba con 14 elementos repartidos en dos páginas y un registro de entregas independiente
de las afirmaciones del agente. Ejecutó **59 acciones**, **88 decisiones Jev simuladas**, **15 llamadas
simuladas de escritura** y **33 llamadas simuladas del supervisor**, con **cero solicitudes HTTP a
proveedores de modelos**, cero pausas de feedback y ninguna entrega duplicada. Demuestra la coordinación
del recorrido con esas decisiones simuladas; no mide la capacidad de los modelos reales.

El [primer desarrollo con modelos reales](../artifacts/coordinator-v2-live-development-01/local-1/report.json)
realizó 14 acciones, 24 decisiones y 24 solicitudes HTTP Jev, 3 llamadas de escritura y 17 llamadas y
17 solicitudes HTTP del supervisor. El diagnóstico encontró instrucciones de acciones futuras en las
restricciones generales: un paso de apertura podía recibir también órdenes de escritura o envío.
Se ajustaron la delimitación del paso actual y las instrucciones de planificación y auditoría; esta
ejecución precede a la comprobación de esas correcciones. No terminó el objetivo ni pidió feedback.

El [segundo desarrollo con modelos reales](../artifacts/coordinator-v2-live-development-02/local-1/report.json)
realizó 29 acciones, 43 decisiones y 43 solicitudes HTTP Jev, 8 llamadas de escritura y 17 llamadas y
17 solicitudes HTTP del supervisor. Terminó por **HTTP 402**, con categoría registrada
`openrouter_in_flight_budget` y motivo `in_flight_budget_exhausted`. Una
[consulta posterior del proveedor](../artifacts/coordinator-v2-provider-budget.json) confirmó saldo
positivo y límite de clave no agotado; el resumen público conserva únicamente esos booleanos, sin
importes. El rechazo de presupuesto en curso no demuestra ausencia de saldo. El resultado permanece
incompleto: siete entregas confirmadas no equivalen a siete recorridos terminados cuando falta una vuelta.

La [reanudación de ese mismo desarrollo](../artifacts/coordinator-v2-live-development-02-resumed/report.json)
conservó el identificador `c3f1eceffb10455e9976ba03521ba8ce`, la pestaña original y el origen del
servidor local. Alcanzó **8 entregas únicas y confirmadas y 7 vueltas**, observó las dos páginas y su
final, y volvió a detenerse por **HTTP 402**, con la misma categoría de presupuesto en curso del
proveedor. No hubo entregas duplicadas ni pausas de feedback. Sus contadores **acumulados** son 34
acciones, 50 decisiones y 50 solicitudes HTTP Jev, 9 llamadas de escritura y 23 llamadas y 23
solicitudes HTTP del supervisor. Estas cifras **sustituyen a las previas para esa ejecución; no se suman**
como si fueran dos recorridos distintos. La evidencia de saldo positivo anterior no identifica la
causa interna exacta ni garantiza que una nueva petición sea aceptada. No se realizaron más intentos
con modelos reales después de este segundo rechazo; la aceptación real queda pendiente de resolver
el bloqueo del proveedor.

La reanudación expuso también un rechazo local incorrecto de `exhausted` cuando todavía quedaban
elementos pendientes. El descubrimiento puede haber cubierto todas las páginas antes de terminar
su procesamiento. Se corrigió esa separación y las regresiones offline verifican que cada
paso y cada elemento deben completarse antes de finalizar o liberar trabajo dependiente. Si una
redirección fusiona dos identidades y deja menos elementos que la cantidad explícita del objetivo,
se reabre el descubrimiento. Por tanto, observar el final
de la segunda página no convierte las ocho entregas parciales en un objetivo completo.

El [diagnóstico de coches.net](../artifacts/coordinator-v2-web-offline/coches-1/report.json) hizo
23 acciones —un clic, dos operaciones de vuelta y veinte desplazamientos—, sin escritura de campos
ni envíos. Los modelos fueron simulados y hubo cero solicitudes a sus proveedores. El recorrido
desde el listado general no completó las tres fichas. Se ajustó el evaluador para partir de
una búsqueda filtrada. Esta ejecución no acredita la aceptación con modelos reales del sitio.

La [comprobación filtrada posterior](../artifacts/coordinator-v2-web-filtered-offline-02/coches-1/report.json)
verificó **tres fichas distintas y tres vueltas al listado**, con 10 acciones —tres clics, seis vueltas
y un desplazamiento—, 18 decisiones Jev simuladas y 10 llamadas simuladas del supervisor. No hubo
escritura, envío de contactos, llamadas de texto ni solicitudes HTTP a modelos. Los filtros estaban
proporcionados por la URL inicial y fueron comprobados independientemente: esta prueba no demuestra
que los modelos sepan configurarlos. El [intento filtrado anterior](../artifacts/coordinator-v2-web-filtered-offline/coches-1/report.json)
quedó registrado como `PermissionError` del entorno, sin crear una ejecución ni realizar acciones.

Las ejecuciones y la reanudación usaron límites explícitos de 180 acciones, 360 decisiones Jev, 360 ventanas,
120 llamadas del supervisor y 900 segundos activos. Ninguno pidió feedback. Las entregas de estos
ensayos pertenecen exclusivamente al servidor local de prueba; el escenario externo bloquea los
formularios de contacto. Las trazas completas quedan en `artifacts/`, ignorado por Git. El registro
publicable omite credenciales, importes, puertos locales, nombres y URLs de anuncios y contenido de
formularios. Las secciones siguientes conservan el historial de versiones anteriores.

## Feedback excepcional, 21 de octubre de 2026

La herramienta real `request_feedback` exige identificar requisitos pendientes y explicar la intervención
concreta que solo el usuario puede aportar. El coordinador rechaza referencias a requisitos desconocidos
o satisfechos. El prompt establece continuación autónoma después de acciones, subtareas y elementos;
el objetivo, el feedback anterior y las observaciones se consultan antes de pedir datos. La necesidad
semántica de una pregunta sigue dependiendo del modelo: el formato y los IDs válidos no la demuestran.

Dos respuestas inválidas consecutivas del supervisor terminan ahora como `unverified`, sin pregunta
ni entrada de consola. Se conserva el estado y la pestaña. Las preguntas justificadas mantienen la
espera de respuesta y la continuación de la misma ejecución.

Las pruebas offline recorren tres subtareas: dos continúan sin pausa, la tercera requiere un título
no especificado y termina después de recibirlo, sin repetir acciones. También rechazan preguntas
sin justificación, sin requisitos bloqueados o relativas a requisitos completados. **688 pruebas
aprobadas en 4,75 s**, sin APIs de pago; Ruff, sintaxis de ambos JavaScript, `git diff --check` y `uv build` correctos.
Esta modificación no incluye un nuevo recorrido de contactos con modelos reales en coches.net.

## Recuperación de enlaces y feedback por consola, 21 de octubre de 2026

Se revisó en modo de lectura el checkpoint `70d7656c2fc94bbf9ae2518e7cae8913`. Con el código
corregido, sus **993 hechos retenidos** exponen un catálogo de **49 enlaces únicos observados**, incluidos
**14 anuncios**. Una decisión de diagnóstico simulada registró los 14 elementos y superó la validación
de la siguiente delegación, únicamente en memoria. El checkpoint original mantuvo su hash: **cero
llamadas a APIs, cero entradas de navegador y ninguna reanudación del bucle de contactos**.
El [informe local](../artifacts/feedback-recovery-20261021/saved-replay.json) identifica expresamente
que la decisión procede del diagnóstico, no de una respuesta nueva de un LLM.

La corrección ofrece enlaces observados sin duplicados en cada revisión y retira `inspect_evidence`
cuando no quedan hechos retenidos por enviar. Los motivos de feedback se limitan al estado real:
en este checkpoint se ofrecieron información de usuario faltante y fallo de proveedor, sin dar por
agotada una recuperación que aún disponía de opciones. Las subtareas de descubrimiento ceden el
control cuando aparecen enlaces nuevos. Los errores de Jev publican categoría y código HTTP sin
exponer mensajes del proveedor. El historial enviado en la solicitud comparada se compactó de
**47.467 a 19.609 bytes**; esta reducción **no demuestra la causa del HTTP 400 observado**, que sigue
sin determinarse. El payload del supervisor en el diagnóstico ocupó **68.432 bytes UTF-8**.

`test.py` y la CLI supervisada esperan ahora feedback mediante `input()` en la misma sesión, sin
exigir una terminal TTY; esto permite responder desde PyCharm. Las entradas vacías o de más de 4000
caracteres vuelven a pedir respuesta sin consumir llamadas. EOF, una consola sin lectura y `--no-input`
dejan el checkpoint pausado y muestran cómo reanudarlo; Ctrl+C devuelve 130. La pestaña permanece
abierta salvo cierre explícito. `submit_feedback()` conserva la ejecución, los presupuestos globales
y los intentos inciertos; prepara una observación nueva y el trabajo continúa al volver a llamar a
`run()`. El reloj de tiempo activo queda congelado durante la espera.

La comprobación en **Chrome real y coches.net** con `scripts/check_supervised_feedback.py` terminó
**verificada** en la ejecución diagnóstica `875615c27792462b863fa1232e6e7d86`. Solicitó el título
esperado, aceptó una respuesta por consola y continuó con **la misma ejecución y la misma pestaña**.
El estado diagnóstico era independiente del checkpoint de origen, cuyo contenido permaneció intacto.
Hubo **dos llamadas al supervisor simulado, cero llamadas a modelos y cero entradas de navegador**;
la pestaña quedó abierta. La lectura DOM independiente también verificó BMW Z4, Madrid y máximo
de 20.000 €. [Informe local](../artifacts/feedback-recovery-20261021/live-console/report.json).

Las dos lecturas de entrada registraron **1.586.819 ms y 17.730 ms de espera**, ambas con **0 ms de
incremento del tiempo activo**. El informe conserva 19 ms activos y 1.604.698 ms de tiempo de pared.
Estos tiempos incluyen espera manual y de la sesión de herramientas; **no son un benchmark de
rendimiento**. El [resumen de evidencia](evidence/feedback-recovery-2026-10-21.json) omite rutas privadas,
datos de contacto y URLs de anuncios. Esta comprobación valida la interacción de consola y la
conservación del estado; no acredita completar los 14 anuncios ni resolver el fallo del proveedor nativo.

Comprobaciones de esta actualización: `uv run pytest` aprobó **681 pruebas en 5,11 s**, sin APIs
de pago. Ruff, la sintaxis de `static/app.js` y `snapshot.js`, `git diff --check` y `uv build` también pasaron.

## Diagnóstico del supervisor real, 20–21 de octubre de 2026

Se ejecutó `test.py` contra **coches.net con los modelos configurados**, separado de las pruebas
automatizadas sin APIs de pago. Ninguno de los seis intentos completó el objetivo. Los cinco primeros
no enviaron contactos; **el sexto envió un contacto real y observó «¡Mensaje enviado!»** antes de volver
al listado filtrado. Son ejecuciones de desarrollo con cambios entre ellas, no una medición de
fiabilidad de una versión fija. La duración es la publicada por cada ejecución.

| Ejecución | Duración | Resultado observado | Log local |
| --- | --- | --- | --- |
| `30ddb39836724c4db8552930665a6ffb` | 25,2 s | Seleccionó Z4 y confirmó el panel; terminó `waiting_feedback` por respuesta del supervisor inválida. | [before.log](../artifacts/diagnose-supervisor-20261020/before.log) |
| `7b61736417c14405a4b4f7d17d8866f9` | 65,7 s | Configuró BMW Z4, Madrid y máximo de 20.000 €; pidió confirmar si debía pulsar «Mostrar 14 resultados», aunque el objetivo ya lo indicaba. Terminó `waiting_feedback`. | [after-01.log](../artifacts/diagnose-supervisor-20261020/after-01.log) |
| `57818b2194ae41ce868cc537c95d76aa` | 7,7 s | Declaró `done` tras revisar únicamente la condición de cookies. El resto del objetivo quedó sin ejecutar: falso positivo de finalización. | [after-02.log](../artifacts/diagnose-supervisor-20261020/after-02.log) |
| `bd80823543c84b979aa9ab50b6412d24` | 21,7 s | Configuró Z4, pero pidió al usuario confirmar la selección y el cierre del panel. Terminó `waiting_feedback`. | [after-03.log](../artifacts/diagnose-supervisor-20261020/after-03.log) |
| `dbdfcd64ed674eabaa9fa0697441d6b9` | 21,8 s | Propuso delegar sin subtarea; después de recibir el error pidió confirmar si debía continuar tras cookies. Terminó `waiting_feedback`. | [after-04.log](../artifacts/diagnose-supervisor-20261020/after-04.log) |
| `021377852bff4939bdaff7263c400d02` | 87,3 s | Aplicó los filtros, mostró resultados, envió un contacto confirmado y volvió al listado. La siguiente decisión usó una URL observada como referencia de elemento y fue rechazada; terminó `waiting_feedback`. | [after-05.log](../artifacts/diagnose-supervisor-20261020/after-05.log) |

El sexto intento registró **19 acciones ejecutadas, 32 decisiones Jev y 32 solicitudes HTTP Jev,
4 llamadas de escritura y 8 llamadas del supervisor con 8 solicitudes HTTP**. Sus cinco intentos
rechazados antes de enviar entrada no se contabilizan como acciones ejecutadas. La confirmación de
envío está conservada en el hecho `bc09c9ebd30570d9:page:text`, observación 30. El estado final todavía
no había registrado el anuncio en `items`; se conserva sin reanudar mutaciones para evitar un contacto
duplicado. **No se ha comprobado completar los 14 resultados ni todo el bucle solicitado.**
El [resumen de evidencia sin datos personales](evidence/supervisor-real-2026-10-21.json) conserva
los contadores de los seis checkpoints y la referencia de confirmación, sin copiar formularios ni páginas completas.

El diagnóstico motivó separar el objetivo global de la instrucción entregada a Jev, mostrar la delegación
completa y conservar su contexto durante una recuperación. También se priorizan y retienen los hechos
que permiten revisar la subtarea al volver a un listado; la revisión de una subtarea puede acreditar
progreso antes de completar un requisito global. Una respuesta inválida recibe una oportunidad de
corrección estructural sin ejecutar la decisión rechazada ni repetir acciones del navegador, y el
coordinador gestiona el avance de `inspect` por la evidencia ya observada.

El supervisor dispone ahora de cuatro herramientas de intención separadas: delegar, inspeccionar
evidencia, finalizar y pedir feedback. La herramienta de delegación exige una subtarea; la de feedback
debe especificar su motivo. Las subtareas declaran `general`, `discover` o `item`: procesar un elemento
requiere una identidad observada y un bucle registrado. Una URL completa puede identificar un elemento
solo si coincide exactamente con un enlace observado ya enviado al supervisor; no sustituye a una
referencia de evidencia para verificar resultados. También se conserva la última evidencia del destino
del elemento activo al volver al listado. Estas correcciones de identidad y alcance son posteriores
al sexto intento y **no tienen todavía una nueva ejecución autónoma con modelos que complete el bucle**.

La comprobación final obtuvo una lectura independiente del DOM real: el encabezado confirmó BMW Z4
y Madrid, y el parámetro de URL `MaxPrice=20000` confirmó el máximo de precio. Los tres filtros quedaron
verificados. Además, se pasó una respuesta histórica por los validadores actuales, adaptándola localmente
con `scope=item` y `loop_id=results-ads-loop`: la URL observada del segundo anuncio se resolvió y registró
correctamente en memoria. Esta comprobación realizó **cero llamadas a modelos, cero entradas de navegador
y ninguna modificación del checkpoint guardado**; no es una nueva respuesta del LLM ni una continuación
del bucle. La pestaña quedó abierta. Evidencia local: [informe de comprobación](../artifacts/diagnose-supervisor-20261020/final-readonly-verification.json)
y [DOM independiente](../artifacts/diagnose-supervisor-20261020/final-independent-dom.json).

La cobertura textual de los requisitos impide delegar o finalizar dejando palabras del objetivo fuera
de sus citas. Esta guarda no prueba exhaustividad semántica ni éxito: un requisito que cite todo el
objetivo todavía puede ser interpretado o evaluado incorrectamente. Las pruebas offline de regresión
comprueban estos contratos, incluida la conservación de la evidencia de un campo tras volver a un
listado con 220 controles y durante la poda de hechos. `test.py` devuelve código cero solo con
`done` y verificación satisfecha, código 2 para los demás estados finales y código 1 si no hay estado.
**Estos intentos muestran progreso parcial, no una aceptación completa del supervisor real.**

Checks de esta fase del **21 de octubre de 2026**: `uv run pytest` aprobó **630 pruebas en 4,09 s**, sin APIs
de pago. También pasaron `uv run ruff check .`, la sintaxis de `static/app.js` y `snapshot.js`,
`git diff --check` y `uv build`.

## Supervisor LangChain y reanudación

Comprobado el **20 de octubre de 2026** en Chrome y **coches.net real**, con supervisor, Jev y auxiliar
simulados. La ejecución final verificó **tres anuncios distintos**, el mensaje esperado sin envío en cada
uno y tres regresos al listado. También interrumpió el trabajo tras el primer mensaje verificado, liberó
la sesión y reanudó sobre la misma pestaña sin repetir el llenado. Cada anuncio tenía un enlace observado
en una tarjeta del listado antes de visitarlo. [Resumen de los intentos](evidence/supervisor-2026-10-20.json).

La ejecución satisfactoria registró **12 acciones: 3 clics, 3 escrituras y 6 operaciones BACK**;
dos entradas del historial por anuncio fueron necesarias para volver al listado. Hubo **18 decisiones
Jev, 3 llamadas al auxiliar y 7 llamadas al supervisor**, todas simuladas, **cero solicitudes HTTP
a modelos** y **8.893 ms de tiempo activo**. Esta duración excluye la pausa de reanudación y no representa
la latencia de modelos reales.

| Intento real | Resultado independiente | Acciones | Decisiones Jev | Auxiliar | Supervisor | Tiempo activo |
| --- | --- | --- | --- | --- | --- | --- |
| `170144-569395` | Desconocido; 0 mensajes | 14 | 17 | 0 | 5 | 3.683 ms |
| `170939-084887` | Desconocido; 1 mensaje y regreso | 16 | 19 | 1 | 3 | 6.830 ms |
| `174201-148251` | Verificado; 3 mensajes y regresos | 12 | 18 | 3 | 7 | 8.893 ms |

El primer intento expuso una limitación del simulador: el textarea se llamaba «Contacta con el anunciante»,
no «Mensaje». El segundo encontró un listado reordenado al volver; se corrigió el simulador para registrar
un único anuncio actual por vez y se compactó el contexto del supervisor. El indicador de reanudación del
primer informe no cuenta como aceptación porque no hubo un primer llenado verificado. Los artefactos
originales se conservan sin modificar.

`scripts/evaluate_supervised_coches.py` prohíbe los clics de contacto y bloquea tanto los transportes nativos
como la invocación del supervisor real. El oráculo DOM independiente contrasta los valores, identidades y
regresos sin usar `DONE` como prueba. Los informes, observaciones y trazas completos quedan en `artifacts/`,
ignorados por Git; el resumen enlazado excluye datos personales de formularios.

La actualización añade un modo supervisado opcional, conservando el modo directo del inspector.
Las pruebas automatizadas usan proveedores y navegadores simulados, sin APIs de pago.
Esta aceptación comprueba la ejecución, las guardas y la persistencia; **no evalúa la precisión del LLM
potente ni la calidad de sus planes**. Las ejecuciones posteriores con modelos reales se describen arriba.
En esta validación del 20 de octubre hubo **550 pruebas aprobadas**, sin APIs de pago. También pasaron
Ruff, la comprobación de sintaxis de `static/app.js` y `snapshot.js`, `git diff --check` y `uv build`.

El evaluador histórico `scripts/evaluate_coches.py` conserva su alcance de filtros y primer anuncio, usando
ahora un objetivo propio. Ya no importa el prompt de `test.py`, que contiene el bucle de envíos al anunciante.

## Conservar la pestaña al terminar

El **20 de octubre de 2026** se cambió el comportamiento predeterminado: salir de `with Agent(...)` desconecta la sesión CDP y mantiene abierta la pestaña. También se conserva al terminar con `blocked`, `unverified`, una excepción o una interrupción. El inspector libera el control sin cerrar la pestaña al reiniciar la tarea o salir. La desconexión impide futuras operaciones sobre esa instancia para evitar que Browser Harness las dirija a otra pestaña.

`close_tab_on_exit=True` o `--close-tab` solicita explícitamente el cierre. `Agent.close()` sigue cerrando únicamente su pestaña; `Agent.disconnect()` la conserva. La CLI, `test.py`, los ejemplos y los evaluadores aplican esta preferencia. Los scripts de comprobación local mantienen la limpieza explícita de sus propias fixtures.

Se comprobó con **Chrome real en coches.net**, sin modelos ni acciones sobre la página: tras salir del contexto, `Target.getTargets` seguía incluyendo la pestaña y su URL, y la sesión del agente estaba desconectada. La pestaña de esta comprobación quedó abierta. [Evidencia](evidence/tab-lifecycle-2026-10-20.json).

**445 pruebas aprobadas**, sin APIs de pago en la suite. Ruff, sintaxis de ambos JS, `git diff --check` y `uv build` correctos. La suite incluye conservación en estados terminales, errores e interrupciones, cierre solicitado, limpieza de inicialización, desconexión idempotente y prohibición de enviar comandos tras desconectar.

## Precisión del diagnóstico HTTP 402

La etiqueta inicial «saldo insuficiente» era demasiado específica para un HTTP 402 sin detalle. Ahora se muestra «crédito o límite de gasto» y, si OpenRouter proporciona campos reconocidos, se distingue límite de clave, presupuesto temporal, coste de petición y presupuesto de organización. Solo se conservan valores permitidos de `limit_source` y `reason`, conforme a la [documentación del proveedor](https://openrouter.ai/docs/api_reference/limits); no se copian mensajes ni cuerpos de error.

Una revisión guardada se volvió a enviar sin navegador y recibió HTTP 402 con `limit_source=openrouter_credits` (`artifacts/provider-limit-probe-20261020.json`). Las consultas autenticadas de solo lectura separaron el margen del límite de clave de los créditos comprados y consumidos. La respuesta de créditos de la cuenta se conserva únicamente en el artefacto local ignorado `artifacts/provider-credit-check-20261020.json`. No se modificaron el saldo, los límites, las claves ni el modelo. Los errores anteriores sin ese detalle no permiten inferir retrospectivamente el origen exacto de cada rechazo.

## Diagnóstico de parada al confirmar

El **20 de octubre de 2026**, después de recibir un log que terminaba en `No verificado: CLICK → "Aceptar"` a los 4,9 segundos, se reprodujo el recorrido en coches.net. La ejecución `coches-validation-20261020-110701-645738` terminó a los **4.647 ms**: había abierto marca/modelo, escrito la búsqueda y seleccionado Z4, pero no había ejecutado «Aceptar». La primera llamada al revisor devolvió **HTTP 402** en 57 ms. [Registro de diagnóstico](evidence/provider-failure-2026-10-20.json).

El transporte descartaba el código HTTP al transformar todos los errores en `provider_error`, y la salida INFO omitía incluso ese motivo. Ahora conserva únicamente una categoría permitida y el código HTTP y muestra la causa en la misma línea: `No verificado: CLICK → "Aceptar" (revisor: saldo insuficiente, HTTP 402)`. No se guardan cuerpos de error, URLs, mensajes arbitrarios ni credenciales. Los reintentos de transporte existentes conservan sus límites; HTTP 402 no se reintenta y ninguna mutación del navegador se repite.

Esta reproducción identifica un rechazo de facturación del proveedor de revisión, no demuestra el saldo exacto de la cuenta. La ejecución sigue siendo **no verificada**, con cero criterios finales confirmados. No se cambió el modelo, no se recargó la cuenta y no se omitió la revisión. Continuar requiere resolver el saldo o límite aplicable en el proveedor configurado. Hubo **7 decisiones Jev, 2 llamadas de escritura, 1 revisión con 1 intento HTTP y 3 acciones de navegador**.

La suite comprueba categorías HTTP, fallos de conexión y JSON, conteos, ausencia de entradas ante rechazo de facturación y logs sin secretos. **364 pruebas aprobadas**, Ruff, sintaxis de ambos JS, `git diff --check` y `uv build` correctos. Se mantienen separados estos casos simulados y la reproducción real con APIs configuradas.

## Logs y coste de revisión

Corregidos el **20 de octubre de 2026**. Las elecciones internas pasan a `DEBUG`; la consola distingue `Ejecutado`, `Descartado` y el estado terminal. Una propuesta invalidada por cambios del DOM ya no parece otro clic realizado. `test.py` imprime el estado y la duración al terminar, sin una línea `ready` por turno. Las acciones realmente sucesivas se siguen mostrando.

La referencia del 19 de octubre empleó **75.996 ms de sus 93.804 ms en 18 revisiones semánticas**. Ahora desplazar una región fuera de la vista o recuperar una propuesta obsoleta no fuerza una revisión adicional. Una apertura observada como `expanded=false` puede omitirla únicamente si no envía un formulario ni abandona otro control expandido. Se conservan las guardas de frescura, el snapshot después de cada acción, las revisiones de confirmaciones, navegación y conclusiones, y la prohibición de repetir entradas de navegador.

El lector observa la capacidad de submit de botones nativos y protege cambios de tipo o propietario del formulario. El revisor recibe hasta 60 ejecuciones compactas además de los diez pasos con efectos recientes; así las confirmaciones antiguas no desaparecen por varios desplazamientos. El empaquetado comprueba los bytes de la petición HTTP completa, incluido el esquema. Una conclusión terminal rechazada y repetida sin evidencia nueva se detiene como `unverified` sin otra llamada de revisión.

| Comprobación final | Resultado |
| --- | --- |
| `uv run pytest` | **329 aprobadas**, sin APIs de pago |
| Chrome con páginas locales | **59 aprobadas**: 21 guardas, 11 candidatos, 11 etiquetas, 10 recuperación y 6 controles submit |
| Ruff y sintaxis de ambos JS | Aprobados |
| `uv build` | Wheel y distribución fuente generados |

Se realizaron cuatro ejecuciones contra coches.net real, documentadas en el [registro de rendimiento y resultados](evidence/performance-2026-10-20.json):

| Ejecución | Duración | Decisiones / revisiones / escritura | Resultado independiente | Estado del agente |
| --- | --- | --- | --- | --- |
| `074529-924107` | 56,218 s | 33 / 11 / 3 | 3/4; no abrió el primer anuncio | `blocked`, ciclo sin progreso |
| `080433-214204` | 46,873 s | 25 / 8 / 3 | 3/4; no abrió el primer anuncio | `unverified`, petición excedía el presupuesto |
| `081713-002561` | 57,437 s | 27 / 11 / 3 | **4/4 verificados** | `unverified`, condición de cookies desconocida |
| `100529-892984` | 25,316 s | 17 / 4 / 3 | Desconocido; no llegó al listado | `unverified`, error del proveedor de revisión |

La segunda ejecución hizo siete transportes de revisión: la octava revisión se detuvo antes de HTTP. Las demás hicieron tantos transportes como revisiones. Los intentos expusieron la pérdida de confirmaciones antiguas y un desbordamiento de 68 bytes del cuerpo completo; ambos motivaron las correcciones y sus pruebas. La tercera comprobó BMW Z4, Madrid, máximo de 20.000 € y apertura del primer anuncio, pero el revisor declaró desconocida la condición de cookies mientras su explicación la describía como no aplicable. Un contraste adicional sobre esa observación guardada reprodujo la contradicción; no fue una prueba unitaria. La protección contra conclusiones idénticas se añadió después y está cubierta con proveedores simulados.

La última ejecución de esta fase incluía el objetivo editado entonces, con la petición de guardar la URL. El evaluador comprobaba solo los cuatro criterios de búsqueda y navegación, no el guardado de esa URL. Antes de ese intento se corrigió una importación de `ORIGINAL_GOAL`, retirada entonces de `test.py`; el arranque fallido no realizó llamadas. Se conservó aquel texto como una única cadena y se corrigió la compatibilidad del evaluador.

La ejecución con cuatro resultados independientes pasó de 93,804 a 57,437 segundos, pero terminó con distinto estado semántico y hubo cambios entre intentos. **Estas muestras no establecen una mejora garantizada de velocidad ni fiabilidad.** La versión final conserva un error explícito cuando el proveedor falla y no presenta resultados desconocidos como éxitos. Las trazas completas permanecen en `artifacts/`, ignorado por Git; el registro enlazado conserva conteos y limitaciones.

## Recuperación y verificación de resultados

Implementada y comprobada el **19 de octubre de 2026**. La última ejecución de esa fecha contra **coches.net real** terminó en `done` y la lectura independiente del DOM verificó **los cuatro criterios** del objetivo que contenía entonces `test.py`: BMW Z4, Madrid, precio máximo de 20.000 € y apertura del primer anuncio del listado. [Registro resumido de todos los intentos y comprobaciones](evidence/reliability-2026-10-19.json).

| Comprobación final | Resultado |
| --- | --- |
| `uv run pytest` | **301 aprobadas**, sin APIs de pago |
| Chrome con páginas locales | **53 aprobadas**: 21 guardas, 11 candidatos, 11 etiquetas y 10 recuperación/observación |
| Ruff, sintaxis de ambos JS y `git diff --check` | Aprobados |
| `uv build` | Wheel y distribución fuente generados |
| Pérdida controlada de sesión CDP en Chrome | Reconexión a la misma pestaña, viewport restaurado, decisión invalidada y un único clic |
| coches.net: `coches-validation-20261019-181220-231274` | **4/4 verificados**, agente `done`, sin error, 93.804 ms |

La implementación añade disponibilidad de la observación, estados de controles, regiones desplazables, cambios observados e historial con contexto. Conserva evidencia acotada del origen al navegar. El revisor comprueba los requisitos y las confirmaciones antes de ejecutar; rechaza valores distintos de los solicitados y conserva los resultados desconocidos como `unknown`. El formato JSON y las referencias se validan contra los hechos realmente enviados. Las relecturas, revisiones y recuperaciones tienen límites; ninguna recuperación repite automáticamente una mutación del navegador.

Una publicidad dinámica desplazó el primer anuncio fuera del viewport durante una revisión. El clic quedó correctamente descartado por falta de frescura, pero la siguiente decisión perdió su contexto. Ahora Jev y el revisor reciben la propuesta cancelada, su contexto y si llegó a ejecutarse. La prueba final reprodujo la recuperación: el agente desplazó la página, localizó el enlace y abrió el anuncio correcto. El enlace observado en el listado y la URL final coincidieron en el anuncio **71724383**. El evaluador obtiene sus datos directamente del DOM, separado del inventario y del juicio del modelo.

La ejecución final de esa fase hizo **25 decisiones Jev, 18 revisiones, 2 llamadas de escritura y 17 acciones de navegador**. Hubo 25 y 18 intentos HTTP de decisión y revisión, respectivamente. Los modelos fueron `jev-latest` (respuesta `jev-1.13.0`), `openai/gpt-4.1-mini` para escribir y `openai/gpt-4.1` para revisar. El revisor se configuró en el `.env` local ignorado por Git; el archivo externo y las credenciales se conservaron. No se cambió `test.py` durante aquella implementación del 19 de octubre. La variante `--original-prompt`, que incluye kilometraje, **no forma parte de esa aceptación**.

Se conservan **13 ejecuciones de desarrollo**, con cambios de código, instrucciones y modelo entre ellas: no son un benchmark de una versión fija. Una versión intermedia consiguió los cuatro resultados pero agotó la revisión y terminó `unverified`; la última consiguió ambos, resultado independiente y `done`. Los otros intentos expusieron respuestas inválidas, selección de umbrales incorrectos, confirmaciones prematuras o bloqueadas, pérdida de sesión y confusión entre listado y detalle. Los contrastes del revisor sobre observaciones guardadas también están registrados, incluidos tres errores de acceso al proveedor; no cuentan como respuestas válidas ni como pruebas unitarias.

El éxito final valida ese recorrido real, no garantiza todas las ejecuciones ni todos los automatismos. Las referencias válidas no garantizan una interpretación semántica correcta: el revisor más pequeño falló en casos de confirmación y GPT‑4.1 también confundió anteriormente listado y detalle. El evaluador independiente sigue siendo necesario. La aceptación independiente comprueba los cuatro criterios de búsqueda y navegación; la condición de cookies queda evaluada por el revisor semántico. El contenido de iframes, shadow DOM y canvas sigue fuera del lector; su cobertura incompleta se publica explícitamente.

Las trazas completas, observaciones y respuestas se guardan localmente bajo `artifacts/`, ignorado por Git. El JSON resumido enlazado conserva conteos y resultados sin copiar credenciales ni páginas completas. No se ha realizado commit ni push.

## Confirmación de paneles de filtros

El **19 de octubre de 2026** se reprodujo manualmente el panel de marca/modelo en coches.net con `Browser.observe` y `Browser.act`, sin inferencia. Tras seleccionar Z4, «Aceptar» aparecía en el inventario y en `click_target` de la primera ventana (índice 13, 27 elementos). El clic cerró el panel; el snapshot posterior mostró «BMW Z4» y «Mostrar 180 resultados». La pestaña de diagnóstico se cerró. [Evidencia acotada](evidence/filter-confirmation-2026-10-19.json).

Se aclararon las instrucciones generales: confirmar cada panel cuando sus opciones estén listas, sin esperar requisitos de otros paneles, y comprobar los filtros aplicados después. La reproducción verifica detección y ejecución; no identifica la decisión de la ejecución fallida ni prueba que Jev siga mejor la instrucción modificada. No se llamó a APIs de modelos.

## Etiquetas de botones con checkbox personalizado

Comprobadas el **19 de octubre de 2026**. El HTML facilitado para Z4 contiene un botón visible sin nombre y un input oculto al árbol accesible. Se añadió una asociación genérica con el texto visible de su fila semántica, únicamente cuando no hay ambigüedad, y se conserva `aria-pressed` en el inventario, las guardas y las preguntas al modelo.

`scripts/check_contextual_labels.py` aprueba **11 comprobaciones con Chrome real y una página local**, sin llamadas a modelos. Una comparación controlada usando la anterior expresión de etiquetado devuelve `button` para ambas filas; el lector corregido devuelve `Z4` y `X3`. Un clic observado sobre Z4 produce `checked=true`, `pressed=true` y un solo evento de clic; X3 permanece sin marcar. El snapshot posterior recoge el nuevo estado. También se verifican cambios de nombre/estado, nombres explícitos, texto oculto, filas ambiguas/anidadas y límites de lectura. Esto valida el HTML reproducido localmente; no es una ejecución completa de `test.py` contra el sitio externo.

La suite unitaria contiene **145 pruebas aprobadas**, incluidas tres de transmisión de los estados `pressed=false/true/mixed` y resolución de la identidad observada. Las **21 comprobaciones de guardas** y **11 de candidatos** existentes siguen pasando en Chrome. Estas últimas usan cuatro decisiones simuladas; ninguna API de pago.

## Snapshot después de cada acción

Comprobado el **19 de octubre de 2026**: el flujo existente obtiene un snapshot nuevo después de cada acción de navegador completada y lo utiliza en la siguiente decisión. Se añadieron **7 casos de regresión** para clic, escritura, selección, ambos desplazamientos, espera y recuperación de una observación interrumpida, en modos manual y automático. Incluyen un fingerprint sin cambios y verifican los datos publicados y recibidos por la política, el registro previo a la observación y la ausencia de repetición de la acción. Modelos y navegador simulados; ninguna API de pago.

`uv run pytest`: **142 aprobadas**. Ruff y la comprobación de sintaxis de `static/app.js` también pasan.

## Logs de decisiones

Comprobados el **19 de octubre de 2026**, con modelos y navegador simulados, sin llamadas a APIs de pago. `uv run pytest` aprueba **135 pruebas**, incluidas **14 nuevas** de logging. Ruff, la comprobación de sintaxis de `static/app.js` y `uv build` también pasan.

Las pruebas verifican el elemento y la opción observados, la separación entre selección y ejecución, el registro antes de observar el resultado, los rechazos por página obsoleta, las reconsultas sin entradas de navegador y los controles sin objetivo. También comprueban la exclusión de solicitudes y valores generados, el escape de controles Unicode y el respeto de la configuración de logging existente. La salida predeterminada se simplificó a una línea por elección, con operación y etiqueta; los eventos detallados de selección y ejecución se conservan en `DEBUG`, vinculados por el mismo identificador.

## CS01: presupuesto y reconsulta de candidatos

Ejecutada el **19 de octubre de 2026**, con Python 3.12.11, fixtures locales, Browser Harness y proveedores simulados. [Evidencia de ejecución](evidence/cs01-2026-10-19.json).

| Comprobación | Resultado |
| --- | --- |
| `uv run pytest` | **121 aprobadas**, sin fallos ni omisiones |
| Pruebas nuevas de CS01 | **45 aprobadas**: 22 de presupuesto/protocolo, 15 de flujo, 5 de observación y 3 de inspector/CLI |
| Suite original `tests/test_agent.py` | **31 aprobadas** y archivo sin modificar |
| `uv run ruff check .` | Aprobado |
| `node --check jev_ultrafast/static/app.js` | Aprobado |
| `node --check jev_ultrafast/snapshot.js` | Aprobado |
| `uv build` | Wheel y distribución fuente generados |
| `uv run python scripts/check_guards.py` | **21 comprobaciones existentes aprobadas** con navegador real |
| `uv run python scripts/check_candidate_observation.py` | **11 comprobaciones nuevas aprobadas** con navegador real y páginas locales |
| `git diff --check` | Aprobado |

Las pruebas comprueban cuotas combinadas, bytes UTF-8 idénticos a los enviados, reparto entre regiones y operaciones, opciones con etiquetas/valores duplicados, candidatos imposibles de empaquetar, ventanas sin progreso, límites acumulados, rechazo de decisiones de otra ventana y ausencia de entradas de navegador durante la reconsulta. Se mantuvieron las 76 pruebas anteriores y se añadieron 45.

El escenario completo de Chrome usa 37 opciones, con la primera inicialmente seleccionada. El proveedor simulado produce `MORE_CANDIDATES → MORE_CANDIDATES → SELECT → DONE`. La lectura independiente del DOM confirma `selectedIndex=36`, valor `value36`, un evento `input` y un evento `change`. El registro contiene **cuatro decisiones simuladas, dos reconsultas y una acción de navegador**, sin auxiliar textual. El contador de intentos de transporte también es simulado en ese escenario; hubo **cero solicitudes de red a APIs de modelos**. `DONE` por sí solo no forma parte de la prueba del resultado.

La comprobación de observación incluye un desplegable con 1000 opciones seguido de un botón, cambios y reemplazos de opciones pendientes, cortes a 5000 controles/opciones, 32 regiones y grupo residual, cuotas configuradas e iframes diagnosticados. Las continuaciones de extracción y capacidades adicionales de CS04 permanecen pendientes. Estos resultados verifican contratos locales; no son tasas de éxito ni mediciones de latencia de Jev en sitios externos.

Los lanzadores de `pytest`, `ruff` y `agentrun` del entorno local se regeneraron con `uv sync --locked --reinstall-package pytest --reinstall-package ruff --reinstall-package agentrun`: conservaban la ruta anterior al traslado del proyecto. No se cambiaron sus versiones fijadas para este trabajo.

## Adaptación inicial a LangGraph y LangChain

Ejecutada el **16 de octubre de 2026** en macOS, con Python 3.12.11.

Base: `browser-use/jev-ultrafast`, commit `1231850a0bf1a0c0341fe408ef1668dbbfdfac46`.
Dependencias resueltas en `uv.lock`: LangGraph 1.2.15 y LangChain Core 1.6.11.

| Comprobación | Resultado |
| --- | --- |
| `uv run pytest` | **76 aprobadas**, sin fallos ni omisiones |
| Suite original `tests/test_agent.py` | **31 aprobadas**; archivo sin modificar respecto a la base |
| Pruebas nuevas de configuración y CLI | **23 aprobadas** |
| Pruebas nuevas de adaptadores LangChain | **8 aprobadas** |
| Pruebas nuevas de flujo LangGraph | **14 aprobadas** |
| `uv run ruff check .` | Aprobado |
| `node --check jev_ultrafast/static/app.js` | Aprobado |
| `node --check jev_ultrafast/snapshot.js` | Aprobado |
| `uv build` | Generados wheel y distribución fuente |
| `uv run python scripts/check_guards.py` | **21 comprobaciones aprobadas** con Chrome y página local |
| Importación del wheel extraído | Aprobada; incluye archivos del navegador y del inspector |
| Arranque real de `agentrun` en puerto local temporal | HTML y `/api/state` correctos; proceso cerrado al terminar |
| Configuración externa | Archivo solicitado cargado; ambas credenciales presentes; Jev seleccionado |
| `git check-ignore .env` | `.env` ignorado; ningún `.env` incluido en el wheel |
| `git diff --check` | Aprobado |

Las pruebas de LangGraph ejercitan el grafo compilado, la pausa entre elegir y actuar, los estados terminales, los límites de ejecución, los callbacks y la recuperación antes/después de una mutación. Los errores ambiguos de ejecución se propagan sin reintentar la acción.

No se realizaron llamadas a APIs de modelos ni se ejecutaron los ejemplos de pago. La comprobación de navegador utiliza la página local original del script; no representa una medición del rendimiento ni del éxito de Jev en sitios externos.

Artefactos generados localmente:

- `dist/agentrun-0.1.0-py3-none-any.whl`
- `dist/agentrun-0.1.0.tar.gz`
