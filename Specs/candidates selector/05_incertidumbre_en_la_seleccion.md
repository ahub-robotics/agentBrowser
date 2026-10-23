# CS05 Incertidumbre en la selección

**Fecha:** 2026-10-16 · **Estado:** Propuesta sin implementar · **Ámbito:** decisión sobre candidatos observados.

**Actualización:** 2026-10-19 · Clasificación de la reconsulta propuesta en CS01.

Esta especificación propone separar una respuesta estructuralmente válida de una decisión suficientemente determinada para continuar. No modifica todavía AgentRun. Forma parte del [índice de especificaciones](README.md) y conserva las invariantes del [proyecto](../../AGENTS.md).

## Problema funcional y evidencia

El selector acepta una respuesta cuyo `choice` corresponde al máximo de su distribución. Esa condición no distingue una preferencia clara de un empate. Tampoco acredita que la opción sea correcta, que exista un candidato apropiado o que el objetivo esté cumplido.

Ejemplo deducido del contrato: una página ofrece dos botones observados con el mismo nombre. Jev devuelve `CLICK` y asigna `0.50` a cada objetivo. La elección de cualquiera supera la validación y puede llegar al ejecutor. Si ambos botones corresponden a acciones funcionales distintas, la ambigüedad puede producir una acción válida sobre el elemento equivocado. Es un caso reproducible con dobles; no se afirma haber observado ese incidente en una ejecución real.

En [model.py](../../AgentBrowser/model.py), `validate_choice` comprueba pertenencia, claves exactas, números finitos entre cero y uno, suma cercana a uno y máximo con tolerancia `1e-6`. Comprueba el rango de `confidence`, pero no su calibración ni su relación con el éxito. `choose` valida la operación y únicamente el objetivo de esa operación. [Agent._choose y Agent._execute](../../AgentBrowser/agent.py) conservan las distribuciones y ejecutan la selección; el [grafo](../../AgentBrowser/workflow.py) no incorpora una política diferenciada de incertidumbre.

## Comportamiento actual y esperado

| Situación | Actual | Propuesto al habilitar la política |
| --- | --- | --- |
| Respuesta mal formada | Rechazo por validación | Mismo rechazo; no es incertidumbre |
| Máximo único | Selección ejecutable con guardas | Evaluación de política sin reinterpretarlo como éxito |
| Empate en un objetivo mutable | Cualquiera de los máximos es válido | Pausa explícita para revisión |
| Empate con `WAIT` seleccionado | Puede esperar | Puede mantener esa inspección acotada |
| `DONE` empatado | Puede terminar | No aceptar automáticamente la conclusión |
| Lista de un candidato | Distribución necesariamente concentrada | Registrar que no hubo alternativas, sin garantía de suficiencia |

El objetivo es impedir que la validez matemática o una etiqueta de confianza se presenten como certeza operativa. También se busca que la pausa sea explicable y comprobable. Quedan fuera la calibración estadística en sitios reales, un segundo modelo, nuevas llamadas LLM, planes por sitio, selección automática de una segunda opción y un verificador universal del objetivo.

## Alternativas comparadas

| Alternativa | Ventaja | Problema |
| --- | --- | --- |
| Conservar solo `argmax` | Simplicidad actual | No representa ambigüedad |
| Umbral fijo de `confidence` | Implementación corta | No está calibrado y puede inducir falsa certeza |
| Multiplicar confianza de operación y objetivo | Produce una cifra | Supone una independencia no demostrada |
| Consultar otro modelo o repetir la pregunta | Añade otra opinión | Más coste y llamadas; no resuelve la validación del resultado |
| Política determinista separada | Razón auditable, sin inferencia adicional | Requiere estados nuevos y tratamiento explícito por los clientes |

Se elige la última alternativa. Ningún valor como `0.8` se denominará probabilidad de éxito. Tampoco se combinarán heads por multiplicación ni se examinarán objetivos de operaciones no elegidas para justificar la ejecución.

## Contrato y algoritmo propuestos

Un evaluador puro recibiría la decisión validada, los candidatos efectivamente enviados, la identidad de la observación y una política versionada. Devolvería:

```text
SelectionAssessment:
  decision_id, observation_id, policy_version
  operation_summary, selected_target_summary | null
  operation_class
  disposition: allow | needs_review
  reason_codes[]
```

Cada resumen incluiría número de opciones, máximo, segundo valor cuando exista, margen, número de máximos dentro de la tolerancia y `confidence` original. Puede incluir entropía normalizada, calculada sobre la distribución normalizada por su suma, exclusivamente como diagnóstico. Para una opción, segundo valor y margen serían `null`; la entropía convencional cero no significaría certeza. La distribución entre veinte opciones no se compararía mediante un umbral universal con otra entre dos.

El procedimiento sería:

1. Ejecutar las validaciones actuales, sin relajarlas ni convertir sus errores en una revisión humana.
2. Resolver la operación y su único head pertinente contra los candidatos de esa solicitud.
3. Calcular resúmenes independientes. Usar la tolerancia existente para empates numéricos; no confundirla con un umbral de seguridad.
4. Clasificar el tipo de operación y aplicar la política a la selección original.
5. Publicar evaluación y razón. Si se permite ejecutar, conservar la elección original y todas las comprobaciones de frescura y ejecutabilidad.

La política inicial propuesta distingue:

| Clase | Regla ante empate relevante | Límite |
| --- | --- | --- |
| Consulta de inventario: `MORE_CANDIDATES` de CS01 | Permitir la operación seleccionada dentro del presupuesto y con continuación válida | Sin target ni entrada de navegador; no garantiza encontrar el elemento |
| Inspección: `WAIT`, scroll | Permitir la operación seleccionada dentro del presupuesto | Puede disparar carga; no demuestra progreso |
| Mutación: `CLICK`, `TYPE_TEXT`, `SELECT` | `needs_review` si operación o objetivo están empatados | No sustituir por el segundo candidato |
| Conclusión: `DONE`, `BLOCKED` | `needs_review` si la operación está empatada | `DONE` único sigue sin verificar éxito |

Esta clasificación describe la operación elegida, no sus consecuencias de negocio. Un scroll puede disparar carga y un clic puede tener efectos externos; el nombre de la operación no demuestra reversibilidad. La política propuesta se limita a la ambigüedad de selección y no introduce reglas de autorización por sitio ni revisiones adicionales para máximos únicos.

El gate no transforma una decisión ambigua de `CLICK`, `TYPE_TEXT` o `SELECT` en `MORE_CANDIDATES`. La reconsulta solo ocurre si Jev la eligió en su decisión normal; se evalúa la distribución de operación sin consumir cabezas ajenas. Las nuevas llamadas para ventanas sucesivas pertenecen a CS01, no son reintentos ni inferencias añadidas por este evaluador.

Los márgenes pequeños sin empate se registrarían, pero esta primera versión no definiría arbitrariamente qué margen resulta seguro. Una política futura podría incorporar bandas obtenidas de evaluación independiente y versionadas por clase; no se activarían como supuesta probabilidad de éxito. Este límite debe quedar visible: la propuesta reduce ambigüedad explícita, no identifica todos los errores de selección.

## Integración, estados y errores

[workflow.py](../../AgentBrowser/workflow.py) incorporaría un nodo de evaluación después de `choose`. En `predict` publicaría la evaluación sin actuar; en `run` y `tick` decidiría entre `execute` y publicación de `needs_review`. La ruta `act` también pasaría por la evaluación para impedir un bypass. `publish` finalizaría el recorrido automático ante revisión pendiente.

`needs_review` sería una pausa, diferente de `done`, `blocked` y un error de proveedor. Este gate publicaría `review_kind=selection`, con una decisión aún no consumida. `tick` y `run` sobre esa pausa publicarían el mismo estado y finalizarían sin volver a decidir. El cliente mostraría razón y selección. La continuación mediante `act` exigiría `review={decision_id, policy_version, approved: true}` y el fingerprint actual; esos campos proceden del operador, nunca de Jev. Se revalidarían política y contexto; una decisión consumida no podría reutilizarse. Si la página cambió, se descartaría la aprobación y haría falta una nueva decisión ordinaria, con su presupuesto habitual. El gate no solicita inferencias adicionales. Las pausas posteriores a ejecución de CS06 usarían `review_kind=execution` y no admitirían este mecanismo de `act`.

[agent.py](../../AgentBrowser/agent.py) almacenaría la evaluación; [model.py](../../AgentBrowser/model.py) mantendría separado el validador; [demo.py](../../AgentBrowser/demo.py), [app.js](../../AgentBrowser/static/app.js) y [cli.py](../../agentrun/cli.py) reconocerían `needs_review`. Se propone salida CLI `3` para esa pausa. La configuración propuesta `selection_policy` tendría versión y modo `off`, `observe` o `enforce`, pasado al constructor y fijado al arrancar inspector/CLI. El despliegue inicial usaría `observe`, sin alterar ejecución; `enforce` activaría los gates explícitamente. Una petición HTTP no podría rebajar el modo del servidor.

## Privacidad y retención

La evaluación no duplicaría texto de página, valores introducidos ni credenciales. Conservaría identificadores locales, métricas y razones durante la ejecución, con el mismo límite de decisiones. Las exportaciones deberían explicar que las trazas existentes pueden contener datos sensibles. No se añadiría telemetría remota ni persistencia entre tareas.

## Aceptación y pruebas sin APIs

Las pruebas usarían distribuciones sintéticas, navegador falso y proveedores sustituidos. Deberían demostrar:

- Empates exactos y próximos dentro de tolerancia producen revisión para mutaciones, aunque `confidence=1`.
- Cambiar un head no seleccionado no altera evaluación ni ejecución.
- Listas de 1, 2, 20 y 200 opciones conservan la semántica y no activan un umbral universal implícito.
- `WAIT` seleccionado puede avanzar bajo empate sin cambiar la elección; `DONE` empatado no finaliza.
- `MORE_CANDIDATES` seleccionado puede avanzar bajo empate dentro de sus límites; una mutación empatada conserva su revisión y no se convierte automáticamente en reconsulta.
- `act` no evita el gate; una revisión aprobada sobre contexto obsoleto no emite input.
- Una revisión no consume otra llamada LLM ni invoca `TextValue`; la escritura aprobada sigue utilizando el helper normal.
- Errores de distribución permanecen errores; ejecución consumida no se repite.

Las métricas serían revisiones por clase y razón, tamaño de candidatos, márgenes, decisiones descartadas por cambio y resultados verificados externamente. El contador de revisiones no sustituiría la tasa de éxito. La incertidumbre causada por recorte depende de [CS01](01_presupuesto_y_recorte.md); los nombres y contexto, de [CS02](02_contexto_semantico.md); la ejecutabilidad, de [CS03](03_ejecutabilidad_y_oclusiones.md); la cobertura, de [CS04](04_cobertura_y_descubrimiento.md). [CS06](06_memoria_de_intentos_rechazados.md) registraría las decisiones retenidas sin convertirlas en acciones fallidas.

Quedan abiertos el contrato definitivo de continuación y la evaluación necesaria para cualquier banda de margen futura.
