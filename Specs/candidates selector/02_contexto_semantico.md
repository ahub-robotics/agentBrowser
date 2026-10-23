# CS02 Contexto semántico de los candidatos

**Fecha:** 2026-10-16  
**Actualización:** 2026-10-19  
**Estado:** Implementación parcial: contexto local acotado por elemento  
**Área:** Representación de elementos y contexto de inferencia  
**Índice:** [Propuestas del selector](README.md)

## Implementación disponible y alcance pendiente

El lector incorpora `context={id, kind, heading}` a los controles y las regiones desplazables observados. El encabezado tiene un máximo de 160 caracteres. Jev recibe ese contexto en el elemento y en su cabeza de objetivo, dentro del cuerpo exacto presupuestado por CS01. No recibe selectores ni guardas completas. Los formularios, diálogos y otros contenedores semánticos permiten distinguir controles con la misma etiqueta.

`field_context` también incorpora este contexto, el feedback de verificación y los efectos recientes. La reutilización de texto generado compara toda esa entrada. Los cambios de contexto o feedback invalidan las solicitudes de decisión preparadas. La evidencia factual conserva referencias ligadas al documento y al estado observado; una región que deja de observarse no demuestra una confirmación correcta.

El inventario conserva además el `href` absoluto observado de los enlaces, limitado a 2000 caracteres y con `href_truncated` cuando corresponde. El revisor puede contrastarlo con el destino observado después de navegar. La fuente conserva como máximo 16 hechos históricos, con posición entre controles observados; no afirma un orden global de resultados. Esta representación no equivale al descriptor minimizado `href_hint` propuesto más abajo.

Esta entrega no implementa el mapa compartido `element_contexts` y sus `context_ref`, los descriptores `href_hint`, la redacción propuesta ni toda la detección de ambigüedad, truncamiento y procedencia descrita a continuación. Por ello CS02 permanece parcialmente implementada. Las secciones siguientes conservan el diagnóstico y el diseño objetivo originales; sus campos propuestos no deben confundirse con el contrato disponible.

Regresiones: [test_policy_evidence.py](../../tests/test_policy_evidence.py), [test_verification_workflow.py](../../tests/test_verification_workflow.py) y la comprobación con Chrome y HTML local [check_recovery.py](../../scripts/check_recovery.py). Cubren etiquetas repetidas, contexto del auxiliar, invalidación de paquetes y diferencias observadas. No acreditan resolución universal de ambigüedades ni éxito completo en coches.net; los resultados de ejecución se registran en [validación](../../docs/validation.md).

## Problema funcional y fundamento

Una página puede mostrar varias tarjetas con un botón «Abrir», o una tabla con varios controles «Editar». El texto global contiene nombres y atributos de todos los resultados, pero el candidato puede conservar solo su etiqueta genérica. Un objetivo como «abre el informe de la fila indicada» exige relacionar el botón con su fila. Es una dificultad deducida del contrato actual, no una tasa de error observada ni evidencia de una ejecución fallida.

En [snapshot.js](../../AgentBrowser/snapshot.js#L61), cada acción incluye nodo, rol, etiqueta, rectángulo, valor y ciertos estados. Las funciones de nombre accesible ya utilizan etiquetas y referencias ARIA. Sin embargo, la relación con una tarjeta, fila, formulario o diálogo no aparece como campo de candidato.

Existe contexto local en [cache.guard](../../AgentBrowser/snapshot.js#L47): usa el ancestro más cercano entre formulario, diálogo, artículo, lista o fila, además de `href` y hasta 6000 caracteres de `innerText`. Esa estructura sirve para comprobar frescura en [Browser.fresh](../../AgentBrowser/browser.py#L88). No se transmite íntegra al proveedor. [action_space y choose](../../AgentBrowser/model.py#L48) construyen una representación reducida sin contexto de contenedor ni destino del enlace. El texto global de página está limitado a 6000 caracteres y no establece asociaciones explícitas.

## Resultado esperado y límites

Cada candidato debe poder indicar de qué bloque visible procede, qué encabezado o etiqueta lo distingue y qué clase de destino sugiere su enlace. La información debe estar ligada al nodo observado, ser acotada y tratarse como datos no confiables.

El objetivo sigue siendo una única instrucción natural. Jev mantiene la decisión final de operación y objetivo en una solicitud; no se introduce otro planificador, reglas por sitio ni un clasificador de negocio. Los identificadores DOM y código pertenecen al ejecutor. No se utilizan capturas como entrada del modelo. LangGraph sigue controlando las rutas, las mutaciones no se reintentan y el registro de ejecución precede a la observación posterior.

No se pretende resolver toda ambigüedad, deducir relaciones visuales arbitrarias ni garantizar anonimización. Tampoco se incorporan shadow DOM, iframes o documentos fuera del viewport; esa cobertura corresponde a [CS04](04_cobertura_y_descubrimiento.md). Tener más contexto no demuestra que un objetivo sea ejecutable ni que una tarea haya terminado.

## Alternativas

| Alternativa | Ventajas | Costes o riesgos |
| --- | --- | --- |
| Ampliar texto global | Implementación sencilla | Más ruido y datos, sin relación explícita con cada botón |
| Copiar la guarda completa | Contexto ya disponible | Expone texto excesivo, campos y URL sin política de minimización; se descarta |
| Concatenar todo al nombre del elemento | No añade estructura | Mezcla nombre accesible con procedencia, dificulta caché y diagnóstico |
| Descriptor estructurado y acotado | Relaciones auditables, presupuesto y privacidad explícitos | Requiere reglas genéricas de extracción y tratamiento de ausencia |

Se recomienda el descriptor estructurado. La guarda local y el contexto del proveedor tendrán contratos distintos: uno verifica vigencia; el otro aporta significado limitado. CS02 opera tras observación CS04, elegibilidad CS03 y supresión CS06; comparte el empaquetado con CS01, antes de Jev, validación y gate CS05. No rescata candidatos descartados anteriormente.

## Contrato propuesto

Cada elemento recibe `context_ref`, vinculado a un mapa `state.element_contexts`. Un descriptor compartido evita repetir el mismo texto para todos los botones del bloque. Sus campos propuestos son:

```json
{
  "context_ref": "c4",
  "kind": "row",
  "heading": "Informe trimestral",
  "labels": ["Región norte", "Publicado"],
  "source": "observed_visible_dom",
  "truncated": false,
  "redacted": false
}
```

Los ejemplos son ilustrativos, no etiquetas incorporadas al algoritmo. El descriptor del enlace es independiente: `href_hint` admite `scheme`, `origin_relation`, `destination_class`, `path_depth` y extensión de una lista cerrada de tipos de documento. Ningún descriptor aporta un selector ejecutable.

Como límites iniciales propuestos: un contexto principal por nodo; encabezado de 120 caracteres; hasta tres etiquetas de 80 caracteres; máximo de 512 bytes UTF-8 por descriptor. El empaquetador de [CS01](01_presupuesto_y_recorte.md) impone además el límite global del cuerpo real, contando las referencias en todas las cabezas. Si un descriptor no cabe, se omite con razón explícita; no queda una referencia colgante.

## Extracción recomendada

1. Resolver la región más cercana mediante semántica nativa o ARIA: fila, elemento de lista/artículo, formulario, diálogo. En empate manda la proximidad DOM; un ancestro más amplio no sustituye silenciosamente al más cercano.
2. Buscar un encabezado asociado por `aria-labelledby`, `legend`, encabezado de fila o heading descendiente próximo. Reutilizar resolución de nombres con protección de ciclos y límites de recorrido.
3. Recoger etiquetas o textos breves de la misma región únicamente cuando sean visibles y estén en el viewport. Excluir descendientes de controles editables, valores de formularios, contenido oculto, scripts y regiones inertes. No usar `scope.innerText` completo.
4. Eliminar duplicados, texto idéntico al nombre del candidato y espacios redundantes; aplicar redacción y límites. Mantener procedencia, omisiones y truncamiento.
5. Si no existe una región semántica, permitir un contexto local del padre con recorrido acotado; declararlo `kind=local`, sin afirmar que se ha descubierto una tarjeta.

El recorrido tendrá máximo de ocho ancestros y 64 nodos textuales por contexto como propuesta inicial. La selección será determinista; no examinará clases CSS o marcas comerciales. Un bloque con varios headings incompatibles se marcará ambiguo y podrá omitir el encabezado, en lugar de inventar una relación.

Los IDs `c4` son locales a una observación, no identidad duradera. Las cabezas de objetivos incluirán el mismo `context_ref` que `elements`; las instrucciones de Jev explicarán cómo resolverlo en el mapa compartido. Debe probarse esta forma con respuestas simuladas antes de cualquier evaluación de proveedor.

## Enlaces y privacidad

Resolver `href` con el parser URL del navegador, pero **no transmitir su cadena original**. Descartar credenciales, query, fragmento y segmentos literales del path. Por defecto, enviar relación con el origen actual, esquema HTTP/HTTPS, número de segmentos y extensión reconocida; para `mailto`, `tel`, `javascript`, `data` o URL inválida, enviar únicamente clase o ausencia. No revelar destinatarios ni código.

El hostname completo tampoco es inocuo: puede incluir identificadores de tenant. La primera versión no lo envía. Una futura opción para dominio público registrable necesitaría una política explícita y una lista de sufijos local versionada; no es requisito de esta entrega. El `href` real permanece en la guarda local y la navegación continúa mediante el nodo observado, sin reconstruir URL desde `href_hint`.

En contexto textual, excluir valores editables y aplicar redacción de patrones claros de correo, teléfono y tokens antes del recorte. Esto reduce exposición, pero no detecta todos los datos personales ni nombres propios. Registrar solo razones y conteos de redacción, nunca el valor retirado. La propuesta limita los campos nuevos; no constituye una auditoría o saneamiento total de `page.text`, URL actual e historial ya enviados por el sistema.

El contenido de página sigue siendo no confiable aunque tenga estructura. Texto como «ignora el objetivo» se entrega como dato, no como instrucciones adicionales. Las credenciales continúan exclusivamente del lado servidor.

## Cambios y coherencia con ejecución

| Módulo | Modificación propuesta |
| --- | --- |
| [snapshot.js](../../AgentBrowser/snapshot.js) | Extraer descriptores minimizados, referencias y pistas de enlace |
| [model.py](../../AgentBrowser/model.py) | Incluir mapa y referencias consistentes en elementos/cabezas |
| [questions.py](../../AgentBrowser/questions.py) | Explicar contexto, ausencia, ambigüedad y carácter no confiable |
| [browser.py](../../AgentBrowser/browser.py) | Comprobar que el descriptor asociado sigue vigente antes de actuar |
| [agent.py](../../AgentBrowser/agent.py) | Conservar versión/digest del contexto en decisiones y evidencia |
| [static/app.js](../../AgentBrowser/static/app.js) | Mostrar región, truncamiento y redacción sin contenido descartado |

El contexto no reemplaza las guardas existentes. Si una relación relevante cambia tras elegir, se invalida la decisión y se recupera mediante [workflow.py](../../AgentBrowser/workflow.py); nunca se reproduce la mutación. Se comparan descriptores normalizados, no IDs efímeros de contexto.

Inicialmente el auxiliar textual conserva su entrada actual: enriquecer Jev no amplía automáticamente los datos del helper. Si posteriormente se añade contexto a `field_context`, debe incorporarse a su entrada completa y, por tanto, a la igualdad que permite reutilizar `pending_text`. No se admite reutilizar texto ignorando un cambio semántico enviado al auxiliar.

## Aceptación y pruebas locales

1. Dos filas con botones idénticos generan referencias diferentes y encabezados asociados a su propia fila, sin texto de la vecina.
2. Formulario, diálogo anidado, artículo y padre genérico producen procedencia correcta y resultados deterministas.
3. Un fixture con campos ocultos, password, valores editables y URL con credenciales/query/fragmento no expone esos valores en los campos nuevos.
4. Contextos extensos respetan límites y declaran truncamiento; el empaquetado no produce referencias inexistentes.
5. Cambiar el contexto después de predecir impide ejecutar; no dispara un retry de mutación.
6. Un proveedor simulado recibe mapa y referencias coherentes en una sola llamada Jev; solo el objetivo de la operación elegida puede ejecutarse.

Usar fixtures HTML locales, mocks HTTP y pruebas de serialización. Los tests no llaman APIs pagadas. Medir asociación correcta en fixtures, bytes adicionales, contextos omitidos, frecuencia de ambigüedad y redacción, tiempo de extracción e invalidaciones por cambios semánticos. Separar contexto omitido de candidato perdido, con conteos por etapa. No presentar esas métricas como mejora medida de éxito de Jev.

## Dependencias, riesgos y preguntas abiertas

CS01 gobierna tamaño; [CS03](03_ejecutabilidad_y_oclusiones.md) mantiene ejecutabilidad; CS04 amplía cobertura. [CS05](05_incertidumbre_en_la_seleccion.md) consume ambigüedad y ausencia explícitas. [CS06](06_memoria_de_intentos_rechazados.md) puede usar digests normalizados, sin guardar texto privado ni asumir que una etiqueta identifica un nodo.

Quedan pendientes las cuotas finales, cómo representar múltiples encabezados y si el auxiliar necesita contexto adicional. El riesgo principal es asociar significado incorrecto o exponer más datos al intentar desambiguar. La aceptación exige procedencia verificable y minimización antes de evaluar utilidad en tareas reales.
