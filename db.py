"""
db.py · Acceso a datos del Taller mecánico.

Todo lo que toca PostgreSQL pasa por aquí:
  - Lee la configuración de variables de entorno (las pone Compose desde .env).
  - Valida los datos ANTES de enviarlos (primera barrera). La base de datos
    tiene además sus propias restricciones CHECK / UNIQUE / FK (segunda barrera).
  - Usa siempre SQL parametrizado (%s): nunca se concatenan valores en el SQL.
  - Cada operación de escritura va en UNA transacción: o se guarda todo o no
    se guarda nada (rollback automático si algo falla).
  - Nunca hace UPDATE ni DELETE: un duplicado se rechaza, no sobrescribe.

Nota: si una transacción se revierte, el contador de id (IDENTITY) no retrocede,
así que pueden verse huecos en los id. Es normal en PostgreSQL y no indica datos perdidos.

Cada función abre su propia conexión y la cierra al terminar. Para una app
pequeña es lo más sencillo y robusto (no quedan conexiones colgadas).
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

# Si se ejecuta fuera de Docker (python app.py en local), carga el .env.
# Dentro del contenedor las variables ya llegan desde Compose y esto no hace nada.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


# --------------------------------------------------------------------------
# Reglas de negocio (las mismas que comprueba la base de datos)
# --------------------------------------------------------------------------
ESQUEMA = "taller"
LETRAS_MATRICULA = "BCDFGHJKLMNPRSTVWXYZ"  # sin vocales, sin Ñ ni Q
RE_MATRICULA = re.compile(rf"^[0-9]{{4}}[{LETRAS_MATRICULA}]{{3}}$")
RE_TELEFONO = re.compile(r"^[6789][0-9]{8}$")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
ANIO_MIN = 1950
KM_MAX = 2_000_000
PRESUPUESTO_MAX = Decimal("99999999.99")
ESTADOS = ("abierta", "en_reparacion", "terminada", "entregada")
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


# --------------------------------------------------------------------------
# Errores propios (la app los muestra tal cual al usuario)
# --------------------------------------------------------------------------
class ErrorConfiguracion(Exception):
    """Falta o es incorrecta alguna variable del .env."""


class ErrorValidacion(Exception):
    """Datos inválidos. No se ha guardado nada."""

    def __init__(self, errores: list[str]):
        self.errores = errores
        super().__init__("; ".join(errores))


class ErrorDuplicado(Exception):
    """Violación del campo único (matrícula). No se ha guardado nada."""


class ErrorNoEncontrado(Exception):
    """El cliente o vehículo indicado no existe."""


# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str
    sslrootcert: str

    def parametros(self) -> dict:
        p = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "sslmode": self.sslmode,
        }
        if self.sslrootcert:
            p["sslrootcert"] = self.sslrootcert
        return p

    def resumen(self) -> dict:
        """Datos de conexión SIN la contraseña (para mostrar o documentar)."""
        return {
            "host": self.host,
            "puerto": self.port,
            "base": self.dbname,
            "usuario": self.user,
            "sslmode": self.sslmode,
            "sslrootcert": self.sslrootcert or "(ninguno)",
        }


def cargar_config(usuario: str | None = None, password: str | None = None) -> Config:
    """Lee DB_* del entorno. `usuario`/`password` permiten conectar como otro
    usuario (lo usa inicializar.py con la credencial de administrador)."""
    faltan = [v for v in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if not os.environ.get(v)]
    if usuario:
        faltan = [v for v in faltan if v not in ("DB_USER", "DB_PASSWORD")]
    if faltan:
        raise ErrorConfiguracion(
            "Faltan variables de configuración: " + ", ".join(faltan)
            + ". Genera el .env con 'python3 configurar.py local' o 'python3 configurar.py aws'."
        )

    try:
        port = int(os.environ.get("DB_PORT", "5432"))
    except ValueError:
        raise ErrorConfiguracion("DB_PORT debe ser un número (normalmente 5432).")

    sslmode = os.environ.get("DB_SSLMODE", "verify-full").strip()
    if sslmode not in SSLMODES:
        raise ErrorConfiguracion(f"DB_SSLMODE='{sslmode}' no es válido. Usa uno de: {', '.join(SSLMODES)}.")

    sslrootcert = os.environ.get("DB_SSLROOTCERT", "").strip()
    if sslmode in ("verify-ca", "verify-full"):
        if not sslrootcert:
            raise ErrorConfiguracion(f"Con DB_SSLMODE={sslmode} hay que indicar DB_SSLROOTCERT (certificado de la CA).")
        if not Path(sslrootcert).is_file():
            raise ErrorConfiguracion(
                f"No existe el certificado {sslrootcert}. Descárgalo en ./certs "
                "(python3 configurar.py aws lo hace) y comprueba el volumen en compose.yaml."
            )

    return Config(
        host=os.environ["DB_HOST"].strip(),
        port=port,
        dbname=os.environ["DB_NAME"].strip(),
        user=(usuario or os.environ["DB_USER"]).strip(),
        password=password if password is not None else os.environ["DB_PASSWORD"],
        sslmode=sslmode,
        sslrootcert=sslrootcert,
    )


def conectar(cfg: Config | None = None) -> psycopg.Connection:
    """Abre una conexión nueva. Usar siempre con `with conectar() as conn:`
    (al salir: commit si todo fue bien, rollback si hubo excepción, y cierre)."""
    cfg = cfg or cargar_config()
    return psycopg.connect(
        **cfg.parametros(),
        connect_timeout=5,
        application_name="taller-app",
        row_factory=dict_row,
        options=f"-c search_path={ESQUEMA}",
    )


# --------------------------------------------------------------------------
# Validación (primera barrera, en la aplicación)
# --------------------------------------------------------------------------
def normalizar_matricula(texto: str | None) -> str:
    """'1234 bcd', '1234-BCD' → '1234BCD'. Así el UNIQUE detecta duplicados
    aunque se escriban con espacios, guiones o minúsculas."""
    return re.sub(r"[\s\-]", "", (texto or "")).upper()


def formatear_matricula(m: str) -> str:
    """'1234BCD' → '1234 BCD'."""
    m = (m or "").strip()
    return f"{m[:4]} {m[4:]}" if len(m) == 7 else m


def _texto(valor) -> str:
    return (valor or "").strip()


def validar_cliente(datos: dict) -> tuple[dict, list[str]]:
    errores = []
    nombre = _texto(datos.get("nombre"))
    telefono = re.sub(r"[\s\-]", "", _texto(datos.get("telefono")))
    if telefono.startswith("+34"):
        telefono = telefono[3:]
    email = _texto(datos.get("email")) or None

    if len(nombre) < 2 or len(nombre) > 100:
        errores.append("Nombre del cliente: entre 2 y 100 caracteres.")
    if not RE_TELEFONO.match(telefono):
        errores.append("Teléfono: 9 dígitos que empiecen por 6, 7, 8 o 9 (p. ej. 612345678).")
    if email and (len(email) > 120 or not RE_EMAIL.match(email)):
        errores.append("Email: formato no válido (p. ej. nombre@dominio.es).")
    return {"nombre": nombre, "telefono": telefono, "email": email}, errores


def validar_vehiculo(datos: dict) -> tuple[dict, list[str]]:
    errores = []
    matricula = normalizar_matricula(datos.get("matricula"))
    marca = _texto(datos.get("marca"))
    modelo = _texto(datos.get("modelo"))
    anio_max = dt.date.today().year + 1

    if not RE_MATRICULA.match(matricula):
        errores.append(
            "Matrícula: formato 0000 BBB (4 números y 3 consonantes, sin vocales, Ñ ni Q)."
        )
    if len(marca) < 2 or len(marca) > 40:
        errores.append("Marca: entre 2 y 40 caracteres.")
    if len(modelo) < 1 or len(modelo) > 60:
        errores.append("Modelo: entre 1 y 60 caracteres.")
    try:
        anio = int(datos.get("anio"))
        if not ANIO_MIN <= anio <= anio_max:
            errores.append(f"Año: debe estar entre {ANIO_MIN} y {anio_max}.")
    except (TypeError, ValueError):
        anio = None
        errores.append("Año: debe ser un número entero.")
    return {"matricula": matricula, "marca": marca, "modelo": modelo, "anio": anio}, errores


def validar_orden(datos: dict) -> tuple[dict, list[str]]:
    errores = []
    hoy = dt.date.today()
    fecha_entrada = datos.get("fecha_entrada") or hoy
    fecha_entrega = datos.get("fecha_entrega") or None
    descripcion = _texto(datos.get("descripcion"))
    estado = datos.get("estado") or "abierta"

    try:
        kilometros = int(datos.get("kilometros"))
        if kilometros < 0:
            errores.append("Kilómetros: no pueden ser negativos.")
        elif kilometros > KM_MAX:
            errores.append(f"Kilómetros: como máximo {KM_MAX:,}".replace(",", ".") + ".")
    except (TypeError, ValueError):
        kilometros = None
        errores.append("Kilómetros: debe ser un número entero.")

    if not isinstance(fecha_entrada, dt.date):
        errores.append("Fecha de entrada: no válida.")
    elif fecha_entrada > hoy:
        errores.append("Fecha de entrada: no puede ser futura (es el día en que el coche entra al taller).")
    if fecha_entrega is not None:
        if not isinstance(fecha_entrega, dt.date):
            errores.append("Fecha de entrega: no válida.")
        elif isinstance(fecha_entrada, dt.date) and fecha_entrega < fecha_entrada:
            errores.append("Fecha de entrega: no puede ser anterior a la fecha de entrada.")

    if len(descripcion) < 5 or len(descripcion) > 500:
        errores.append("Descripción de la avería: entre 5 y 500 caracteres.")

    presupuesto = datos.get("presupuesto")
    if presupuesto in (None, ""):
        presupuesto = None
    else:
        try:
            presupuesto = Decimal(str(presupuesto)).quantize(Decimal("0.01"))
            if presupuesto < 0:
                errores.append("Presupuesto: no puede ser negativo.")
            elif presupuesto > PRESUPUESTO_MAX:
                errores.append("Presupuesto: importe demasiado alto.")
        except InvalidOperation:
            errores.append("Presupuesto: debe ser un número.")

    if estado not in ESTADOS:
        errores.append(f"Estado: uno de {', '.join(ESTADOS)}.")

    return {
        "fecha_entrada": fecha_entrada,
        "fecha_entrega": fecha_entrega,
        "kilometros": kilometros,
        "descripcion": descripcion,
        "presupuesto": presupuesto,
        "estado": estado,
    }, errores


# --------------------------------------------------------------------------
# Traducción de errores de PostgreSQL (segunda barrera, en la base de datos)
# --------------------------------------------------------------------------
_MENSAJES_RESTRICCION = {
    "clientes_nombre_check": "La base de datos rechazó el nombre del cliente.",
    "clientes_telefono_check": "La base de datos rechazó el teléfono.",
    "clientes_email_check": "La base de datos rechazó el email.",
    "vehiculos_matricula_formato": "La base de datos rechazó la matrícula: formato no válido.",
    "vehiculos_marca_check": "La base de datos rechazó la marca.",
    "vehiculos_modelo_check": "La base de datos rechazó el modelo.",
    "vehiculos_anio_check": "La base de datos rechazó el año del vehículo.",
    "ordenes_kilometros_check": "La base de datos rechazó los kilómetros (no pueden ser negativos).",
    "ordenes_descripcion_check": "La base de datos rechazó la descripción.",
    "ordenes_presupuesto_check": "La base de datos rechazó el presupuesto (no puede ser negativo).",
    "ordenes_estado_check": "La base de datos rechazó el estado de la orden.",
    "ordenes_fechas_coherentes": "La base de datos rechazó las fechas: la entrega es anterior a la entrada.",
}


def _traducir(e: psycopg.Error, matricula: str | None = None) -> Exception:
    nombre = getattr(e.diag, "constraint_name", None) or ""
    if isinstance(e, errors.UniqueViolation):
        if nombre == "vehiculos_matricula_unica" and matricula:
            return ErrorDuplicado(
                f"Ya existe un vehículo con la matrícula {formatear_matricula(matricula)}. "
                "No se ha guardado nada y el registro existente no se ha modificado."
            )
        return ErrorDuplicado("Registro duplicado. No se ha guardado nada.")
    if isinstance(e, errors.CheckViolation):
        return ErrorValidacion([_MENSAJES_RESTRICCION.get(nombre, f"La base de datos rechazó un valor ({nombre}).")])
    if isinstance(e, errors.ForeignKeyViolation):
        return ErrorNoEncontrado("El cliente o vehículo relacionado no existe.")
    if isinstance(e, errors.StringDataRightTruncation):
        return ErrorValidacion(["Algún texto es demasiado largo."])
    return e


# --------------------------------------------------------------------------
# Consultas (solo lectura)
# --------------------------------------------------------------------------
def resumen() -> dict:
    with conectar() as conn:
        return conn.execute(
            """
            SELECT (SELECT count(*) FROM clientes)                                   AS clientes,
                   (SELECT count(*) FROM vehiculos)                                  AS vehiculos,
                   (SELECT count(*) FROM ordenes_reparacion)                         AS ordenes,
                   (SELECT count(*) FROM ordenes_reparacion
                     WHERE estado IN ('abierta', 'en_reparacion'))                   AS en_taller
            """
        ).fetchone()


def listar_clientes() -> list[dict]:
    with conectar() as conn:
        return conn.execute(
            "SELECT id, nombre, telefono, email FROM clientes ORDER BY nombre"
        ).fetchall()


def listar_vehiculos() -> list[dict]:
    with conectar() as conn:
        return conn.execute(
            """
            SELECT v.matricula, v.marca, v.modelo, v.anio,
                   c.nombre AS cliente, c.telefono,
                   count(o.id)          AS ordenes,
                   max(o.fecha_entrada) AS ultima_visita,
                   max(o.kilometros)    AS ultimos_km
              FROM vehiculos v
              JOIN clientes c ON c.id = v.cliente_id
              LEFT JOIN ordenes_reparacion o ON o.vehiculo_id = v.id
             GROUP BY v.id, c.id
             ORDER BY ultima_visita DESC NULLS LAST, v.matricula
            """
        ).fetchall()


def buscar_vehiculo(matricula: str) -> dict | None:
    with conectar() as conn:
        return conn.execute(
            """
            SELECT v.id, v.matricula, v.marca, v.modelo, v.anio, v.creado_en,
                   c.id AS cliente_id, c.nombre AS cliente, c.telefono, c.email
              FROM vehiculos v JOIN clientes c ON c.id = v.cliente_id
             WHERE v.matricula = %s
            """,
            (normalizar_matricula(matricula),),
        ).fetchone()


def historial(matricula: str) -> list[dict]:
    with conectar() as conn:
        return conn.execute(
            """
            SELECT o.id, o.fecha_entrada, o.fecha_entrega, o.kilometros,
                   o.descripcion, o.presupuesto, o.estado
              FROM ordenes_reparacion o JOIN vehiculos v ON v.id = o.vehiculo_id
             WHERE v.matricula = %s
             ORDER BY o.fecha_entrada DESC, o.id DESC
            """,
            (normalizar_matricula(matricula),),
        ).fetchall()


def ultimas_ordenes(limite: int = 20) -> list[dict]:
    with conectar() as conn:
        return conn.execute(
            """
            SELECT o.id, v.matricula, v.marca, v.modelo, c.nombre AS cliente,
                   o.fecha_entrada, o.fecha_entrega, o.kilometros, o.estado, o.descripcion
              FROM ordenes_reparacion o
              JOIN vehiculos v ON v.id = o.vehiculo_id
              JOIN clientes c ON c.id = v.cliente_id
             ORDER BY o.creado_en DESC, o.id DESC
             LIMIT %s
            """,
            (limite,),
        ).fetchall()


def info_conexion() -> dict:
    """Con qué base, usuario y cifrado estamos conectados (sin secretos)."""
    cfg = cargar_config()
    with conectar(cfg) as conn:
        fila = conn.execute(
            """
            SELECT current_user AS usuario, current_database() AS base,
                   host(inet_server_addr()) AS ip_servidor, inet_server_port() AS puerto_servidor,
                   current_setting('server_version') AS version_postgres,
                   s.ssl, s.version AS tls_version, s.cipher AS tls_cifrado
              FROM pg_stat_ssl s
             WHERE s.pid = pg_backend_pid()
            """
        ).fetchone()
    return {"host": cfg.host, "sslmode": cfg.sslmode, **fila}


# --------------------------------------------------------------------------
# Escrituras (siempre en una transacción)
# --------------------------------------------------------------------------
def registrar_vehiculo(
    vehiculo: dict,
    cliente_id: int | None = None,
    cliente_nuevo: dict | None = None,
    orden: dict | None = None,
) -> dict:
    """Da de alta un vehículo (y, si se indica, su cliente nuevo y su primera
    orden) en UNA transacción. Si cualquier paso falla —matrícula duplicada,
    dato inválido…— se revierte todo: no queda un cliente suelto sin coche."""
    errores = []
    if (cliente_id is None) == (cliente_nuevo is None):
        errores.append("Indica un cliente existente o los datos de un cliente nuevo.")
    if cliente_nuevo is not None:
        cliente_nuevo, e = validar_cliente(cliente_nuevo)
        errores += e
    vehiculo, e = validar_vehiculo(vehiculo)
    errores += e
    if orden is not None:
        orden, e = validar_orden(orden)
        errores += e
    if errores:
        raise ErrorValidacion(errores)

    try:
        with conectar() as conn, conn.transaction():
            if cliente_nuevo is not None:
                cliente_id = conn.execute(
                    "INSERT INTO clientes (nombre, telefono, email) VALUES (%s, %s, %s) RETURNING id",
                    (cliente_nuevo["nombre"], cliente_nuevo["telefono"], cliente_nuevo["email"]),
                ).fetchone()["id"]
            elif conn.execute("SELECT 1 FROM clientes WHERE id = %s", (cliente_id,)).fetchone() is None:
                raise ErrorNoEncontrado("El cliente seleccionado no existe.")

            vehiculo_id = conn.execute(
                """INSERT INTO vehiculos (matricula, cliente_id, marca, modelo, anio)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (vehiculo["matricula"], cliente_id, vehiculo["marca"], vehiculo["modelo"], vehiculo["anio"]),
            ).fetchone()["id"]

            orden_id = None
            if orden is not None:
                orden_id = _insertar_orden(conn, vehiculo_id, orden)
    except psycopg.Error as e:
        raise _traducir(e, vehiculo["matricula"]) from e

    return {
        "cliente_id": cliente_id,
        "vehiculo_id": vehiculo_id,
        "orden_id": orden_id,
        "matricula": vehiculo["matricula"],
    }


def crear_orden(matricula: str, orden: dict) -> int:
    """Abre una orden de reparación para un vehículo existente."""
    orden, errores = validar_orden(orden)
    if errores:
        raise ErrorValidacion(errores)
    matricula = normalizar_matricula(matricula)

    try:
        with conectar() as conn, conn.transaction():
            veh = conn.execute("SELECT id FROM vehiculos WHERE matricula = %s", (matricula,)).fetchone()
            if veh is None:
                raise ErrorNoEncontrado(f"No existe ningún vehículo con matrícula {formatear_matricula(matricula)}.")

            # Coherencia entre tablas: el cuentakilómetros no puede ir hacia atrás
            # respecto a la última orden de ese mismo vehículo.
            previo = conn.execute(
                """SELECT kilometros, fecha_entrada FROM ordenes_reparacion
                    WHERE vehiculo_id = %s AND fecha_entrada <= %s
                    ORDER BY fecha_entrada DESC, id DESC LIMIT 1""",
                (veh["id"], orden["fecha_entrada"]),
            ).fetchone()
            if previo and orden["kilometros"] < previo["kilometros"]:
                raise ErrorValidacion([
                    f"Kilómetros: {orden['kilometros']} es menos que en la visita del "
                    f"{previo['fecha_entrada']:%d/%m/%Y} ({previo['kilometros']} km)."
                ])
            return _insertar_orden(conn, veh["id"], orden)
    except psycopg.Error as e:
        raise _traducir(e, matricula) from e


def _insertar_orden(conn: psycopg.Connection, vehiculo_id: int, o: dict) -> int:
    return conn.execute(
        """INSERT INTO ordenes_reparacion
               (vehiculo_id, fecha_entrada, fecha_entrega, kilometros, descripcion, presupuesto, estado)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (vehiculo_id, o["fecha_entrada"], o["fecha_entrega"], o["kilometros"],
         o["descripcion"], o["presupuesto"], o["estado"]),
    ).fetchone()["id"]