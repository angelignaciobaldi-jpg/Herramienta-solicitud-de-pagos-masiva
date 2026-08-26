# Solicitudes de pago masivas · SIPP

Especificación técnica y de diseño de la **Herramienta Automatizadora de
Solicitudes de Pago**. Documento vivo: se actualiza conforme se cierran los
pendientes de la sección final.

> **Complementa a:** [ARQUITECTURA.md](ARQUITECTURA.md) (cómo se estructura el
> código) y [DISENO.md](DISENO.md) (cómo se ve la interfaz). Donde este documento
> calle, mandan aquellos.
>
> **Antecedente:** el RPA de un solo uso `RPA Solicitud de pagos/sipp_rpa.py`
> (Python + Playwright), que ya corrió contra SIPP producción registrando pagos
> a ex-colaboradores. Esta herramienta lo generaliza y le pone interfaz. Los
> selectores y *workarounds* de la sección 8 vienen de ahí: están probados
> contra el sistema real, no deducidos del HTML.

---

## 1. Objetivo

Aplicación de escritorio que permite preparar, revisar y capturar de forma masiva
solicitudes de pago en el ERP SIPP, usando un RPA que opera la interfaz web del
sistema.

El usuario arma un lote a partir de documentos (CFDI, plantillas de Excel) o de
captura manual, lo revisa en una tabla, y lanza la ejecución. El RPA captura cada
solicitud en SIPP y se detiene en el punto que el usuario haya definido.

## 2. Alcance

Dentro de alcance:

- Ingesta de documentos: PDF, XML (CFDI), Excel, carpetas completas.
- Captura manual de solicitudes dentro de la app.
- Normalización y validación previa a la ejecución.
- Tabla maestro-detalle interactiva para revisión y ajuste.
- Motor RPA sobre la interfaz web de SIPP.
- Punto de parada configurable por lote y por solicitud.
- Configuración de credenciales de SIPP.
- Bitácora con evidencias y exportación de resultados.
- Actualización automática desde GitHub Releases.

**Tipo de pago cubierto: únicamente `Pago Extraordinario`**,
con sus tres tipos de beneficiario: Proveedor, Deudor y Acreedor. Es el único
tipo con código probado, y el único que expone las pestañas de Insumos y
Conceptos de Pago.

Fuera de alcance (por ahora):

- Los otros tres tipos de pago del catálogo: `Anticipo` (requiere Orden de
  Compra), `Anticipo Deudores` y `Saldo de Viáticos y Gasolina`. Cada uno tiene
  su propio panel y su propio flujo; se agregarán como módulos aparte sin tocar
  el motor común.
- Consolidación centralizada de resultados entre usuarios.
- Integración por API con SIPP (no disponible; de ahí el RPA).
- Autorización de solicitudes dentro de la herramienta.

## 3. Decisiones de stack

Se adopta el **estándar Quetzaltic** de [ARQUITECTURA.md](ARQUITECTURA.md), el
mismo de las herramientas de Tesorería y Activos Fijos.

| Área | Decisión | Motivo |
|---|---|---|
| UI | **Flet** (Material, escritorio) | Estándar del equipo; [DISENO.md](DISENO.md) ya está aterrizado en `ui/tema.py` |
| Lenguaje | **Python 3.12** | El RPA probado ya está en Python; se porta casi íntegro |
| Tabla | Componente propio sobre `ui/tabla_responsiva.py` | Filas de 44px y densidad por `on_size_change`, ya resueltos en Activos Fijos |
| Persistencia | **SQLite** (stdlib) | Local, transaccional, sin servidor ni dependencias |
| RPA | **Playwright** (Chromium) | Auto-waiting, iframes limpios, `storage_state` |
| Navegador | Chromium descargado a `DATOS\ms-playwright` | No se empaqueta (≈150 MB); se baja la 1.ª vez con `asegurar_navegador` |
| OCR | **Tesseract** local + PyMuPDF | Offline: los documentos no salen del equipo |
| Secretos | **DPAPI** (`core/dpapi.py`) | Atado a la cuenta de Windows |
| Empaquetado | `flet pack` + **Inno Setup** por usuario | Sin UAC |
| Distribución | **GitHub Actions** + Releases privadas | `core/auto_updater.py` |

Notas:

- Modo **visible** por defecto en el RPA (no headless). Headless queda como
  opción avanzada en Configuración. El operador necesita ver qué pasa.
- **Se descartó Electron + Vue 3 + AG Grid** (propuesta original de este
  documento): habría obligado a reescribir en Node las ~1,300 líneas de RPA ya
  probadas contra producción, y habría dejado a este proyecto fuera del estándar
  que comparten las demás herramientas del equipo. AG Grid resolvía la tabla,
  pero la tabla es el problema barato; el RPA es el caro.
- Se descartó Tauri: el RPA quedaría como binario *sidecar*.

### Constantes de este proyecto

Conforme a la sección 10 de [ARQUITECTURA.md](ARQUITECTURA.md):

| Dónde | Constante | Valor |
|-------|-----------|-------|
| `auto_updater.py` | `NOMBRE_ASSET` | `Instalador_SolicitudesPago.exe` |
| `instalador.iss` | `OutputBaseFilename` | `Instalador_SolicitudesPago` |
| `instalador.iss` | `AppId` | `{954a532b-3b54-49ed-8971-e19617967f45}` |
| `instalador.iss` / `win_taskbar.py` | `AUMID` | `QuetzalticSolutions.HerramientasSolicitudesPago` |
| `construir.bat` / `compilar.yml` | nombre `-n` del build | `SolicitudesPago` |
| `rutas.py` | `_SUBCARPETA_DATOS` | `Quetzaltic Solutions\Herramientas de Solicitudes de Pago` |
| `db.py` | nombre del `.db` | `solicitudes_pago.db` |

`OWNER` / `REPO` apuntan al repositorio de GitHub del proyecto.

> El repositorio es **público**, así que el mapa de selectores y las URLs del
> portal viven fuera de él, en `datos/sipp.json` (ver `core/sipp_datos.py`).
> El CI repone ese archivo desde el secreto `SIPP_DATOS_JSON` al compilar.

## 4. Arquitectura

```
app.py                          # Shell: splash, tema, navegación, auto-updater
core/                           # Backend: NO importa Flet
├── rutas.py        [R]         # BUNDLE / DATOS / INSTALL
├── version.py      [R]         # versión (el CI la sincroniza con el tag)
├── entorno.py      [R]         # lectura de .env
├── dpapi.py        [R]         # cifrado atado al usuario de Windows
├── credenciales.py [R]         # credenciales SIPP cifradas
├── preferencias.py [R]         # preferencias por máquina (JSON)
├── auto_updater.py [R]         # actualización desde GitHub Releases
├── win_titlebar.py [R]
├── win_taskbar.py  [R]
├── ocr.py          [R]         # PDF con texto / rasterizado + Tesseract
├── empresas.py     [R]         # catálogo Grupo Petroil
├── rpa_sipp.py     [R/D]       # SesionSipp + flujo de solicitud de pago
├── selectores.py   [D]         # mapa de selectores (primario + respaldos)
├── adaptadores/    [D]         # cfdi.py · excel.py · manual.py → SolicitudDraft
├── validador.py    [D]         # reglas previas a encolar
├── db.py           [D]         # SQLite: lote / solicitud / partida / documento / bitacora
└── exportador.py   [D]         # Excel / CSV
ui/
├── comun.py                    # colores, helpers, CampoFecha, catálogo empresas
├── tema.py                     # DISENO.md aterrizado (fuente única de color/tipografía)
├── documentos.py               # carga y emparejamiento PDF/XML
├── solicitudes.py              # tabla maestro-detalle + toolbar + ejecución
├── bitacora.py                 # historial, capturas, exportación
└── configuracion.py            # credenciales, ambiente, verificar conexión
```

**Regla estructural:** los tres adaptadores producen exactamente el mismo DTO
(`SolicitudDraft`). El motor RPA no sabe si un registro vino de un XML, de un
Excel o de un formulario.

**Regla del shell:** cada pantalla cumple el contrato de la sección 4 de
[ARQUITECTURA.md](ARQUITECTURA.md) — recibe `app`, expone `.contenido`, y
opcionalmente `_on_resize` y `cargar_desde_db`.

**Trabajo pesado fuera del hilo de UI:** el RPA, el OCR y las descargas corren
en `asyncio.to_thread(...)`. La interfaz nunca se congela durante un lote.

## 5. Modelo de datos

SQLite en `DATOS`, con `inicializar()` que crea las tablas y aplica migraciones
incrementales por `ALTER TABLE`.

```sql
CREATE TABLE lote (
  id                TEXT PRIMARY KEY,
  nombre            TEXT NOT NULL,
  parada_default    TEXT NOT NULL,   -- 'LLENADA' | 'GUARDADA' | 'AUTORIZAR'
  ambiente          TEXT NOT NULL,   -- 'PRUEBAS' | 'PRODUCCION'
  estado            TEXT NOT NULL,   -- 'BORRADOR' | 'EJECUTANDO' | 'PAUSADO' | 'CERRADO'
  creado_en         TEXT NOT NULL
);

CREATE TABLE solicitud (
  id                  TEXT PRIMARY KEY,
  lote_id             TEXT NOT NULL REFERENCES lote(id),
  orden               INTEGER NOT NULL,
  empresa             TEXT NOT NULL,   -- nombre visible; el RPA selecciona por texto
  sucursal            TEXT NOT NULL,
  tipo_pago           TEXT NOT NULL DEFAULT 'Pago Extraordinario',
  tipo_beneficiario   TEXT NOT NULL,   -- 'Proveedor' | 'Deudor' | 'Acreedor'
  beneficiario_nuevo  INTEGER NOT NULL DEFAULT 0,   -- marca 'No Registrado'
  beneficiario_folio  TEXT,            -- ID_PROVEEDOR si ya existe en el catálogo
  beneficiario_rfc    TEXT,
  beneficiario_nombre TEXT NOT NULL,
  beneficiario_correo TEXT,
  cuenta_clabe        TEXT,            -- para emparejar la cuenta existente
  cuenta_banco        TEXT,
  cuenta_titular      TEXT,
  cuenta_tipo_transf  TEXT DEFAULT 'SPEI',
  forma_pago          TEXT NOT NULL,   -- Cheque | Transferencia | Efectivo | Linea de Captura
  tipo_gasto          TEXT NOT NULL,   -- Deducible | No Deducible | Deducible SF | Contribución
  fecha_pago          TEXT NOT NULL,   -- 'DD/MM/AAAA' (comun.FORMATO_FECHA)
  moneda              TEXT NOT NULL DEFAULT 'Pesos (MXN)',
  descripcion         TEXT,
  importe_total       REAL NOT NULL,   -- derivado; ver reglas
  origen              TEXT NOT NULL,   -- 'CFDI' | 'EXCEL' | 'MANUAL'
  parada              TEXT NOT NULL,   -- hereda de lote.parada_default
  estado              TEXT NOT NULL,
  folio_sipp          TEXT,
  clave_idempotencia  TEXT NOT NULL,
  intentos            INTEGER NOT NULL DEFAULT 0,
  error_msg           TEXT,
  actualizado_en      TEXT NOT NULL
);

-- Desglose. SIPP tiene DOS mecanismos distintos y esta tabla modela ambos.
CREATE TABLE partida (
  id                 TEXT PRIMARY KEY,
  solicitud_id       TEXT NOT NULL REFERENCES solicitud(id) ON DELETE CASCADE,
  clase              TEXT NOT NULL,   -- 'CONCEPTO' | 'INSUMO'
  orden              INTEGER NOT NULL,
  -- clase = 'CONCEPTO' (grid de Conceptos de Pago)
  concepto_nombre    TEXT,            -- se busca por palabras en el catálogo de la empresa
  -- clase = 'INSUMO' (grid de Insumos & Servicios)
  tipo_compra        TEXT,
  insumo_id          TEXT,
  insumo_nombre      TEXT,
  cantidad           REAL DEFAULT 1,
  precio_unitario    REAL,
  centro_costos      TEXT,
  cuenta_contable    TEXT,
  -- comunes
  descripcion        TEXT,
  importe            REAL NOT NULL,
  origen             TEXT NOT NULL    -- puede diferir del origen de la solicitud
);

CREATE TABLE documento (
  id           TEXT PRIMARY KEY,
  solicitud_id TEXT REFERENCES solicitud(id),
  lote_id      TEXT REFERENCES lote(id),
  tipo         TEXT NOT NULL,   -- 'CFDI_XML' | 'CFDI_PDF' | 'CARATULA' | 'VOBO' | 'EXCEL' | 'ANEXO'
  ruta         TEXT NOT NULL,
  hash_sha256  TEXT NOT NULL,
  uuid_cfdi    TEXT,
  cargado_en   TEXT NOT NULL
);

CREATE TABLE bitacora (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  solicitud_id TEXT REFERENCES solicitud(id),
  momento      TEXT NOT NULL,
  paso         TEXT NOT NULL,
  nivel        TEXT NOT NULL,   -- 'INFO' | 'WARN' | 'ERROR'
  mensaje      TEXT,
  captura_ruta TEXT
);

CREATE UNIQUE INDEX idx_idem ON solicitud(clave_idempotencia);
CREATE INDEX idx_sol_lote ON solicitud(lote_id, orden);
CREATE INDEX idx_part_sol ON partida(solicitud_id, clase, orden);
CREATE INDEX idx_doc_hash ON documento(hash_sha256);
```

Reglas del modelo:

1. **El tipo de beneficiario decide cuál de los dos desgloses aplica, y son
   excluyentes.** Verificado contra stage el 31/07/2026: SIPP muestra

   | Tipo de beneficiario | Pestaña de desglose |
   |---|---|
   | Proveedor | Insumos & Servicios (**no** hay Conceptos de Pago) |
   | Deudor | Conceptos de Pago (**no** hay Insumos) |
   | Acreedor | Conceptos de Pago (**no** hay Insumos) |

   No es una preferencia del usuario: pedirle conceptos a un Proveedor es pedir
   una pestaña que no está en pantalla. Lo fija `catalogos.CLASE_DESGLOSE`, y de
   ahí lo toman el validador, el adaptador de Excel, el formulario de captura y
   el motor.
2. **`importe_total` se deriva de la clase que aplica**, no de la suma de las
   dos. En Pago Extraordinario el campo `Cantidad a Pagar` (`#ID_CANTIDADPAGAR`)
   está **deshabilitado**: SIPP lo calcula a partir del desglose. Sumar ambas
   clases daría el doble. Si existe CFDI asociado, su total se compara y se
   marca discrepancia en lugar de sobrescribir.
3. Toda solicitud necesita **al menos un renglón de la clase que le toca**, o se
   guardaría en $0. Los renglones de la otra clase se conservan en la base pero
   se ignoran al capturar, y el validador lo avisa.
3. `origen` existe por separado en `solicitud` y en `partida`. Una solicitud de
   origen CFDI puede tener partidas agregadas manualmente o por el modal masivo.
4. `clave_idempotencia` se calcula sobre la solicitud completa **incluyendo
   partidas**. Si cambian las partidas después de un intento fallido, es una
   solicitud distinta y se reintenta limpia.
5. `ambiente` se guarda **en el lote**, no en preferencias: un lote capturado en
   stage no debe poder reanudarse contra producción.

## 6. Ingesta y adaptadores

### CfdiAdapter

El **XML es la fuente de verdad**, no el PDF. Del CFDI se extrae: UUID, RFC
emisor y receptor, subtotal, impuestos, total, moneda, tipo de cambio, forma y
método de pago, y conceptos (que se mapean a partidas de clase `INSUMO`).

- Emparejamiento automático PDF ↔ XML por UUID o por nombre de archivo.
- El PDF se conserva como evidencia adjunta y, cuando aplica, como el documento
  que se sube al campo `PDF` de la solicitud.

### ExcelAdapter

El formato lo define `core/plantilla_excel.py`, que es la **fuente única**: de
ahí sale tanto la plantilla que el usuario descarga como las reglas con que se
vuelve a leer, así que no pueden desalinearse.

- **Plantilla descargable** con cuatro hojas: `Instrucciones`, `Solicitudes`
  (una fila por solicitud), `Partidas` (desglose opcional, ligado por
  `Referencia`) y `Catálogos`. Las columnas de catálogo se validan con listas
  desplegables tomadas de los valores reales de SIPP.
- **Una fila = una solicitud.** El caso normal es un pago por persona con un
  concepto; obligar a dos hojas para eso volvería tedioso lo común. Cuando una
  solicitud sí lleva varios conceptos o insumos, se le pone `Referencia` y se
  desglosa en la segunda hoja, que manda sobre el concepto de la fila principal.
- **Mapeo de columnas configurable**: la detección automática reconoce los
  encabezados de la plantilla y sus sinónimos (los que ya usan los archivos del
  área: `MONTO`, `CLAVE INTERBANCARIA`, `EX-COLABORADOR (DESCRIPCIÓN)`…). Si
  falta algún campo obligatorio, la interfaz pide asignarlo a mano.
- **La CLABE y la fecha se piden como texto.** Si Excel convierte la CLABE en
  número pierde los ceros de la izquierda —y el pago se iría a otra cuenta—; y
  una fecha real se reinterpreta según la configuración regional del equipo, de
  modo que `03/04` puede ser 3 de abril o 4 de marzo según quién la abra. El
  lector acepta ambas formas de todos modos, pero la plantilla pide la segura.
- La fila de ejemplo de la plantilla lleva una marca y **se ignora al importar**.

### CaratulaAdapter — una carátula, una solicitud

Invierte el orden de la carga masiva, y lo hace porque así llega el trabajo: el
área recibe **una carpeta con una carátula por persona**, y esa carpeta ya es el
listado de a quién hay que pagarle.

- Cada archivo (PDF, JPG o PNG) se convierte en un borrador de solicitud con su
  carátula ya adjunta.
- El **nombre del beneficiario sale del nombre del archivo**, descartando el
  ruido habitual: `CARATULA`, `Edo Cta`, el banco, meses, folios.
  `Edo Cta Banorte LUIS ALBERTO DIAZ.pdf` → `LUIS ALBERTO DIAZ`. No se lee el
  contenido del PDF: los formatos varían por banco y el OCR sería menos
  confiable que el nombre que una persona escribió a propósito.
- **Los acentos y la Ñ se conservan.** El nombre se registra tal cual en SIPP al
  dar de alta al beneficiario, y `JOSE NUNO` en vez de `JOSÉ ÑUÑO` lo deja mal
  escrito en el catálogo del ERP de forma permanente.
- Si no se reconoce ningún nombre, el borrador se marca y el nombre **se edita
  en la propia tabla**: más rápido que renombrar el archivo y volver a cargar.
- Después, el **Excel completa por emparejamiento de nombre** (tolerando el
  orden: `RUIZ SOTO MARIA` encuentra a `MARIA RUIZ SOTO`). Solo se rellenan los
  campos vacíos, para no pisar correcciones manuales, y **el nombre del
  beneficiario nunca se sobrescribe**: manda el de la carátula, que es la que
  acredita la cuenta a la que se va a pagar. Una fila del Excel se consume una
  sola vez.

Lo que este orden garantiza y el inverso no: **ninguna solicitud puede quedarse
sin carátula**, porque no existe si no hay archivo.

### ManualAdapter

- Formulario dentro de la app con los mismos validadores.

### Ingesta de carpetas

- Recorrido recursivo.
- Hash SHA-256 por archivo para no reprocesar el mismo documento.
- **Emparejamiento de carátulas y Vo.Bo. por nombre de persona**, normalizando
  acentos y espacios, como ya hace `buscar_archivo_por_nombre`.

### Documentos obligatorios: carátula y Vo.Bo.

SIPP pide dos archivos que el robot no puede inventar, y sin los cuales el flujo
se queda a medias:

- **Carátula bancaria** — obligatoria al dar de alta una cuenta nueva
  y además en el campo `PDF` de la solicitud. Sin
  ella SIPP no registra la cuenta, así que el motor **se detiene antes de tocar
  el formulario** y marca la solicitud como `REVISAR`, en vez de dejar un
  beneficiario registrado a medias que después hay que limpiar a mano.
- **Vo.Bo. de Compras** — respaldo obligatorio en Pago Extraordinario. Solo se
  puede adjuntar **después** de guardar.

Se asignan de dos maneras, y ambas terminan en la tabla `documento`:

1. **Uno por uno**, desde el formulario de captura.
2. **Por carpeta** (`core/documentos.py`), emparejando por el nombre del
   beneficiario. Es lo que se usa en un lote grande: el área recibe una carpeta
   con un PDF por persona y nadie va a adjuntar noventa archivos a mano. El
   emparejamiento tolera acentos, Ñ, dobles espacios y texto de más en el nombre
   del archivo (`CARATULA JUAN PEREZ LOPEZ BBVA.pdf` encuentra a *Juan Pérez
   López*), pero exige que **todas** las palabras del nombre estén presentes: un
   apellido de menos ya no es la misma persona.

**La carátula siempre acaba en PDF.** Es lo que el área entrega al banco y lo
que SIPP guarda como respaldo de la cuenta, pero llega a menudo como foto o
captura de pantalla. Al registrarla —el único punto por el que pasan las dos
vías de alta— una imagen se convierte a PDF (`documentos.asegurar_pdf`) y se
guarda en `DATOS\caratulas`, no junto al original: suele venir de una carpeta
compartida o de Descargas, donde no se debe escribir. Si la conversión falla se
usa el original, porque quedarse sin carátula detiene la solicitud entera y un
JPG no. El Vo.Bo. se registra tal cual: suele ser la captura del correo de
autorización y SIPP la acepta.

La falta de carátula se avisa en tres momentos: al guardar la solicitud, en el
detalle de su fila en la tabla, y en la confirmación previa a ejecutar el lote.

### Preparación de archivos antes de subir

Regla del sistema real, no negociable: SIPP rechaza archivos con Ñ o acentos en
el nombre y no acepta más de ~10 MB. Antes de cada subida se genera una copia
temporal con el nombre saneado y, si es un PDF que excede el límite, se comprime
con PyMuPDF bajando DPI y calidad en escalones.

## 7. Validaciones previas

Se ejecutan antes de encolar y se vuelven a ejecutar al editar en la tabla:

- Beneficiario existe en el catálogo de SIPP (o está marcado como nuevo).
- RFC con longitud y forma válidas; SIPP lo valida en línea con
  `validarRFCRegistrado()` y bloquea si ya está registrado.
- Fecha de pago válida y no en día inhábil.
- Suma de partidas `CONCEPTO` cuadra con el total del CFDI (cuando aplica).
- Sin duplicados dentro del mismo lote (por `clave_idempotencia`). El índice
  único es **por lote**, no global: rehacer un lote desde cero es una operación
  legítima. La protección contra pagar dos veces de verdad no es esta, sino la
  consulta al propio SIPP antes de capturar (§8).
- Centro de costos y cuenta contable existen (solo partidas `INSUMO`).
- **Al menos un renglón del desglose que le corresponde al tipo de
  beneficiario**, con importe > 0 (ver la regla 1 del modelo).
- Si la forma de pago es Transferencia: CLABE presente, con 18 dígitos y **con
  su dígito verificador correcto** (algoritmo de la ABM, pesos 3·7·1, en
  `validador.clabe_valida`), y carátula localizada.

  El verificador es la única regla que distingue una CLABE con un dígito
  equivocado de una correcta: las dos miden 18, las dos son todo dígitos y las
  dos empiezan con un código de banco real. Si no se comprueba aquí, el error
  aparece cuando el dinero ya salió a otra cuenta. Aplica igual venga la CLABE
  del OCR de una carátula, de un Excel o tecleada a mano.

El modal de asignación masiva **usa el mismo validador**. No debe existir un
camino corto que lo evite.

## 8. Motor RPA

### Máquina de estados

```
PENDIENTE → VALIDADA → EN_CAPTURA → LLENADA → GUARDADA → ENVIADA_AUTORIZAR
                                       ↓         ↓
                                   (parada)  (parada)
ERROR · OMITIDA · REVISAR (transiciones desde cualquier estado)
```

Cada transición se persiste **antes** de ejecutar el paso siguiente.

`REVISAR` es un estado terminal propio para los casos que el robot no debe
resolver solo: beneficiario con varias coincidencias en el catálogo, o empresa
sin el concepto de pago requerido.

### El beneficiario lo decide SIPP, no la solicitud

El campo `beneficiario_nuevo` es una **expectativa de quien capturó**, no un
hecho: nadie que llena un Excel puede saber si esa persona ya está dada de alta
en el ERP. Por eso el motor **consulta el catálogo primero, siempre**, y actúa
según lo que encuentre:

| Coincidencias | Qué hace |
|---|---|
| 1 | La selecciona y usa su cuenta bancaria. **Verifica** que el nombre que quedó en el formulario sea el que se buscó |
| 0 | Marca «No Registrado» y la da de alta (exige RFC y carátula) |
| varias | `REVISAR`: elegir mal manda el dinero a otra persona |

Cuando la expectativa y la realidad no coinciden, se anota en la bitácora y
manda la realidad. Equivocarse sale caro en los dos sentidos: dar de alta un
duplicado, o buscar a alguien que no existe.

Para elegir la cuenta de un beneficiario existente se empareja por CLABE. Si la
solicitud no la trae y el beneficiario tiene **una sola** cuenta, se usa esa; si
tiene varias, `REVISAR`.

### Punto de parada

- `AUTORIZAR` **exige el Vo.Bo. de Compras**: SIPP no envía a autorización un
  Pago Extraordinario sin ese documento de respaldo. Si falta, la solicitud se
  queda en `GUARDADA` con el motivo, en vez de intentarlo y fallar.
- Valor por defecto a nivel lote (`lote.parada_default`).
- Columna editable por fila en la tabla, que hereda ese valor.
- El motor lee `solicitud.parada` **al momento de procesar la fila**, no al
  iniciar el lote. Permite cambiar de opinión con el proceso corriendo.
- `LLENADA` deja el formulario completo sin guardar, para que el operador revise
  y dé el clic. Es el modo de la primera corrida de cualquier lote nuevo.

### Idempotencia

Es el punto crítico. Antes de capturar, el RPA busca en el listado de SIPP la
solicitud correspondiente a la `clave_idempotencia`, filtrando por beneficiario,
descripción y rango de fechas (`filtros.nb_Proveedor`, `filtros.de_descripcion`,
`fh_inicio` / `fh_fin`). Si ya existe, marca la fila como `GUARDADA` con su folio
y continúa. El `folio_sipp` se persiste en cuanto aparece en pantalla, no al
final del flujo.

### Reintentos y errores

- Máximo 3 intentos con backoff.
- Captura de pantalla automática en cada error, guardada en la bitácora.
- Ante sesión expirada o cambio de contraseña obligatorio en SIPP: pausar el
  lote, no seguir fallando. SIPP muestra el modal
  `#divBloqueo_modalActualizarContrasena` cuando la contraseña es la
  predeterminada; ese caso se detecta desde el login.
- **Error de almacenamiento de Google** (`Error making Google REST call`,
  `CloudStorage 403 Forbidden`): es intermitente al subir archivos. Se cierra el
  aviso y se reintenta la subida; no debe abortar el lote.
- **Recuperación dura entre registros:** si el formulario queda atorado (modal
  que no cierra), se recarga la página, se reconfigura la sesión si reaparece esa
  pantalla, y se vuelve a navegar al listado.

### Concurrencia

Una sesión de SIPP por usuario, un contexto de navegador, una solicitud a la vez.
Sin paralelismo.

### Cómo se opera SIPP realmente

SIPP es una **SPA de AngularJS 1.x** (ui-router + ng-grid + plugin *chosen*)
sobre ColdFusion, en `index.cfm#/SolicitudPago`. Todo es **AJAX**: el RPA espera
cambios de DOM y de red, nunca navegaciones completas. Tres consecuencias:

1. **Los `<select>` nativos están ocultos** (`style="display:none"`) detrás de un
   widget *chosen*. No se pueden operar con `select_option`. Hay que abrir el
   contenedor hermano `div.chosen-container`, **teclear** el texto en
   `input.chosen-search-input` (con `type()`, **nunca** `fill()`: chosen filtra
   con el evento `keyup`, y sin él las opciones más allá de las ~100 que
   renderiza por defecto jamás aparecen) y clicar el `li.active-result`. Las
   listas dependientes —Tipo de Pago Extraordinario, Sucursal, Cuentas
   Bancarias— se llenan *después* de elegir el campo padre, así que el helper
   reintenta cerrando con `Escape` y reabriendo.
2. **Al escribir en un input hay que disparar `input` y `change` a mano**, o
   AngularJS no actualiza el modelo. En los campos con directiva de moneda
   (`contenido_moneda`) además hay que teclear y hacer `blur` con `Tab`.
3. **Hay ids duplicados.** Varios campos del beneficiario —RFC, razón social,
   correo y el botón de cuenta bancaria— existen **tres veces** en el DOM, uno
   por panel de tipo de beneficiario. Siempre hay que acotar por el panel
   contenedor:
   la plantilla `paneles_beneficiario` del archivo de datos, con el id del
   tipo (1 Proveedor · 2 Deudor · 3 Acreedor).

### Mapa de selectores

Vive en `core/selectores.py` (constantes con nombre) respaldado por
`selectores.json` en `DATOS` para poder parchear en caliente sin recompilar,
cada uno con primario y respaldos. Los de abajo están verificados contra las
páginas reales de [Paginas html/](Paginas%20html/) y contra la corrida en
producción del RPA previo.

Las **80 entradas del mapa no se documentan aquí**: viven en `datos/sipp.json`,
que no se versiona. Este repositorio es público, y la lista completa —cada campo
del formulario de pagos, su id, sus respaldos— es una descripción bastante
precisa de la estructura interna del ERP. `datos/sipp.ejemplo.json` documenta el
formato con valores de mentira; ver `core/sipp_datos.py` para cómo llega el
archivo real a cada máquina.

Lo que sí conviene dejar escrito es **cómo está organizado** y las trampas que
condicionan su uso, porque eso es conocimiento que se pierde si no se anota:

| Prefijo | Qué agrupa |
|---|---|
| `login.*` | Entrada al portal y el modal de contraseña vencida |
| `sesion.*` | Configuración de empresa y plaza posterior al login |
| `listado.*` | Filtros, búsqueda y acciones de la rejilla de solicitudes |
| `sol.*` | Encabezado y datos generales del formulario |
| `ben.*` | Beneficiario, su buscador y sus cuentas |
| `cb.*` | Modal de cuenta bancaria |
| `ins.*`, `cc.*` | Insumos y centros de costo, con sus modales de ayuda |
| `con.*` | Grid de conceptos de pago |
| `acc.*`, `doc.*` | Guardar, autorizar y documentos de respaldo |
| `tabs.*`, `grid.*`, `alerta.*` | Tira de pestañas, renglones y avisos |

Además de los selectores, el archivo lleva `consultas`: fragmentos de JavaScript
que interrogan el modelo de AngularJS para leer lo que el portal no pinta en
pantalla —el folio tras guardar, la cuenta que quedó registrada— y para forzar
el recálculo del total cuando el portal no lo dispara solo.

Tres reglas que hay que respetar al editarlo:

1. **Acota por panel** los campos del beneficiario (`paneles_beneficiario`):
   varios ids existen tres veces, uno por tipo, y sin acotar se escribe en el
   que está oculto.
2. **Usa `:visible`** donde un elemento aparezca varias veces con uno solo en
   pantalla. Es lo que pasa con el buscador de beneficiario, y sin el filtro el
   clic muere por timeout contra el nodo escondido.
3. **Los `<select>` no se operan con `select_option`**: están ocultos tras el
   widget *chosen* (ver arriba).


**Trampa conocida del grid de conceptos:** hay que clicar la celda de selección
(`div.ngSelectionCell`), nunca el nombre u otra celda de la fila — en ng-grid eso
*deselecciona* el renglón y el total vuelve a $0. Tras seleccionar se verifica
`Cantidad a Pagar`; si quedó en cero, se fuerza vía el `scope` de Angular. Una
solicitud jamás debe guardarse en $0.

### Verificación previa

Acción "Verificar conexión con SIPP" en Configuración: entra, navega a la
pantalla de solicitud y comprueba que cada selector resuelva, sin capturar nada.
Detecta cambios en SIPP antes de lanzar un lote.

### Ambientes

Las URLs de los dos ambientes están en `datos/sipp.json`, no aquí: son el punto
de entrada al sistema y este repositorio es público. La herramienta las lee al
arrancar y las muestra en Configuración ⚙.

**La aplicación instalada trabaja siempre contra producción y no ofrece
cambiarlo.** Poder elegir servía sobre todo para equivocarse: la elección se
guardaba en el equipo, así que haberla dejado en pruebas una vez bastaba para
capturar un lote entero contra stage sin que nada lo delatara salvo un
indicador que ya nadie mira. El encabezado sigue mostrando la franja de
producción —cada solicitud guardada consume un folio real y eso no debe poder
olvidarse— y Configuración enseña la URL, para poder comprobar contra dónde se
trabaja aunque no se pueda cambiar.

**El desarrollo y las pruebas se hacen contra stage**, desde los scripts de
`scripts/`, que sí reciben el ambiente. Diferencias conocidas de stage: el
catálogo de conceptos de pago no es el mismo que en producción, y la subida de
archivos falla de forma intermitente.

### Lo verificado contra stage

Corrida del **31/07/2026** con `scripts/explorar_sipp.py` (no captura ni guarda
nada; solo navega y lee):

- ✅ Login, configuración de sesión y navegación SPA al formulario.
- ✅ Los **29 selectores críticos** resuelven en el portal vivo.
- ✅ Los **desplegables «chosen»**, incluidas las listas dependientes: Sucursal
  se llena tras elegir Empresa, y Tipo de Beneficiario tras Tipo de Pago. Era la
  única pieza del motor que los fixtures no podían probar.
- ✅ `abrir_pestana` por texto, acotado a la tira de pestañas.
- ✅ Los catálogos de la plantilla coinciden **exactamente** con los de SIPP:
  Forma de Pago y Tipo de Gasto, cuatro valores cada uno.
- ⚠️ La etiqueta real de la pestaña es **«Insumos & Servicios»**, no «Insumos».
- ⚠️ El grid de conceptos **virtualiza**: solo mantiene en el DOM las filas
  visibles. Hay que recorrerlo con scroll tanto para importar el catálogo como
  para **elegir el concepto al capturar**; mirar solo lo que está puesto daba
  por inexistentes los conceptos de más abajo, que son los dados de alta
  después (reportado en producción el 26/08/2026).
- ⚠️ En stage, `Aske` no tiene conceptos de pago asignados para Acreedor ni para
  Deudor (el grid llega vacío). Para probar el llenado completo hace falta una
  empresa que sí los tenga.

Corrida del **31/07/2026** con `scripts/prueba_llenado_stage.py` (llena el
formulario completo y se detiene antes de Guardar, sin consumir folio):

- ✅ Alta de beneficiario nuevo con el panel acotado, y su cuenta bancaria.
- ✅ **Carátula adjunta** y enganchada al modelo de SIPP.
- ✅ Grid de conceptos: importe tecleado, renglón seleccionado y
  **`Cantidad a Pagar` = $1,234.56**, que es la prueba de que la solicitud no
  se guardaría en $0.
- ⚠️ **SIPP descarta los signos** de los campos con la directiva
  `contenido_alfanumerico_con_espacios`: «PRUEBA AUTOMATIZADA - NO AUTORIZAR»
  queda como «PRUEBA AUTOMATIZADA NO AUTORIZAR». Se normaliza antes de teclear
  (`rpa_sipp.texto_sipp`) y, sobre todo, **antes de buscar**: la búsqueda de
  idempotencia filtraba por descripción y no habría encontrado lo ya capturado,
  registrando el pago dos veces.
- ⚠️ Los dos campos de archivo **no suben en el mismo momento**: el `PDF` de la
  solicitud solo engancha el archivo y SIPP lo sube al Guardar, mientras que
  el del modal de cuenta bancaria sube al cerrar ese modal.

Corrida del **01/08/2026** con `prueba_guardado_stage.py` y
`prueba_autorizacion_stage.py` (estas sí escriben en el portal):

- ✅ **Guardar**, con lectura del folio. Se comprueba que SIPP lo confirme: sin
  folio ni botón de autorizar, se lanza error en vez de dar el paso por bueno.
- ✅ **Adjuntar el Vo.Bo.** como documento de respaldo, y su subida al guardar.
- ✅ **Solicitar Autorización**: la solicitud pasó de `BORRADOR` a `PENDIENTE`.
- ✅ La **salvaguarda de idempotencia** encuentra lo ya capturado (folio 3788) y
  no da falsos positivos.
- ⚠️ **SIPP exige el Vo.Bo. de Compras para autorizar** un Pago Extraordinario.
  Sin él, pulsar el botón no hace nada más que mostrar un aviso. El motor ahora
  se detiene antes de intentarlo y deja la solicitud en `GUARDADA` explicando
  que falta el documento.
- ⚠️ **El filtro del listado se queda pegado** entre búsquedas: se escribe
  siempre, aunque venga vacío, o una solicitud sin descripción heredaría la de
  la anterior y buscaría a la persona equivocada.
- ⚠️ `filtros.nb_Proveedor` es **readonly**: lo llena el buscador de proveedores
  y no se puede teclear. El listado se filtra por descripción.

Corrida del **24/08/2026**, instrumentando el portal para ver por qué el
guardado no avanzaba (folios 3808–3810):

- ⚠️ **SIPP rechaza en silencio.** `generarSolicitud()` valida campo por campo y,
  al fallar, llama a `inlineMsg(campo, mensaje)` y hace `return`: pinta un globo
  pegado al campo y no muestra alerta, no abre diálogo y no llama a su servidor.
  Desde fuera es indistinguible de «el botón no hizo nada». El motor ahora
  observa esas funciones (`espiar_validaciones`) y **reporta el mensaje textual
  del portal** en vez de esperar un folio que no va a llegar.
- ⚠️ **La fecha de pago solo se registra al perder el foco.** El campo visible
  lleva el texto con máscara; SIPP lo convierte a fecha y lo pasa al modelo en un
  `blur` de jQuery (`directivas.js`). Escribir y disparar `input`/`change` dejaba
  el recuadro con la fecha a la vista y el modelo vacío, y al guardar cortaba con
  «La información de la Fecha Pago es requerida». `_poner_fecha` comprueba ahora
  **el modelo**, no lo que se ve.
- ⚠️ **Elegir el Vo.Bo. no es haberlo subido.** SIPP le pone nombre al renglón
  (`NB_DOCUMENTO`) cuando termina de subirlo, y ese nombre es lo que mira para
  dejar enviar a autorizar. Comprobar el selector de archivo —que se llena al
  instante— daba el adjunto por bueno con la subida a medias, y el envío se
  rechazaba sin relación aparente con el documento que sí se había elegido.
- ⚠️ **El catálogo se busca por nombre y SIPP identifica por RFC.** Si el nombre
  no coincide letra por letra con el registrado, el robot daba de alta un
  duplicado y SIPP lo rechazaba al guardar. Ahora se consulta al escribir el RFC
  y se detiene ahí, con el RFC y la solicitud en el mensaje.
- ✅ Extremo a extremo tras los arreglos: **guardar → adjuntar Vo.Bo. → solicitar
  autorización**, comprobado en el listado (3809 y 3810 en `PENDIENTE`; la 3808,
  de antes del arreglo del adjunto, se quedó en `BORRADOR`).

Ningún paso del motor queda sin validar contra el ambiente de pruebas. Lo que no
se ha hecho nunca es una corrida contra **producción**.

### Pruebas con fixtures

Las páginas de [Paginas html/](Paginas%20html/) se sirven localmente y Playwright
ejecuta el llenado contra las copias: valida navegación, resolución de selectores
y llenado de campos sin tocar SIPP ni consumir folios. Cubre el llenado, no el
guardado (los fixtures no tienen backend). El guardado se prueba en stage.

Las páginas HTML son **datos reales del portal: no se versionan** (sección 8 de
[ARQUITECTURA.md](ARQUITECTURA.md)).

## 9. Interfaz

Aplica [DISENO.md](DISENO.md) sin excepciones: roles de color de `ui/tema.py`,
nada de hex sueltos, filas de tabla de 44px, ancho mínimo de ventana 960px,
navegación horizontal en el encabezado, fechas siempre por `comun.CampoFecha`.

### Secciones

| Sección | Contenido |
|---|---|
| Solicitudes | Tabla maestro-detalle, toolbar del lote, altas (manual, carátulas, Excel), ejecución |
| Documentos | Carga por arrastre, archivo o carpeta. Emparejamiento PDF/XML y de carátulas/Vo.Bo. |
| Conceptos | Catálogo de conceptos de pago: importación desde SIPP y alta manual |
| Bitácora | Historial por solicitud, capturas de pantalla, exportación |
| Configuración | Credenciales, modo visible/headless, verificación de conexión, versión. El ambiente se muestra, no se elige |

### Tabla de solicitudes

- Maestro-detalle: la fila expandible muestra las partidas, separadas por clase
  (Conceptos de Pago / Insumos y Servicios), con su origen y centro de costos.
- Columnas: estado, beneficiario, tipo de beneficiario, empresa, fecha, importe,
  punto de parada, folio SIPP.
- Resize, reorder y pinning de columnas; el estado de columnas se persiste en
  SQLite para sobrevivir reinicios y actualizaciones.
- Color de fila por estado, tomando `ERROR` de `ft.Colors.ERROR` y los demás de
  los roles del tema.
- Edición inline que re-dispara la validación.
- Selección múltiple para acciones masivas: omitir, cambiar punto de parada,
  reintentar.
- Marca visual en celdas provenientes de documento frente a celdas capturadas.

### Reporte final del lote

Al terminar la ejecución —haya acabado entera, se haya detenido o se haya
interrumpido— se abre un modal con **todas** las solicitudes del lote:

- Arriba, el **total** y el desglose por resultado.
- Debajo, **una pestaña por resultado**: Registradas, Con errores, Para revisar
  y —solo si tiene algo— Sin procesar. Cada fila lleva beneficiario, descripción,
  importe, folio de SIPP y el motivo cuando lo hay.

Sustituye al aviso que se desvanecía: con lotes de decenas de solicitudes, saber
CUÁNTAS fallaron no sirve de nada sin saber cuáles. Se lee de la base y no del
resumen del motor, para que sobreviva a que el lote se corte a media captura.

Los estados se reparten así, y el reparto **suma siempre el total**: un estado
que no encaje en ningún grupo cae en «sin procesar» en vez de desaparecer.
`LLENADA` va con las de revisar —el formulario se llenó, pero espera a que
alguien lo guarde— y no con las que no se intentaron.

### Catálogo de conceptos de pago

SIPP asigna los conceptos **por empresa** y no ofrece forma de consultarlos sin
entrar al formulario de captura. Eso obliga a escribirlos de memoria, y un
concepto mal escrito no falla al capturarlo: falla a media corrida, cuando el
robot no lo encuentra en el grid y marca la solicitud para revisión.

La pestaña **Conceptos** mantiene una lista local que alimenta los desplegables
de la plantilla de Excel y del formulario de captura, para **elegir en vez de
teclear**.

Es una **lista única para todo el grupo**, no una por empresa: el criterio del
área es que todas manejen los mismos conceptos. La empresa desde la que se leyó
cada uno se conserva solo como rastro informativo, en el tooltip de su origen.

- **Importar desde SIPP** lee el grid de una empresa y lo guarda. El selector de
  empresa solo decide **de dónde leer**. Volver a importar no duplica —ni desde
  otra empresa— y sirve para detectar conceptos agregados después. El catálogo
  se sembró con los **33 conceptos de `Abastecedora`**, la empresa base.
- **Alta manual** permitida, porque a veces hace falta uno que aún no se ha
  importado, pero marcada aparte y con **advertencia explícita**: un concepto
  escrito a mano puede no existir en SIPP, y si no existe, la solicitud que lo
  use no se podrá capturar. Hay que pedirlo al equipo de soporte.
- Los nombres se normalizan (mayúsculas, sin acentos, sin dobles espacios) para
  que «Pago PTU» y «PAGO PTU» no acaben siendo dos entradas del mismo concepto.
- En la tabla, cada concepto muestra su origen: **importado de SIPP** (existe
  con certeza) o **manual, sin verificar**.
- En el formulario de captura el control es un desplegable **editable**: ofrece
  el catálogo y filtra al escribir, pero no impide capturar uno que no esté.

### Modal de carga masiva de solicitudes

Se abre desde el botón **Carga masiva** de la toolbar del lote. Tres pasos, y
**nada se guarda hasta el último**: importar cien solicitudes de golpe solo sirve
si antes se ve, de golpe, qué está mal en cuáles.

1. **Descargar la plantilla.** Genera el `.xlsx` descrito en §6 con las
   instrucciones y los catálogos ya cargados.
2. **Elegir el archivo lleno.** Se analiza fuera del hilo de la interfaz. Si
   algún campo obligatorio no se reconoció, aparece el mapeo columna → campo.
3. **Vista previa.** Una fila por registro con semáforo: lista, con problemas, o
   repetida. El detalle de cada error va en la fila y completo en su tooltip, y
   hay un filtro para ver solo las problemáticas.

Las filas con errores **no bloquean a las demás**: se importa lo bueno y el
archivo se corrige para el resto. Las que ya existen en la base se omiten por su
`clave_idempotencia` y se informa cuáles fueron, con su número de fila.

### Modal de asignación masiva de concepto / insumo

- **Alcance**: todas las del lote · solo seleccionadas · solo las que no tienen
  desglose. «Sin desglose» se mide contra **la clase que le toca a cada
  solicitud**: un Proveedor con insumos ya lo tiene; un Acreedor con insumos de
  un CFDI pero sin conceptos, no.
- **La clase no se elige**: la impone el tipo de beneficiario, como en el resto
  de la herramienta. Una misma asignación puede producir conceptos e insumos en
  el mismo lote, y está bien.
- **Modo**: agregar como renglón adicional · reemplazar los existentes.
- **Datos**: concepto o insumo (desplegable del catálogo, editable), y para los
  insumos centro de costos y cuenta contable.
- **Importe**: tomar el total de la solicitud · distribuirlo entre los renglones
  · un importe fijo · dejar en blanco. «Tomar el total» entiende por total el
  **importe implícito**: `importe_total` si ya existe y, si no, la suma de las
  partidas que haya. Sin eso no serviría para el caso principal —una solicitud
  importada de un CFDI vale cero hasta que tenga conceptos—.
- El reparto ajusta el **último renglón** con el sobrante de redondeo, para que
  la suma cuadre al centavo: 100 entre 3 da 33.33 · 33.33 · **33.34**.
- Advertencia cuando hay solicitudes con partidas provenientes de CFDI, y más
  fuerte todavía si el modo es reemplazar: eso lo timbró el SAT.
- **Nunca se tocan las solicitudes ya capturadas** (`GUARDADA`,
  `ENVIADA_AUTORIZAR`): cambiarles el desglose aquí no cambiaría nada en SIPP y
  dejaría la base mintiendo. Se listan como omitidas.
- El botón principal es **Vista previa**, no Aplicar: muestra el antes/después
  de cada fila y desde ahí se confirma. **Cambiar cualquier criterio invalida la
  vista previa**, para que no se pueda aplicar un plan distinto del que se vio.
- Antes de aplicar se guarda un **snapshot** del lote. Se conserva solo el
  último —es un «deshacer», no un historial— y se consume al usarlo.

## 10. Credenciales y seguridad

- Almacenamiento con **DPAPI** vía `core/credenciales.py` →
  `credenciales_rpa.json` en `DATOS`. Nunca en JSON plano, en `.env` ni en el
  código.
- La contraseña nunca se escribe en la bitácora y se enmascara en las capturas de
  pantalla.
- Sesión persistida vía `storage_state` cifrado. Antes de cada lote se verifica
  con una petición a una página autenticada; si redirige a login, se usan las
  credenciales.
- El PAT de GitHub del updater va en `QUETZALTIC_GITHUB_PAT` (variable de entorno
  o `.env` junto al `.exe`), con alcance de solo lectura del repo.
- `.gitignore` cubre `*.db`, credenciales, tokens, `.env`, diagnósticos del RPA,
  `ms-playwright/`, tessdata, salidas de build, **las páginas HTML de SIPP** y
  los documentos de prueba reales (`CARATULAS/`, `VOBO/`).

## 11. Actualización y CI

Un push a `main` no es un release. El flujo es el de la sección 10 de
[ARQUITECTURA.md](ARQUITECTURA.md):

1. Merge a `main`.
2. **Publicar un Release** con un tag mayor (`0.2.0`) → dispara GitHub Actions.
3. El CI sincroniza `version.py` y `AppVersion` con el tag, corre
   `scripts/smoke_import.py`, compila con `flet pack`, empaqueta Tesseract, arma
   el instalador con Inno Setup y sube `Instalador_SolicitudesPago.exe`.
4. La app lista las releases al arrancar, toma la de mayor versión, descarga el
   asset privado con el PAT y lo aplica en silencio, reiniciándose sola. **No
   se usa `releases/latest`**: ese endpoint esconde los pre-releases, y con las
   dos releases del repo marcadas así respondía 404 —la app anunciaba que
   estaba al día mientras la versión nueva llevaba días publicada
   (25/08/2026)—. Un fallo de consulta ya no se disfraza de «no hay novedad»:
   se dice que no se pudo comprobar.

Requisitos propios de esta herramienta:

- Mostrar versión actual y notas de cambio en la UI.
- **Bloquear la actualización si hay un lote en ejecución.**
- Soportar `minVersion`: si la instalada es menor, bloquear la ejecución de lotes
  hasta actualizar. Sirve para forzar la adopción de un parche de selectores
  cuando SIPP cambie.
- Firma de código en Windows para evitar advertencias de SmartScreen.

## 12. Pendientes por definir

Cerrados con las páginas HTML reales (ver sección 8):

- ~~Captura de partidas: ¿grid o pantalla aparte?~~ → Grid en el mismo
  formulario. Insumos en `tab == 4`, Conceptos en `tab == 5`. Los insumos abren
  modales de ayuda anidados con retorno al formulario padre.
- ~~¿Postbacks o AJAX?~~ → AngularJS 1.x, todo AJAX.
- ~~Reglas de tipo de cambio~~ → El formulario **no tiene** campo de tipo de
  cambio. Solo `Moneda`, que se deshabilita al elegir cuenta bancaria: la moneda
  la impone la cuenta. Una solicitud en moneda extranjera exige una cuenta
  bancaria en esa moneda.
- ~~Catálogo completo de campos~~ → Levantado en la sección 8; falta anotar
  obligatoriedad por combinación de tipo de beneficiario, que solo se confirma
  probando en stage.

Abiertos:

- **Política de días inhábiles.** El formulario no valida el calendario
  (`#fh_pago` es una máscara `99/99/9999` libre). Hay que decidir entre un
  calendario propio mantenido en la app o no validar y dejarlo al criterio del
  operador.
- **Obligatoriedad real por tipo de beneficiario.** Qué campos exige SIPP para
  Proveedor y Deudor; de Acreedor ya se sabe por la corrida en producción.
- **Comportamiento del grid de Insumos**, sin automatizar hasta hoy: cómo se
  captura cantidad y precio unitario en el renglón, y si el total de insumos
  debe cuadrar contra el de conceptos.
- **Concepto de pago por defecto.** El RPA previo usaba `PAGO PTU` fijo desde
  configuración. Al generalizar, hay que decidir si viene del Excel/CFDI, si se
  elige por lote, o ambas.

## 13. Fases

| Fase | Entregable |
|---|---|
| 1 | Shell Flet + `core/rutas.py`, `db.py`, tema y tabla maestro-detalle con captura manual |
| 2 | Port de `sipp_rpa.py` a `core/rpa_sipp.py` (`SesionSipp` + flujo de Pago Extraordinario) contra **fixtures locales** |
| 3 | `ExcelAdapter` y `CfdiAdapter` + validador |
| 4 | Ejecución contra **stage**, máquina de estados, idempotencia, bitácora y evidencias |
| 5 | Grid de Insumos (el tramo sin automatizar) y modal de asignación masiva con snapshot y deshacer |
| 6 | Ejecución contra producción con parada en `LLENADA`, y liberación |
| 7 | CI, instalador, firma de código y actualización automática |

La fase 2 arranca de código probado, no de cero: es un port con reorganización,
no una reescritura. La fase 5 es la de mayor riesgo, porque es la única parte del
formulario que nadie ha automatizado todavía.
