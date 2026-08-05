# Herramienta Automatizadora de Solicitudes de Pago · SIPP

Aplicación de escritorio para preparar, revisar y capturar de forma masiva
solicitudes de pago en el ERP **SIPP**, mediante un RPA que opera su interfaz
web. Cubre el tipo **Pago Extraordinario** (beneficiario Proveedor, Deudor o
Acreedor).

| Documento | Qué define |
|---|---|
| [ESPECIFICACION.md](ESPECIFICACION.md) | Qué hace la herramienta, el modelo de datos y el mapa de selectores de SIPP |
| [ARQUITECTURA.md](ARQUITECTURA.md) | Estándar de las herramientas Quetzaltic: estructura, rutas, secretos, distribución |
| [DISENO.md](DISENO.md) | Sistema visual (paleta, tipografía, densidad), aterrizado en `ui/tema.py` |

---

## Poner a andar el proyecto

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium   # solo cuando llegue el motor RPA
python app.py
```

Verificación de que nada quedó roto:

```powershell
python scripts/probar.py              # las pruebas de lógica (lo primero que corres)
python scripts/smoke_import.py        # imports (lo exige el CI)
python scripts/smoke_render.py        # abre la app y pinta cada pantalla
python scripts/prueba_rpa_fixtures.py # el mapa de selectores vs. las páginas reales
```

`probar.py` corre las suites de [scripts/pruebas/](scripts/pruebas/) —datos,
ingesta, catálogo, asignación e interfaz— sin abrir ventana ni tocar SIPP, con
una base temporal por prueba. Acepta un filtro por nombre y `-v` para ver el
detalle de las que fallen:

```powershell
python scripts/probar.py asignacion -v
```

Cubren la lógica, no el dibujado: que la pantalla se vea es trabajo de
`smoke_render.py`, y que el robot haga lo que dice, de las pruebas contra stage.

Contra el SIPP de pruebas (leen, no capturan):

```powershell
python scripts/explorar_sipp.py         # desplegables y conceptos por empresa
python scripts/prueba_llenado_stage.py  # llena una solicitud SIN guardarla
python scripts/importar_conceptos.py    # siembra el catálogo de conceptos
```

`prueba_guardado_stage.py` existe también, pero **sí guarda**: consume un folio
en pruebas. Úsalo solo cuando quieras revalidar el guardado completo y la
salvaguarda de idempotencia.

Corre **los cuatro** antes de publicar. El de imports no basta, porque un control
se construye sin quejarse en Python y aun así puede fallar más tarde, de dos
formas que no dejan rastro en la consola:

- **Ventana en blanco** — un valor que no se puede serializar (un `set` donde
  Flet espera una `list`) tumba el `page.update()` completo y no llega nada al
  cliente. `smoke_render.py` lo caza porque sí serializa.
- **Rectángulo gris** — un error de layout de Flutter, como un hijo con `expand`
  dentro de un `Row(wrap=True)`. Ese error se queda del lado de Flutter y nunca
  vuelve a Python, así que ni pintando la pantalla se detecta: `smoke_render.py`
  lo busca revisando el árbol de controles.

Necesita sesión de escritorio (abre una ventana unos segundos), por eso no está
conectado al CI.

## Configuración

Todo se captura desde el botón ⚙ de la barra superior:

- **Credenciales SIPP** — se guardan cifradas con DPAPI, atadas a tu cuenta de
  Windows. Nunca en claro ni en el repositorio.
- **Ambiente** — `PRUEBAS` (stage) o `PRODUCCION`. El indicador del encabezado
  lo muestra siempre; en producción se pinta en rojo, porque ahí cada solicitud
  guardada consume un folio real.
- **Navegador** — visible por defecto, para poder seguir el trabajo del robot.

El PAT de GitHub del auto-updater va aparte, en la variable de entorno
`QUETZALTIC_GITHUB_PAT` (ver [.env.example](.env.example)).

## Estructura

```
app.py            Shell: ventana, tema, navegación, auto-updater
core/             Backend (no importa Flet)
  catalogos.py    Catálogos reales de SIPP (extraídos del DOM del portal)
  db.py           SQLite: lote · solicitud · partida · documento · bitácora
  validador.py    Reglas previas a encolar
  rpa_sipp.py     (fase 2) SesionSipp + flujo de solicitud de pago
ui/               Una pantalla por archivo; cada una expone `.contenido`
  solicitudes.py  Tabla maestro-detalle del lote  ← pantalla principal
  documentos.py   (fase 3) ingesta de CFDI / Excel / anexos
  bitacora.py     Historial y evidencias
  configuracion.py  Modal de configuración
scripts/          Verificación y utilidades
  probar.py       Corredor de las pruebas de lógica
  pruebas/        Las suites: datos · ingesta · catálogo · asignación · interfaz
```

Los datos en runtime (base, preferencias, credenciales, navegador) **nunca** se
escriben junto al `.exe`: van a `%LOCALAPPDATA%\Quetzaltic Solutions\Herramientas
de Solicitudes de Pago`. En desarrollo van a la raíz del proyecto.

## Estado

| Fase | Entregable | Estado |
|---|---|---|
| 1 | Shell + SQLite + tabla maestro-detalle con captura manual | **Listo** |
| 2 | Port del RPA (`sipp_rpa.py`) contra fixtures locales | **Listo** |
| 3 | `ExcelAdapter` (carga masiva), `CfdiAdapter` y validador | **Listo** |
| 4 | Ejecución completa validada contra el ambiente de pruebas | **Listo** |
| 5 | Asignación masiva con vista previa y deshacer | **Listo** · falta validar el grid de Insumos en stage |
| 6 | Ejecución contra producción con parada en `LLENADA` | Pendiente |
| 7 | CI, instalador, firma de código y actualización automática | Pendiente |

Lo que ya se puede hacer hoy:

- Crear lotes y capturar solicitudes a mano con su desglose de conceptos e
  insumos; revisarlas, duplicarlas, omitirlas y fijar el punto de parada.
- **Alta desde carátulas**: cada archivo de una carpeta se vuelve una solicitud,
  con el beneficiario tomado del nombre del archivo y la carátula ya adjunta;
  después el Excel completa el resto emparejando por nombre.
- **Carga masiva desde Excel**: descargar la plantilla, llenarla, y revisar la
  vista previa fila por fila antes de importar.
- **Adjuntar carátulas y Vo.Bo.**, uno por uno en el formulario o una carpeta
  entera emparejando por el nombre del beneficiario.
- **Verificar la conexión con SIPP** desde Configuración ⚙: entra al portal y
  comprueba que el formulario responda.
- **Catálogo de conceptos de pago** (pestaña Conceptos): importarlos de SIPP y
  agregarlos a mano. Alimentan los desplegables del Excel y de la captura.
- **Asignación masiva**: poner el mismo concepto o insumo a todo el lote, con
  vista previa del antes/después fila por fila y **Deshacer**.
- **Ejecutar el lote**, con confirmación, barra de progreso y botón de detener.

**Validado de punta a punta contra el SIPP de pruebas:** login, sesión,
navegación, los desplegables «chosen», el alta de beneficiario con su cuenta
bancaria y su carátula, el grid de conceptos con el importe correcto,
**Guardar** con lectura de folio, adjuntar el **Vo.Bo.**, **Solicitar
Autorización** (la solicitud pasó de `BORRADOR` a `PENDIENTE`) y la salvaguarda
de idempotencia.

> ⚠️ **Ninguna corrida se ha hecho contra producción.** La primera debe ser en
> **PRUEBAS**, con punto de parada **«Llenar y esperar»** y una sola solicitud.
>
> Para llegar hasta la autorización hace falta el **Vo.Bo. de Compras**: SIPP no
> envía un Pago Extraordinario sin ese documento. Si falta, la solicitud se
> queda guardada y la herramienta explica por qué.

## Publicar una versión

1. Merge a `main`.
2. Crear un **Release** en GitHub con un tag **mayor** que el anterior (`0.2.0`).
3. El CI ([.github/workflows/compilar.yml](.github/workflows/compilar.yml))
   sincroniza la versión con el tag, corre el smoke test, compila con
   `flet pack`, arma el instalador con Inno Setup y sube
   `Instalador_SolicitudesPago.exe` como asset.

Editar un Release viejo **no** dispara el build: hay que publicar uno nuevo.
