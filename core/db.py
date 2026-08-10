"""Persistencia local del dominio: lotes, solicitudes, partidas, documentos y
bitácora. SQLite sin servidor, en la carpeta escribible del usuario (rutas.DATOS).

Patrón (ver ARQUITECTURA.md §9):
  - Una `@dataclass` por entidad.
  - Una **clave única de negocio** por solicitud: `clave_idempotencia`. Es lo que
    impide capturar dos veces el mismo pago si el proceso se reanuda a medias.
  - `inicializar()` crea las tablas Y aplica migraciones incrementales, para no
    romper las bases que ya existen en los equipos.
  - La lista de columnas de cada tabla es la **fuente única** que alimenta el
    INSERT, el UPDATE y las migraciones: agregar un campo es tocar un solo sitio.

Sobre las partidas: SIPP desglosa una solicitud de dos maneras distintas
—"Conceptos de Pago" y "Insumos & Servicios"— y la herramienta necesita ambas
(ESPECIFICACION.md §5). Se modelan en la misma tabla con la columna `clase`,
porque comparten el maestro-detalle de la interfaz y el cálculo de totales; los
campos que solo aplican a una clase quedan en NULL para la otra.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from core import catalogos, rutas

RUTA_DB = os.path.join(rutas.DATOS, "solicitudes_pago.db")

# Clases de partida (ver el docstring del módulo).
CONCEPTO = "CONCEPTO"
INSUMO = "INSUMO"


# --------------------------------------------------------------------------- #
#  Entidades
# --------------------------------------------------------------------------- #
def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _nuevo_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Lote:
    """Un conjunto de solicitudes que se preparan y ejecutan juntas."""

    id: str = field(default_factory=_nuevo_id)
    nombre: str = ""
    parada_default: str = "LLENADA"
    # El ambiente se guarda EN EL LOTE, no en preferencias: un lote capturado
    # contra pruebas no debe poder reanudarse contra producción por accidente.
    ambiente: str = "PRUEBAS"
    estado: str = "BORRADOR"
    creado_en: str = field(default_factory=_ahora)


@dataclass
class Solicitud:
    """Una solicitud de pago, tal como se capturará en SIPP."""

    id: str = field(default_factory=_nuevo_id)
    lote_id: str = ""
    orden: int = 0
    empresa: str = ""
    sucursal: str = ""
    tipo_pago: str = "Pago Extraordinario"
    tipo_beneficiario: str = "Acreedor"
    # 1 = se marca "No Registrado" y se capturan RFC/razón social/correo.
    beneficiario_nuevo: int = 0
    beneficiario_folio: str = ""
    beneficiario_rfc: str = ""
    beneficiario_nombre: str = ""
    beneficiario_correo: str = ""
    cuenta_clabe: str = ""
    cuenta_banco: str = ""
    cuenta_titular: str = ""
    cuenta_tipo_transf: str = "SPEI"
    forma_pago: str = "Transferencia"
    tipo_gasto: str = "No Deducible"
    fecha_pago: str = ""          # 'DD/MM/AAAA' (comun.FORMATO_FECHA)
    moneda: str = "Pesos (MXN)"
    descripcion: str = ""
    importe_total: float = 0.0    # derivado de las partidas CONCEPTO
    origen: str = "MANUAL"        # 'CFDI' | 'EXCEL' | 'MANUAL'
    parada: str = "LLENADA"
    estado: str = "PENDIENTE"
    folio_sipp: str = ""
    clave_idempotencia: str = ""
    intentos: int = 0
    error_msg: str = ""
    actualizado_en: str = field(default_factory=_ahora)


@dataclass
class Partida:
    """Un renglón del desglose. `clase` decide qué grid de SIPP la recibe."""

    id: str = field(default_factory=_nuevo_id)
    solicitud_id: str = ""
    clase: str = CONCEPTO
    orden: int = 0
    # clase = CONCEPTO
    concepto_nombre: str = ""
    # clase = INSUMO
    tipo_compra: str = ""
    insumo_id: str = ""
    insumo_nombre: str = ""
    cantidad: float = 1.0
    precio_unitario: float = 0.0
    centro_costos: str = ""
    cuenta_contable: str = ""
    # comunes
    descripcion: str = ""
    importe: float = 0.0
    origen: str = "MANUAL"


@dataclass
class Documento:
    """Un archivo asociado a una solicitud o al lote (CFDI, carátula, Vo.Bo.)."""

    id: str = field(default_factory=_nuevo_id)
    solicitud_id: str = ""
    lote_id: str = ""
    tipo: str = ""          # CFDI_XML | CFDI_PDF | CARATULA | VOBO | EXCEL | ANEXO
    ruta: str = ""
    hash_sha256: str = ""
    uuid_cfdi: str = ""
    cargado_en: str = field(default_factory=_ahora)


@dataclass
class EntradaBitacora:
    """Una línea del historial de ejecución, con su evidencia si la hubo."""

    id: int | None = None
    solicitud_id: str = ""
    momento: str = field(default_factory=_ahora)
    paso: str = ""
    nivel: str = "INFO"     # INFO | WARN | ERROR
    mensaje: str = ""
    captura_ruta: str = ""


# --------------------------------------------------------------------------- #
#  Esquema
# --------------------------------------------------------------------------- #
# Fuente única de columnas: de aquí salen el CREATE, el INSERT, el UPDATE y las
# migraciones. El orden importa solo para el CREATE.
_TABLAS: dict[str, dict[str, str]] = {
    "lote": {
        "id": "TEXT PRIMARY KEY",
        "nombre": "TEXT NOT NULL DEFAULT ''",
        "parada_default": "TEXT NOT NULL DEFAULT 'LLENADA'",
        "ambiente": "TEXT NOT NULL DEFAULT 'PRUEBAS'",
        "estado": "TEXT NOT NULL DEFAULT 'BORRADOR'",
        "creado_en": "TEXT NOT NULL DEFAULT ''",
    },
    "solicitud": {
        "id": "TEXT PRIMARY KEY",
        "lote_id": "TEXT NOT NULL DEFAULT ''",
        "orden": "INTEGER NOT NULL DEFAULT 0",
        "empresa": "TEXT NOT NULL DEFAULT ''",
        "sucursal": "TEXT NOT NULL DEFAULT ''",
        "tipo_pago": "TEXT NOT NULL DEFAULT 'Pago Extraordinario'",
        "tipo_beneficiario": "TEXT NOT NULL DEFAULT 'Acreedor'",
        "beneficiario_nuevo": "INTEGER NOT NULL DEFAULT 0",
        "beneficiario_folio": "TEXT NOT NULL DEFAULT ''",
        "beneficiario_rfc": "TEXT NOT NULL DEFAULT ''",
        "beneficiario_nombre": "TEXT NOT NULL DEFAULT ''",
        "beneficiario_correo": "TEXT NOT NULL DEFAULT ''",
        "cuenta_clabe": "TEXT NOT NULL DEFAULT ''",
        "cuenta_banco": "TEXT NOT NULL DEFAULT ''",
        "cuenta_titular": "TEXT NOT NULL DEFAULT ''",
        "cuenta_tipo_transf": "TEXT NOT NULL DEFAULT 'SPEI'",
        "forma_pago": "TEXT NOT NULL DEFAULT 'Transferencia'",
        "tipo_gasto": "TEXT NOT NULL DEFAULT 'No Deducible'",
        "fecha_pago": "TEXT NOT NULL DEFAULT ''",
        "moneda": "TEXT NOT NULL DEFAULT 'Pesos (MXN)'",
        "descripcion": "TEXT NOT NULL DEFAULT ''",
        "importe_total": "REAL NOT NULL DEFAULT 0",
        "origen": "TEXT NOT NULL DEFAULT 'MANUAL'",
        "parada": "TEXT NOT NULL DEFAULT 'LLENADA'",
        "estado": "TEXT NOT NULL DEFAULT 'PENDIENTE'",
        "folio_sipp": "TEXT NOT NULL DEFAULT ''",
        "clave_idempotencia": "TEXT NOT NULL DEFAULT ''",
        "intentos": "INTEGER NOT NULL DEFAULT 0",
        "error_msg": "TEXT NOT NULL DEFAULT ''",
        "actualizado_en": "TEXT NOT NULL DEFAULT ''",
    },
    "partida": {
        "id": "TEXT PRIMARY KEY",
        "solicitud_id": "TEXT NOT NULL DEFAULT ''",
        "clase": "TEXT NOT NULL DEFAULT 'CONCEPTO'",
        "orden": "INTEGER NOT NULL DEFAULT 0",
        "concepto_nombre": "TEXT NOT NULL DEFAULT ''",
        "tipo_compra": "TEXT NOT NULL DEFAULT ''",
        "insumo_id": "TEXT NOT NULL DEFAULT ''",
        "insumo_nombre": "TEXT NOT NULL DEFAULT ''",
        "cantidad": "REAL NOT NULL DEFAULT 1",
        "precio_unitario": "REAL NOT NULL DEFAULT 0",
        "centro_costos": "TEXT NOT NULL DEFAULT ''",
        "cuenta_contable": "TEXT NOT NULL DEFAULT ''",
        "descripcion": "TEXT NOT NULL DEFAULT ''",
        "importe": "REAL NOT NULL DEFAULT 0",
        "origen": "TEXT NOT NULL DEFAULT 'MANUAL'",
    },
    "documento": {
        "id": "TEXT PRIMARY KEY",
        "solicitud_id": "TEXT NOT NULL DEFAULT ''",
        "lote_id": "TEXT NOT NULL DEFAULT ''",
        "tipo": "TEXT NOT NULL DEFAULT ''",
        "ruta": "TEXT NOT NULL DEFAULT ''",
        "hash_sha256": "TEXT NOT NULL DEFAULT ''",
        "uuid_cfdi": "TEXT NOT NULL DEFAULT ''",
        "cargado_en": "TEXT NOT NULL DEFAULT ''",
    },
    # Catálogo local de conceptos de pago. SIPP los asigna por empresa y no
    # expone forma de consultarlos sin entrar al formulario, así que se importan
    # una vez y quedan aquí para alimentar los desplegables de la plantilla y
    # de la captura. Ver core/conceptos.py.
    #
    # Es una lista ÚNICA, no una por empresa: el criterio del área es que todas
    # las empresas manejen los mismos conceptos, y el de Abastecedora sirve de
    # base. `empresa` se conserva solo como rastro de dónde se importó cada uno.
    "concepto_pago": {
        "id": "TEXT PRIMARY KEY",
        "nombre": "TEXT NOT NULL DEFAULT ''",
        "empresa": "TEXT NOT NULL DEFAULT ''",        # informativo, no es clave
        "origen": "TEXT NOT NULL DEFAULT 'MANUAL'",   # 'SIPP' | 'MANUAL'
        "nota": "TEXT NOT NULL DEFAULT ''",
        "creado_en": "TEXT NOT NULL DEFAULT ''",
    },
    # Copia del estado de un lote antes de una operación masiva, para poder
    # deshacerla. Se guarda como JSON: es un respaldo puntual, no algo que se
    # consulte, y no vale la pena normalizarlo en tablas.
    "snapshot": {
        "id": "TEXT PRIMARY KEY",
        "lote_id": "TEXT NOT NULL DEFAULT ''",
        "motivo": "TEXT NOT NULL DEFAULT ''",
        "datos": "TEXT NOT NULL DEFAULT ''",
        "creado_en": "TEXT NOT NULL DEFAULT ''",
    },
    "bitacora": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "solicitud_id": "TEXT NOT NULL DEFAULT ''",
        "momento": "TEXT NOT NULL DEFAULT ''",
        "paso": "TEXT NOT NULL DEFAULT ''",
        "nivel": "TEXT NOT NULL DEFAULT 'INFO'",
        "mensaje": "TEXT NOT NULL DEFAULT ''",
        "captura_ruta": "TEXT NOT NULL DEFAULT ''",
    },
}

_INDICES = [
    # La clave de negocio, única **por lote**: si dos filas del mismo lote
    # producen la misma clave, es el mismo pago capturado dos veces y el segundo
    # INSERT debe fallar.
    #
    # El alcance es el lote y no toda la base a propósito. Un lote es una unidad
    # de trabajo: rehacerlo desde cero —reimportar las mismas carátulas en un
    # lote limpio— es una operación legítima que un índice global bloquearía sin
    # explicación útil. La protección real contra pagar dos veces no vive aquí,
    # sino en `FlujoSolicitudPago.buscar_existente`, que consulta el propio SIPP
    # antes de capturar: esa sí atraviesa lotes, reinstalaciones y equipos.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_idem_lote "
    "ON solicitud(lote_id, clave_idempotencia)",
    "CREATE INDEX IF NOT EXISTS idx_sol_lote ON solicitud(lote_id, orden)",
    "CREATE INDEX IF NOT EXISTS idx_part_sol "
    "ON partida(solicitud_id, clase, orden)",
    "CREATE INDEX IF NOT EXISTS idx_doc_hash ON documento(hash_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_doc_sol ON documento(solicitud_id)",
    "CREATE INDEX IF NOT EXISTS idx_bit_sol ON bitacora(solicitud_id, id)",
    # Un concepto por NOMBRE, sin importar de qué empresa se importó: el
    # catálogo es una lista única para todo el grupo.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_concepto_nombre "
    "ON concepto_pago(nombre)",
]


class ClaveDuplicada(Exception):
    """Ya existe una solicitud con esa clave de idempotencia en la base."""


# --------------------------------------------------------------------------- #
#  Conexión e inicialización
# --------------------------------------------------------------------------- #
def conectar() -> sqlite3.Connection:
    """Abre la base con claves foráneas activas y filas accesibles por nombre."""
    os.makedirs(os.path.dirname(RUTA_DB), exist_ok=True)
    con = sqlite3.connect(RUTA_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def inicializar() -> None:
    """Crea las tablas si no existen y agrega las columnas que falten.

    Las migraciones son incrementales y solo AGREGAN: nunca se borra ni se
    renombra una columna, para que una versión vieja de la app pueda seguir
    leyendo una base ya migrada sin tronar.
    """
    with conectar() as con:
        for tabla, columnas in _TABLAS.items():
            defs = ", ".join(f"{c} {t}" for c, t in columnas.items())
            con.execute(f"CREATE TABLE IF NOT EXISTS {tabla} ({defs})")
            existentes = {
                fila["name"]
                for fila in con.execute(f"PRAGMA table_info({tabla})")
            }
            for col, tipo in columnas.items():
                if col in existentes:
                    continue
                # PRIMARY KEY/AUTOINCREMENT no se pueden agregar por ALTER TABLE;
                # tampoco hace falta: si la tabla existe, ya tiene su clave.
                if "PRIMARY KEY" in tipo:
                    continue
                con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
        # Migración: el índice de idempotencia era único en TODA la base y pasó
        # a serlo por lote. Se retira el viejo antes de crear el nuevo, o el
        # anterior seguiría bloqueando lo que el nuevo permite.
        con.execute("DROP INDEX IF EXISTS idx_idem")
        # Migración: el catálogo de conceptos era único por (empresa, nombre) y
        # pasó a serlo por nombre. Se quita el índice viejo y se dejan las
        # entradas repetidas en una sola, quedándose con la importada de SIPP
        # —que es la verificada— cuando hay de las dos.
        con.execute("DROP INDEX IF EXISTS idx_concepto")
        # De cada nombre repetido se conserva UNA entrada, prefiriendo la
        # importada de SIPP (la verificada) y, a igualdad, la más antigua. Tiene
        # que ir ANTES de crear el índice único, o su creación fallaría.
        con.execute(
            "DELETE FROM concepto_pago WHERE rowid NOT IN ("
            "  SELECT ("
            "    SELECT c2.rowid FROM concepto_pago c2"
            "    WHERE c2.nombre = c.nombre"
            "    ORDER BY CASE WHEN c2.origen = 'SIPP' THEN 0 ELSE 1 END,"
            "             c2.rowid"
            "    LIMIT 1)"
            "  FROM concepto_pago c)")
        for sql in _INDICES:
            con.execute(sql)


# --------------------------------------------------------------------------- #
#  Helpers de mapeo dataclass <-> fila
# --------------------------------------------------------------------------- #
def _columnas(tabla: str, incluir_id: bool = True) -> list[str]:
    cols = list(_TABLAS[tabla].keys())
    return cols if incluir_id else [c for c in cols if c != "id"]


def _a_dict(obj) -> dict:
    return asdict(obj)


def _desde_fila(clase, fila: sqlite3.Row):
    """Construye la dataclass a partir de una fila, ignorando columnas que la
    versión actual del código ya no conoce (base migrada por una versión nueva)."""
    nombres = {f.name for f in fields(clase)}
    return clase(**{k: fila[k] for k in fila.keys() if k in nombres})


def _guardar(con: sqlite3.Connection, tabla: str, obj) -> None:
    """INSERT OR REPLACE genérico a partir de la lista de columnas de la tabla."""
    cols = _columnas(tabla)
    datos = _a_dict(obj)
    valores = [datos.get(c) for c in cols]
    marcas = ", ".join("?" * len(cols))
    con.execute(
        f"INSERT OR REPLACE INTO {tabla} ({', '.join(cols)}) VALUES ({marcas})",
        valores,
    )


# --------------------------------------------------------------------------- #
#  Clave de idempotencia
# --------------------------------------------------------------------------- #
def calcular_clave(solicitud: Solicitud, partidas: list[Partida]) -> str:
    """Huella de la solicitud COMPLETA, partidas incluidas.

    Incluir las partidas es deliberado: si tras un intento fallido se corrige el
    desglose, ya no es el mismo pago y debe reintentarse limpio en vez de que el
    robot lo dé por capturado. Se excluyen los campos de ejecución (estado,
    folio, intentos) porque cambian sin que cambie el pago.
    """
    base = [
        solicitud.empresa,
        solicitud.sucursal,
        solicitud.tipo_pago,
        solicitud.tipo_beneficiario,
        solicitud.beneficiario_rfc.strip().upper(),
        solicitud.beneficiario_nombre.strip().upper(),
        solicitud.cuenta_clabe.strip(),
        solicitud.fecha_pago,
        solicitud.moneda,
        f"{solicitud.importe_total:.2f}",
        solicitud.descripcion.strip(),
    ]
    for p in sorted(partidas, key=lambda x: (x.clase, x.orden)):
        base += [
            p.clase, p.concepto_nombre.strip().upper(),
            p.insumo_id.strip(), p.centro_costos.strip(),
            f"{p.importe:.2f}",
        ]
    crudo = "|".join(base)
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()


def total_conceptos(partidas: list[Partida]) -> float:
    """Suma de las partidas de clase CONCEPTO."""
    return round(sum(p.importe for p in partidas if p.clase == CONCEPTO), 2)


def total_desglose(tipo_beneficiario: str, partidas: list[Partida]) -> float:
    """Importe de la solicitud, sumando la clase de partida que le corresponde.

    En Pago Extraordinario el campo 'Cantidad a Pagar' está deshabilitado y lo
    calcula SIPP a partir del desglose. Pero **cuál desglose depende del tipo de
    beneficiario**: a un Proveedor se le capturan Insumos & Servicios y a un
    Deudor o Acreedor, Conceptos de Pago. Son pestañas distintas y excluyentes,
    así que sumar las dos clases daría el doble.
    """
    clase = catalogos.clase_desglose(tipo_beneficiario)
    return round(sum(p.importe for p in partidas if p.clase == clase), 2)


# --------------------------------------------------------------------------- #
#  Lotes
# --------------------------------------------------------------------------- #
def guardar_lote(lote: Lote) -> Lote:
    with conectar() as con:
        _guardar(con, "lote", lote)
    return lote


def listar_lotes() -> list[Lote]:
    with conectar() as con:
        filas = con.execute(
            "SELECT * FROM lote ORDER BY creado_en DESC").fetchall()
    return [_desde_fila(Lote, f) for f in filas]


def obtener_lote(lote_id: str) -> Lote | None:
    with conectar() as con:
        fila = con.execute(
            "SELECT * FROM lote WHERE id = ?", (lote_id,)).fetchone()
    return _desde_fila(Lote, fila) if fila else None


def borrar_lote(lote_id: str) -> None:
    """Borra el lote con todo lo que cuelga de él (en cascada manual, para no
    depender de que la base vieja tenga declaradas las claves foráneas)."""
    with conectar() as con:
        ids = [f["id"] for f in con.execute(
            "SELECT id FROM solicitud WHERE lote_id = ?", (lote_id,))]
        for sid in ids:
            con.execute("DELETE FROM partida WHERE solicitud_id = ?", (sid,))
            con.execute("DELETE FROM bitacora WHERE solicitud_id = ?", (sid,))
            con.execute("DELETE FROM documento WHERE solicitud_id = ?", (sid,))
        con.execute("DELETE FROM solicitud WHERE lote_id = ?", (lote_id,))
        con.execute("DELETE FROM documento WHERE lote_id = ?", (lote_id,))
        con.execute("DELETE FROM lote WHERE id = ?", (lote_id,))


# --------------------------------------------------------------------------- #
#  Solicitudes y partidas
# --------------------------------------------------------------------------- #
def guardar_solicitud(solicitud: Solicitud,
                      partidas: list[Partida] | None = None) -> Solicitud:
    """Guarda la solicitud con su desglose, en UNA transacción.

    Recalcula `importe_total` y `clave_idempotencia` a partir de las partidas:
    ninguna pantalla debería fijarlos a mano, para que no puedan quedar
    desalineados con el desglose que sí se va a capturar.

    Lanza `ClaveDuplicada` si la clave ya existe en OTRA solicitud.
    """
    partidas = list(partidas or [])
    for i, p in enumerate(partidas):
        p.solicitud_id = solicitud.id
        p.orden = i
    solicitud.importe_total = total_desglose(
        solicitud.tipo_beneficiario, partidas)
    solicitud.clave_idempotencia = calcular_clave(solicitud, partidas)
    solicitud.actualizado_en = _ahora()

    with conectar() as con:
        choque = con.execute(
            "SELECT id FROM solicitud "
            "WHERE lote_id = ? AND clave_idempotencia = ? AND id != ?",
            (solicitud.lote_id, solicitud.clave_idempotencia, solicitud.id),
        ).fetchone()
        if choque:
            raise ClaveDuplicada(
                "Ya existe una solicitud idéntica en este lote "
                f"(beneficiario '{solicitud.beneficiario_nombre}', "
                f"importe {solicitud.importe_total:,.2f}).")
        _guardar(con, "solicitud", solicitud)
        # El desglose se reemplaza completo: es más simple y más seguro que
        # reconciliar altas/bajas, y son pocos renglones por solicitud.
        con.execute("DELETE FROM partida WHERE solicitud_id = ?", (solicitud.id,))
        for p in partidas:
            _guardar(con, "partida", p)
    return solicitud


def listar_solicitudes(lote_id: str | None = None) -> list[Solicitud]:
    sql = "SELECT * FROM solicitud"
    args: tuple = ()
    if lote_id:
        sql += " WHERE lote_id = ?"
        args = (lote_id,)
    sql += " ORDER BY orden, actualizado_en"
    with conectar() as con:
        filas = con.execute(sql, args).fetchall()
    return [_desde_fila(Solicitud, f) for f in filas]


def obtener_solicitud(solicitud_id: str) -> Solicitud | None:
    with conectar() as con:
        fila = con.execute(
            "SELECT * FROM solicitud WHERE id = ?", (solicitud_id,)).fetchone()
    return _desde_fila(Solicitud, fila) if fila else None


def listar_partidas(solicitud_id: str, clase: str | None = None) -> list[Partida]:
    sql = "SELECT * FROM partida WHERE solicitud_id = ?"
    args: list = [solicitud_id]
    if clase:
        sql += " AND clase = ?"
        args.append(clase)
    sql += " ORDER BY clase, orden"
    with conectar() as con:
        filas = con.execute(sql, tuple(args)).fetchall()
    return [_desde_fila(Partida, f) for f in filas]


def borrar_solicitud(solicitud_id: str) -> None:
    with conectar() as con:
        con.execute("DELETE FROM partida WHERE solicitud_id = ?", (solicitud_id,))
        con.execute("DELETE FROM bitacora WHERE solicitud_id = ?", (solicitud_id,))
        con.execute("DELETE FROM documento WHERE solicitud_id = ?", (solicitud_id,))
        con.execute("DELETE FROM solicitud WHERE id = ?", (solicitud_id,))


def actualizar_estado(solicitud_id: str, estado: str, *,
                      folio_sipp: str | None = None,
                      error_msg: str | None = None,
                      sumar_intento: bool = False) -> None:
    """Persiste el avance de una solicitud.

    El motor RPA llama esto ANTES de ejecutar el paso siguiente, para que un
    corte de luz a media captura deje la base contando la verdad y no un estado
    optimista. Por lo mismo, el folio se guarda en cuanto aparece en pantalla.
    """
    campos = ["estado = ?", "actualizado_en = ?"]
    args: list = [estado, _ahora()]
    if folio_sipp is not None:
        campos.append("folio_sipp = ?")
        args.append(folio_sipp)
    if error_msg is not None:
        campos.append("error_msg = ?")
        args.append(error_msg)
    if sumar_intento:
        campos.append("intentos = intentos + 1")
    args.append(solicitud_id)
    with conectar() as con:
        con.execute(
            f"UPDATE solicitud SET {', '.join(campos)} WHERE id = ?", args)


def actualizar_parada(solicitud_id: str, parada: str) -> None:
    """Cambia el punto de parada de una fila.

    Se lee al momento de procesar cada solicitud, no al iniciar el lote: por eso
    esto puede pasar con el proceso corriendo.
    """
    with conectar() as con:
        con.execute(
            "UPDATE solicitud SET parada = ?, actualizado_en = ? WHERE id = ?",
            (parada, _ahora(), solicitud_id))


# --------------------------------------------------------------------------- #
#  Documentos
# --------------------------------------------------------------------------- #
def hash_archivo(ruta: str) -> str:
    """SHA-256 del archivo, para no reprocesar el mismo documento dos veces."""
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for bloque in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def guardar_documento(doc: Documento) -> Documento:
    with conectar() as con:
        _guardar(con, "documento", doc)
    return doc


def documento_por_hash(hash_sha256: str) -> Documento | None:
    with conectar() as con:
        fila = con.execute(
            "SELECT * FROM documento WHERE hash_sha256 = ?",
            (hash_sha256,)).fetchone()
    return _desde_fila(Documento, fila) if fila else None


def borrar_documento(documento_id: str) -> None:
    with conectar() as con:
        con.execute("DELETE FROM documento WHERE id = ?", (documento_id,))


def listar_documentos(solicitud_id: str | None = None,
                      lote_id: str | None = None) -> list[Documento]:
    sql = "SELECT * FROM documento WHERE 1 = 1"
    args: list = []
    if solicitud_id:
        sql += " AND solicitud_id = ?"
        args.append(solicitud_id)
    if lote_id:
        sql += " AND lote_id = ?"
        args.append(lote_id)
    sql += " ORDER BY cargado_en"
    with conectar() as con:
        filas = con.execute(sql, tuple(args)).fetchall()
    return [_desde_fila(Documento, f) for f in filas]


# --------------------------------------------------------------------------- #
#  Bitácora
# --------------------------------------------------------------------------- #
def registrar(solicitud_id: str, paso: str, mensaje: str = "",
              nivel: str = "INFO", captura_ruta: str = "") -> None:
    """Agrega una línea a la bitácora. Nunca debe tumbar al motor: si la base
    está bloqueada o el disco lleno, el lote sigue."""
    try:
        with conectar() as con:
            con.execute(
                "INSERT INTO bitacora "
                "(solicitud_id, momento, paso, nivel, mensaje, captura_ruta) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (solicitud_id, _ahora(), paso, nivel, mensaje, captura_ruta))
    except sqlite3.Error:
        pass


def listar_bitacora(solicitud_id: str | None = None,
                    limite: int = 500) -> list[EntradaBitacora]:
    sql = "SELECT * FROM bitacora"
    args: list = []
    if solicitud_id:
        sql += " WHERE solicitud_id = ?"
        args.append(solicitud_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limite)
    with conectar() as con:
        filas = con.execute(sql, tuple(args)).fetchall()
    return [_desde_fila(EntradaBitacora, f) for f in filas]


# --------------------------------------------------------------------------- #
#  Snapshots: deshacer una operación masiva
# --------------------------------------------------------------------------- #
# Una asignación masiva puede tocar cien solicitudes de una vez. Sin forma de
# volver atrás, un error de criterio —el importe mal repartido, el concepto que
# no era— obliga a rehacer el lote entero a mano. Por eso se guarda el estado
# ANTES de aplicar, y se conserva solo el último: es un «deshacer», no un
# historial de versiones.
def guardar_snapshot(lote_id: str, motivo: str) -> str:
    """Fotografía las solicitudes y partidas del lote. Devuelve su id."""
    contenido = {
        "solicitudes": [asdict(s) for s in listar_solicitudes(lote_id)],
        "partidas": {s.id: [asdict(p) for p in listar_partidas(s.id)]
                     for s in listar_solicitudes(lote_id)},
    }
    snap_id = _nuevo_id()
    with conectar() as con:
        # Solo el último: guardar todos convertiría esto en un historial que
        # nadie mantiene y que solo hace crecer la base.
        con.execute("DELETE FROM snapshot WHERE lote_id = ?", (lote_id,))
        con.execute(
            "INSERT INTO snapshot (id, lote_id, motivo, datos, creado_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (snap_id, lote_id, motivo,
             json.dumps(contenido, ensure_ascii=False), _ahora()))
    return snap_id


def hay_snapshot(lote_id: str) -> dict | None:
    """Datos del snapshot disponible para deshacer, o None."""
    with conectar() as con:
        fila = con.execute(
            "SELECT id, motivo, creado_en FROM snapshot WHERE lote_id = ?",
            (lote_id,)).fetchone()
    return dict(fila) if fila else None


def restaurar_snapshot(lote_id: str) -> bool:
    """Devuelve el lote al estado del snapshot. False si no había ninguno.

    Reemplaza solicitudes y partidas por completo, incluidas las que la
    operación hubiera creado: deshacer a medias sería peor que no deshacer.
    """
    with conectar() as con:
        fila = con.execute(
            "SELECT datos FROM snapshot WHERE lote_id = ?", (lote_id,)).fetchone()
        if not fila:
            return False
        contenido = json.loads(fila["datos"])

        # Se borra el estado actual del lote y se repone el guardado.
        ids = [f["id"] for f in con.execute(
            "SELECT id FROM solicitud WHERE lote_id = ?", (lote_id,))]
        for sid in ids:
            con.execute("DELETE FROM partida WHERE solicitud_id = ?", (sid,))
        con.execute("DELETE FROM solicitud WHERE lote_id = ?", (lote_id,))

        for datos in contenido.get("solicitudes", []):
            _guardar(con, "solicitud", Solicitud(**datos))
        for partidas in contenido.get("partidas", {}).values():
            for datos in partidas:
                _guardar(con, "partida", Partida(**datos))
        # El snapshot se consume: deshacer dos veces seguidas no tiene sentido.
        con.execute("DELETE FROM snapshot WHERE lote_id = ?", (lote_id,))
    return True


def solicitud_en_snapshot(lote_id: str, solicitud_id: str) -> bool:
    """True si esa solicitud aparece en el snapshot y se puede revertir sola."""
    with conectar() as con:
        fila = con.execute(
            "SELECT datos FROM snapshot WHERE lote_id = ?", (lote_id,)).fetchone()
    if not fila:
        return False
    return any(s["id"] == solicitud_id
               for s in json.loads(fila["datos"]).get("solicitudes", []))


def restaurar_solicitud_snapshot(lote_id: str, solicitud_id: str) -> bool:
    """Devuelve UNA solicitud al estado del snapshot, dejando el resto como está.

    Es el «deshacer» de una fila, y se comporta distinto del global a propósito:
    **no consume el snapshot**. Una asignación masiva toca decenas de
    solicitudes y lo normal es que la mayoría queden bien y una o dos no; si
    revertir la primera borrara el snapshot, las demás se quedarían sin vuelta
    atrás. El snapshot se conserva hasta que se deshaga todo el lote o hasta que
    otra operación masiva lo reemplace.

    Devuelve False si no hay snapshot o si esa solicitud no está en él (por
    ejemplo, si se creó después): no hay estado anterior al que volver.
    """
    with conectar() as con:
        fila = con.execute(
            "SELECT datos FROM snapshot WHERE lote_id = ?", (lote_id,)).fetchone()
        if not fila:
            return False
        contenido = json.loads(fila["datos"])
        datos = next((s for s in contenido.get("solicitudes", [])
                      if s["id"] == solicitud_id), None)
        if datos is None:
            return False

        con.execute("DELETE FROM partida WHERE solicitud_id = ?",
                    (solicitud_id,))
        con.execute("DELETE FROM solicitud WHERE id = ?", (solicitud_id,))
        _guardar(con, "solicitud", Solicitud(**datos))
        for p in contenido.get("partidas", {}).get(solicitud_id, []):
            _guardar(con, "partida", Partida(**p))
    return True


# --------------------------------------------------------------------------- #
#  Estado de la interfaz (columnas de la tabla, filtros)
# --------------------------------------------------------------------------- #
# Vive en la base y no en preferencias.json porque acompaña a los datos: si el
# usuario reordena columnas, debe seguir igual tras una actualización de la app.
def guardar_estado_ui(clave: str, valor) -> None:
    with conectar() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS estado_ui "
            "(clave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
        con.execute(
            "INSERT OR REPLACE INTO estado_ui (clave, valor) VALUES (?, ?)",
            (clave, json.dumps(valor, ensure_ascii=False)))


def cargar_estado_ui(clave: str, defecto=None):
    try:
        with conectar() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS estado_ui "
                "(clave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
            fila = con.execute(
                "SELECT valor FROM estado_ui WHERE clave = ?", (clave,)).fetchone()
        return json.loads(fila["valor"]) if fila else defecto
    except (sqlite3.Error, ValueError):
        return defecto


# --------------------------------------------------------------------------- #
#  Resumen para el encabezado del lote
# --------------------------------------------------------------------------- #
def resumen_lote(lote_id: str) -> dict:
    """Conteo por estado e importe total del lote, para la barra de progreso."""
    with conectar() as con:
        filas = con.execute(
            "SELECT estado, COUNT(*) AS n, SUM(importe_total) AS importe "
            "FROM solicitud WHERE lote_id = ? GROUP BY estado", (lote_id,)
        ).fetchall()
    por_estado = {f["estado"]: f["n"] for f in filas}
    return {
        "por_estado": por_estado,
        "total": sum(por_estado.values()),
        "importe": round(sum(f["importe"] or 0 for f in filas), 2),
    }
