# CS04 Cobertura y descubrimiento de candidatos

**Fecha:** 2026-10-16  
**Actualización:** 2026-10-19 · Scroll vertical por región y observación posterior  
**Estado:** Fase A implementada; fases B–D y continuaciones de extracción pendientes  
**Ámbito:** Descubrimiento incremental de controles y regiones navegables.

**Índice:** [Propuestas del selector](README.md)

## Implementación disponible

Documento y paneles con desplazamiento vertical se representan como regiones observadas. Cada acción conserva nodo, dirección, posición, dimensiones y contexto acotado. Las cabezas `scroll_up_target` y `scroll_down_target` forman parte de la misma petición TypeSafe que la operación; solo se consume la cabeza elegida. Las regiones consumen cuotas de nodos y acciones de CS01. Los adaptadores antiguos que ofrecen scroll global sin nodo mantienen compatibilidad.

El ejecutor comprueba frescura, posición y un punto alcanzable de la región antes de enviar una única rueda. Después registra la ejecución y obtiene un snapshot nuevo. Observar y reconsultar candidatos siguen siendo lecturas: no desplazan, abren ni rellenan controles. Los controles recortados por un panel solo aparecen cuando otra observación comprueba su visibilidad. Las relecturas acotadas durante una carga tampoco repiten la mutación anterior.

Se mantiene la distinción entre `MORE_CANDIDATES`, que pagina el inventario ya observado, y el scroll, que permite descubrir contenido nuevo mediante una acción explícita. No hay continuación de extracción ni soporte adicional para shadow DOM, iframes, nuevos tipos de controles o scroll horizontal en esta entrega. El contrato general de referencias y adaptadores descrito a continuación sigue siendo el diseño para las fases pendientes.

Pruebas: [test_policy_evidence.py](../../tests/test_policy_evidence.py) comprueba cabezas, identidad y presupuestos; [test_readiness.py](../../tests/test_readiness.py) comprueba observaciones durante cargas; [check_recovery.py](../../scripts/check_recovery.py), con [recovery.html](../../tests/fixtures/recovery.html), verifica un scroll de panel sin mover su vecino, invalidación previa a la entrada, descubrimiento de opciones y carga retrasada. Son pruebas locales sin modelos de pago. La aceptación real en coches.net y sus limitaciones se documentan por separado en [validación](../../docs/validation.md); fase A implementada no equivale a una tarea real completada.

## Problema funcional y evidencia

El agente solo puede elegir lo que su lector representa. Hoy se consulta el documento principal con una lista de etiquetas y roles; se exige que el centro del control esté dentro del viewport. Los desplazamientos disponibles se derivan del documento y se ejecutan en un punto fijo. Véanse [snapshot.js, líneas 24–43, 56–59 y 102–103](../../AgentBrowser/snapshot.js) y [browser.py, líneas 138–139](../../AgentBrowser/browser.py).

Ejemplos deducidos: «Continuar» puede estar dentro de un panel con scroll propio; un componente puede usar `div onclick` sin rol; un formulario puede residir dentro de un shadow root abierto o de un iframe. La extracción actual no recorre esas fronteras. Son límites identificados en código, no porcentajes de fallo medidos. Los inputs `date`, `range` y `color` tampoco reciben un rol nativo por defecto; añadir su detección no demuestra que el ejecutor pueda editarlos correctamente.

## Resultado esperado y alcance

La propuesta amplía cobertura por fases verificables. Cada control ofrecido debe conservar identidad, contexto y operaciones que el ejecutor soporte. Descubrir un elemento fuera de pantalla no lo convierte en un objetivo de clic: primero se ofrece un desplazamiento observado, se ejecuta como acción y se vuelve a observar.

No se permite que `observe` haga scroll, abra menús, cambie foco, fuerce estilos o active widgets. Se mantienen una entrada natural, Jev con operación y cabezas en una solicitud, LangGraph como controlador, texto auxiliar exclusivo de `TYPE_TEXT`, ausencia de retry de mutaciones y capturas solo para humanos.

Quedan fuera de estas entregas canvas/OCR, shadow roots cerrados, controles nativos sin operación demostrada, pestañas nuevas, carga de archivos, teclado arbitrario y adaptaciones por sitio. Los controles excluidos se podrán diagnosticar; no se anunciarán como soportados.

## Alternativas evaluadas

| Alternativa | Ventaja | Coste o límite |
| --- | --- | --- |
| Ampliar selectores del documento principal | Bajo coste para controles sencillos | No resuelve scroll interior ni fronteras DOM |
| Recorrer todo y permitir clic fuera de vista | Más candidatos aparentes | Rompe correspondencia con la entrada física; se descarta |
| Sustituir todo por árbol AX o snapshot CDP | Aporta información de otras estructuras | Migración de identidad, semántica y ejecución; no prueba accionabilidad |
| Adaptadores incrementales por contexto | Aísla riesgos y conserva contratos | Requiere matriz explícita de capacidades y gestión de sesiones |

Se selecciona la última alternativa, manteniendo el lector DOM actual como primer adaptador. Las demás fuentes podrán enriquecerlo después, sin tratar sus nodos como intercambiables automáticamente.

## Contratos propuestos

Introducir una referencia interna `ElementRef={session_scope, frame_id, document_epoch, node_id}`. `session_scope` identifica la sesión propietaria; `frame_id`, el frame; `document_epoch` cambia con el documento/contexto y `node_id` identifica el objeto dentro de esa generación. El registro añadirá sesión CDP concreta y contexto de ejecución, sin enviarlos como instrucciones al modelo.

Un ID de frame o un número de contexto reutilizado no bastarán para ejecutar. Navegación, detach o destrucción de contexto invalidarán todas sus referencias. La asignación de época pertenecerá al controlador, apoyada en eventos y evidencia del documento, no exclusivamente en URL. Los índices cortos que recibe Jev seguirán siendo temporales por observación y resolverán exactamente una referencia vigente.

Cada adaptador devolverá candidatos, regiones desplazables, límites de cobertura y diagnósticos de recursos omitidos. Una región tendrá referencia, nombre derivado de contexto, ejes y direcciones con recorrido pendiente. Las regiones se agruparán y presupuestarán conforme a [CS01](01_presupuesto_y_recorte.md); los datos de contexto seguirán [CS02](02_contexto_semantico.md).

### Continuación de extracción y ventanas de CS01

CS01 define cómo Jev pide `MORE_CANDIDATES` para consultar ventanas posteriores del inventario observado. CS04 define cómo obtener elementos aún no inspeccionados dentro de un contexto soportado. Son dos avances distintos: cambiar de ventana no desplaza la página ni amplía por sí solo las capacidades del lector.

Un adaptador que interrumpa una lectura antes de agotar el presupuesto acumulado podrá devolver un cursor local de continuación, vinculado a sesión, documento, contexto y versión de inventario. Solo se anunciará continuación disponible si puede validarse y reanudarse sin entradas de navegador. El modelo no construye ni recibe ese cursor. Al reanudar, deduplicar por la identidad completa de acción de CS01 —referencia, operación e identidad de opción para `SELECT`—, preservar el orden determinista, contabilizar todo el trabajo de inspección y aplicar CS03, CS06, CS02 y CS01 a los candidatos añadidos. Una lectura parcial no publica totales exactos desconocidos.

Los límites acumulados de CS01 no se renuevan con cada lectura o ventana. Al agotarlos, perder la validez del cursor o encontrar una frontera no soportada, declarar la causa y la cobertura incompleta; no repetir indefinidamente la misma extracción ni prometer que aparecerá el elemento. Un cambio relevante obliga a reobservar y descartar referencias caducadas, conservando el gasto global de la tarea. Cada adaptador deberá demostrar con pruebas su contrato de continuación antes de anunciarlo como disponible; mientras no lo tenga, CS01 solo pagina los candidatos ya observados.

Si el siguiente elemento requiere desplazar un panel o abrir un menú, la continuación no lo hace silenciosamente. Jev debe elegir una operación de navegador compatible, ejecutarla una sola vez y volver a observar. Las comprobaciones ordinarias de frescura tampoco avanzan cursores ni ventanas.

## Plan incremental

### Fase A: scroll interior y frontera del viewport

Detectar regiones cuyo contenido desborde su área cliente y cuyo overflow admita desplazamiento, además del documento. Exigir una zona visible y alcanzable con el hit test de [CS03](03_ejecutabilidad_y_oclusiones.md). No ofrecer un scroll si ya se está en el límite de esa dirección.

Con varias regiones, `SCROLL_DOWN` y `SCROLL_UP` necesitan cabezas de objetivo propias; hoy son controles globales sin target. El nuevo contrato unificará documento y regiones bajo índices observados, con dirección y desplazamiento acotado definidos por código. `model.py` añadirá la cabeza a la misma solicitud. El contrato nativo y su correspondencia con el ejecutor se verificarán offline antes de habilitarlo.

El ejecutor resolverá la región vigente y un punto interior congruente antes de emitir una rueda CDP. Tras una única ejecución, registrará y observará: no repetirá el gesto porque el scroll no se haya movido. La falta de movimiento será evidencia. Los controles parcialmente visibles cuyo centro siga fuera no se ofrecerán como clic hasta que una observación posterior cumpla CS03. No se llamará silenciosamente a `scrollIntoView`.

### Fase B: semántica adicional limitada

Crear una matriz explícita de etiqueta/rol, operaciones y pruebas de ejecución. Primero incorporar controles que pueden operarse por clic ordinario y poseen nombre suficiente. Para `div onclick`, aceptar inicialmente solo un manejador directo observable más nombre y elegibilidad; ni `cursor:pointer` ni `tabindex` serán pruebas suficientes por sí solos.

El atributo o propiedad `onclick` no cubre listeners delegados ni todos los añadidos con `addEventListener`. Esa limitación se documentará. Una futura consulta CDP de listeners requerirá presupuesto y pruebas; no se inspeccionará ni ejecutará código de la aplicación para inventar objetivos. Los tipos `date`, `range` y `color` quedarán como cobertura diagnosticada hasta disponer de una operación compatible probada; no se mapearán automáticamente a `TYPE_TEXT`.

### Fase C: shadow DOM abierto

Recorrer raíces abiertas con un registro de objetos compartido por documento y un conjunto de visitados para evitar duplicados, especialmente en slots. Resolver etiquetas dentro de la raíz correcta y conservar el contexto del host. La enumeración debe acompañarse de hit tests que atraviesen hosts y raíces abiertas, así como comprobaciones de ancestros compuestos, visibilidad y clipping.

Un nodo interno no será clicable solo porque se haya enumerado: el ejecutor debe resolver la misma referencia y demostrar una ruta alcanzable desde el documento superior. Los cambios o reemplazos de host invalidarán referencias afectadas. Shadow DOM cerrado y controles internos del navegador permanecen fuera de alcance, aunque ciertas APIs diagnósticas puedan describirlos.

### Fase D: frames y procesos

Comenzar con iframes del mismo origen y geometría sin rotaciones ni escalados complejos. Asociar cada documento a su frame, recortar por viewport y cadena de frames y traducir el punto al espacio de coordenadas del target que recibe la entrada. Verificar que ningún ancestro cubra al iframe. Frames no visibles no aportarán clics accionables.

Después, habilitar frames de otro origen y fuera del proceso principal solo con una prueba de capacidad del transporte. CDP documenta [Page.getFrameTree](https://chromedevtools.github.io/devtools-protocol/tot/Page/), contextos en [Runtime.evaluate](https://chromedevtools.github.io/devtools-protocol/v8/Runtime/) y sesiones planas en [Target](https://chromedevtools.github.io/devtools-protocol/tot/Target/). Su existencia no acredita que la integración actual de Browser Harness gestione todos sus eventos: esa compatibilidad deberá verificarse.

El controlador deberá asociar sesiones hijas exclusivamente al árbol del target propietario, mantener altas/bajas y evaluar el lector en el contexto identificado. Si falta contexto, transformación fiable o sesión, se reportará cobertura incompleta sin inventar candidatos. No se ampliarán permisos del frame ni se relajará su política para completar una tarea. La documentación [DOM](https://chromedevtools.github.io/devtools-protocol/tot/DOM/) ofrece exploración de subárboles con `pierce`; será una alternativa diagnóstica, no una garantía automática de ejecución. Referencias consultadas el 16-10-2026; capacidades dependientes de versión.

## Cambios y riesgos

Cambiarían [snapshot.js](../../AgentBrowser/snapshot.js), [browser.py](../../AgentBrowser/browser.py), [model.py](../../AgentBrowser/model.py), [questions.py](../../AgentBrowser/questions.py) y la serialización de [agent.py](../../AgentBrowser/agent.py). El inspector necesitaría representar procedencia y región sin exponer internals CDP como instrucciones de usuario. LangGraph conservará el recorrido observar/elegir/ejecutar/publicar y la recuperación sin replay.

Los riesgos incluyen explosión de candidatos, documentos que cambian durante una observación conjunta, IDs reutilizados, errores de coordenadas, scroll que alcanza al ancestro y exposición de texto de más contextos. Se limitarán profundidad, número de frames y tiempo de extracción, declarando siempre qué quedó omitido. No se afirmará atomicidad entre documentos; la frescura final validará el frame objetivo y su cadena relevante.

## Aceptación y pruebas sin modelos

Cada fase debe pasar antes de activar la siguiente:

- Panel local: el botón fuera de vista no aparece como clic; una decisión simulada de scroll mueve una región concreta; solo la reobservación lo incorpora.
- Dos paneles: sus cabezas de scroll contienen referencias distintas; nunca se consume el target de otra operación.
- Roles y eventos: matriz de controles permitidos y excluidos, incluidos falsos positivos con cursor y `tabindex`; cada candidato tiene al menos una operación ejecutable comprobada.
- Shadow abierto con slots: cero duplicados de identidad; overlay superior bloquea entrada; reemplazo de host invalida el objetivo viejo.
- Iframes locales: mismo origen y, en fase D avanzada, dos orígenes loopback; navegación y detach producen cero entradas sobre referencias antiguas.
- Observación: cero cambios en foco, scroll, valores o contadores de entrada. Fallo CDP parcial: cero replay.
- Continuación de lectura habilitada: recorrido parcial reanudable aporta referencias nuevas sin duplicados ni cambios de página; un cursor obsoleto se rechaza y los límites acumulados no se reinician. Sin continuación soportada, el paginado CS01 mantiene visible la extracción incompleta.

Los tests usarán HTML local, dos servidores loopback cuando proceda y dobles de proveedores. No habrá APIs de pago. Cada fixture tendrá un manifiesto de controles esperados para medir cobertura por capacidad, no una cifra de compatibilidad universal.

## Métricas y decisiones abiertas

Medir cobertura frente al manifiesto, falsos candidatos, exclusiones por capacidad, contextos omitidos, tiempo p50/p95, solicitudes CDP, colisiones de identidad y desplazamientos sin efecto. Propuestas de aceptación: 100 % del manifiesto habilitado, cero identidad cruzada y cero entrada sobre referencias caducadas. Presupuestos de profundidad, latencia y frames se fijarán con medición local antes de cada fase.

Dependencias principales: CS01, CS02, CS03 y [CS06](06_memoria_de_intentos_rechazados.md). Quedan abiertos soporte horizontal, geometría transformada, límites de sesiones y cobertura de eventos delegados. La presencia de un índice nuevo no sustituye la verificación del resultado final ni modifica el alcance de [CS05](05_incertidumbre_en_la_seleccion.md).
