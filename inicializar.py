"""
inicializar.py · Prepara la base de datos ANTES de arrancar la aplicación.

Se conecta con la credencial de ADMINISTRADOR (separada de la de la app) y:
  1. Crea el esquema `taller` con sus tablas y restricciones (si no existen).
  2. Crea o actualiza el usuario LIMITADO de la aplicación (DB_USER / DB_PASSWORD
     del .env) con permisos solo de SELECT e INSERT sobre esas tablas.
  3. Carga los datos de prueba si las tablas están vacías.

Es idempotente: se puede ejecutar varias veces sin duplicar nada.

Uso (dentro del contenedor, que ya tiene las dependencias y el certificado):
    Local: docker compose --profile local run --rm app python inicializar.py
    AWS:   docker compose run --rm app python inicializar.py

Pide por teclado el usuario y la contraseña del administrador (en AWS, el
usuario maestro de RDS). Así la credencial de administrador no se guarda en
ningún archivo ni la recibe el contenedor de la aplicación.

Opciones:
    --sin-datos     no cargar datos de prueba
    --reiniciar     BORRA el esquema y lo vuelve a crear (solo desarrollo)
    --admin-user X  usuario administrador (por defecto: DB_ADMIN_USER o 'postgres')
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import sys

import psycopg
from psycopg import sql

import db

# --------------------------------------------------------------------------
# Esquema: las restricciones son la SEGUNDA barrera (la primera es db.py)
# --------------------------------------------------------------------------
ESQUEMA_SQL = r"""
CREATE SCHEMA IF NOT EXISTS taller;

CREATE TABLE IF NOT EXISTS taller.clientes (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre      varchar(100) NOT NULL,
    telefono    varchar(9)   NOT NULL,
    email       varchar(120),
    creado_en   timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT clientes_nombre_check   CHECK (char_length(btrim(nombre)) >= 2),
    CONSTRAINT clientes_telefono_check CHECK (telefono ~ '^[6789][0-9]{8}$'),
    CONSTRAINT clientes_email_check    CHECK (email IS NULL OR email ~ '^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')
);

CREATE TABLE IF NOT EXISTS taller.vehiculos (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    matricula   varchar(7)   NOT NULL,
    cliente_id  integer      NOT NULL REFERENCES taller.clientes(id) ON DELETE RESTRICT,
    marca       varchar(40)  NOT NULL,
    modelo      varchar(60)  NOT NULL,
    anio        smallint     NOT NULL,
    creado_en   timestamptz  NOT NULL DEFAULT now(),
    -- Campo ÚNICO comprobable: la matrícula (guardada normalizada: '1234BCD')
    CONSTRAINT vehiculos_matricula_unica   UNIQUE (matricula),
    CONSTRAINT vehiculos_matricula_formato CHECK (matricula ~ '^[0-9]{4}[BCDFGHJKLMNPRSTVWXYZ]{3}$'),
    CONSTRAINT vehiculos_marca_check       CHECK (char_length(btrim(marca)) >= 2),
    CONSTRAINT vehiculos_modelo_check      CHECK (char_length(btrim(modelo)) >= 1),
    -- Límite amplio en la base; la app aplica el estricto (1950 … año actual + 1)
    CONSTRAINT vehiculos_anio_check        CHECK (anio BETWEEN 1950 AND 2100)
);

CREATE TABLE IF NOT EXISTS taller.ordenes_reparacion (
    id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehiculo_id     integer       NOT NULL REFERENCES taller.vehiculos(id) ON DELETE RESTRICT,
    fecha_entrada   date          NOT NULL DEFAULT current_date,
    fecha_entrega   date,
    kilometros      integer       NOT NULL,
    descripcion     varchar(500)  NOT NULL,
    presupuesto     numeric(10,2),
    estado          varchar(15)   NOT NULL DEFAULT 'abierta',
    creado_en       timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT ordenes_kilometros_check   CHECK (kilometros BETWEEN 0 AND 2000000),
    CONSTRAINT ordenes_descripcion_check  CHECK (char_length(btrim(descripcion)) >= 5),
    CONSTRAINT ordenes_presupuesto_check  CHECK (presupuesto IS NULL OR presupuesto >= 0),
    CONSTRAINT ordenes_estado_check       CHECK (estado IN ('abierta','en_reparacion','terminada','entregada')),
    CONSTRAINT ordenes_fechas_coherentes  CHECK (fecha_entrega IS NULL OR fecha_entrega >= fecha_entrada)
);

CREATE INDEX IF NOT EXISTS vehiculos_cliente_idx ON taller.vehiculos (cliente_id);
CREATE INDEX IF NOT EXISTS ordenes_vehiculo_idx  ON taller.ordenes_reparacion (vehiculo_id, fecha_entrada DESC);
"""

TABLAS = ("clientes", "vehiculos", "ordenes_reparacion")

# --------------------------------------------------------------------------
# Datos de prueba (ficticios)
# --------------------------------------------------------------------------
CLIENTES = [
    ("María López García",              "612345678", "maria.lopez@example.com"),
    ("Javier Martín Ruiz",              "655112233", None),
    ("Lucía Fernández Gil",             "699887766", "lucia.fg@example.com"),
    ("Antonio Sánchez Pérez",           "634221100", None),
    ("Elena Romero Díaz",               "677450012", "elena.romero@example.com"),
    ("Transportes Hermanos Vidal S.L.", "913456789", "flota@hvidal.example.com"),
]
# (matrícula, índice de cliente, marca, modelo, año)
VEHICULOS = [
    ("1234BCD", 0, "Seat",          "Ibiza",    2017),
    ("3141BBC", 0, "Peugeot",       "208",      2023),
    ("5678FGH", 1, "Renault",       "Clio",     2015),
    ("2468JKL", 2, "Toyota",        "Corolla",  2021),
    ("1357MNP", 3, "Ford",          "Focus",    2012),
    ("9021RST", 4, "Volkswagen",    "Golf",     2019),
    ("4455VWX", 5, "Mercedes-Benz", "Sprinter", 2020),
    ("7788XYZ", 5, "Iveco",         "Daily",    2018),
]
# (índice de vehículo, días atrás entrada, días de taller o None, km, descripción, presupuesto, estado)
ORDENES = [
    (0, 400, 2,    61200,  "Revisión de los 60.000 km: aceite, filtros y frenos",  245.00, "entregada"),
    (0, 35,  1,    74850,  "Cambio de pastillas delanteras",                          120.50, "entregada"),
    (1, 20,  1,    9800,   "Primera revisión oficial",                                 95.00, "entregada"),
    (2, 180, 3,    142300, "Sustitución del embrague",                                780.00, "entregada"),
    (2, 3,   None, 151020, "Ruido en la suspensión trasera",                         None,   "en_reparacion"),
    (3, 60,  1,    38900,  "Cambio de neumáticos (4 uds.)",                           410.00, "entregada"),
    (4, 2,   None, 213400, "Testigo de motor encendido, diagnosis",                   60.00, "abierta"),
    (5, 90,  2,    55600,  "Correa de distribución y bomba de agua",                  690.00, "entregada"),
    (6, 15,  4,    188000, "Reparación de inyectores",                               1250.00, "terminada"),
    (6, 1,   None, 189150, "Revisión ITV previa",                                      80.00, "abierta"),
    (7, 120, 1,    99500,  "Cambio de batería",                                       140.00, "entregada"),
]


def crear_esquema(conn: psycopg.Connection, reiniciar: bool) -> None:
    if reiniciar:
        conn.execute("DROP SCHEMA IF EXISTS taller CASCADE")
        print("  · Esquema 'taller' borrado (--reiniciar).")
    conn.execute(ESQUEMA_SQL)
    print("  · Esquema y tablas listos: taller." + ", taller.".join(TABLAS))


def crear_usuario_app(conn: psycopg.Connection, cfg_app: db.Config) -> None:
    """Usuario de la aplicación con el MÍNIMO de permisos:
    conectarse a esta base, usar el esquema taller, SELECT e INSERT.
    Sin UPDATE, DELETE, TRUNCATE ni CREATE: no puede sobrescribir ni borrar."""
    usuario = sql.Identifier(cfg_app.user)
    clave = sql.Literal(cfg_app.password)

    existe = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (cfg_app.user,)).fetchone()
    if existe:
        conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(usuario, clave))
        print(f"  · Usuario '{cfg_app.user}' ya existía: contraseña sincronizada con el .env.")
    else:
        conn.execute(
            sql.SQL("CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT CONNECTION LIMIT 20 PASSWORD {}").format(usuario, clave)
        )
        print(f"  · Usuario '{cfg_app.user}' creado.")

    base = sql.Identifier(cfg_app.dbname)
    tablas = sql.SQL(", ").join(sql.Identifier("taller", t) for t in TABLAS)
    ordenes = [
        # Nadie más que los usuarios autorizados entra en esta base
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(base),
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(base, usuario),
        sql.SQL("REVOKE ALL ON SCHEMA taller FROM PUBLIC"),
        sql.SQL("GRANT USAGE ON SCHEMA taller TO {}").format(usuario),
        # Por si se re-ejecuta tras un cambio: se parte de cero y se concede lo justo
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA taller FROM {}").format(usuario),
        sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(tablas, usuario),
        sql.SQL("ALTER ROLE {} SET search_path = taller").format(usuario),
    ]
    for orden in ordenes:
        conn.execute(orden)

    # El esquema public no debe permitir crear objetos (PostgreSQL ≥ 15 ya lo hace;
    # en versiones anteriores lo forzamos). Si no somos dueños de public, se ignora.
    try:
        with conn.transaction():
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    except psycopg.Error:
        pass

    rol = conn.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication FROM pg_roles WHERE rolname = %s",
        (cfg_app.user,),
    ).fetchone()
    if any(rol.values()):
        raise SystemExit(f"✘ El usuario {cfg_app.user} tiene atributos de administrador: {rol}. Revísalo.")
    print(f"  · Permisos: CONNECT, USAGE en taller, SELECT e INSERT en {', '.join(TABLAS)}.")


def cargar_datos(conn: psycopg.Connection) -> None:
    if conn.execute("SELECT count(*) AS n FROM taller.clientes").fetchone()["n"] > 0:
        print("  · Ya hay datos: no se cargan los de prueba.")
        return
    hoy = dt.date.today()
    ids_cli = [
        conn.execute(
            "INSERT INTO taller.clientes (nombre, telefono, email) VALUES (%s, %s, %s) RETURNING id", c
        ).fetchone()["id"]
        for c in CLIENTES
    ]
    ids_veh = [
        conn.execute(
            "INSERT INTO taller.vehiculos (matricula, cliente_id, marca, modelo, anio) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (m, ids_cli[ic], marca, modelo, anio),
        ).fetchone()["id"]
        for m, ic, marca, modelo, anio in VEHICULOS
    ]
    for iv, atras, dias, km, desc, pres, estado in ORDENES:
        entrada = hoy - dt.timedelta(days=atras)
        entrega = entrada + dt.timedelta(days=dias) if dias is not None else None
        conn.execute(
            "INSERT INTO taller.ordenes_reparacion "
            "(vehiculo_id, fecha_entrada, fecha_entrega, kilometros, descripcion, presupuesto, estado) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (ids_veh[iv], entrada, entrega, km, desc, pres, estado),
        )
    print(f"  · Datos de prueba: {len(CLIENTES)} clientes, {len(VEHICULOS)} vehículos, {len(ORDENES)} órdenes.")


def conectar_admin(cfg_app: db.Config, admin_user: str, admin_pw: str) -> psycopg.Connection:
    """Conecta como administrador a la base de la app. Si la base aún no existe
    (p. ej. RDS creado sin 'nombre de base inicial'), la crea desde 'postgres'."""
    cfg = db.cargar_config(usuario=admin_user, password=admin_pw)
    try:
        return db.conectar(cfg)
    except psycopg.OperationalError as e:
        if f'database "{cfg.dbname}" does not exist' not in str(e):
            raise
    print(f"  · La base '{cfg.dbname}' no existe: creándola…")
    cfg_pg = db.Config(**{**cfg.__dict__, "dbname": "postgres"})
    with psycopg.connect(**cfg_pg.parametros(), connect_timeout=5, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cfg.dbname)))
    return db.conectar(cfg)


def main() -> None:
    p = argparse.ArgumentParser(description="Prepara esquema, usuario de la app y datos de prueba.")
    p.add_argument("--sin-datos", action="store_true", help="no cargar datos de prueba")
    p.add_argument("--reiniciar", action="store_true", help="BORRAR el esquema y recrearlo (solo desarrollo)")
    p.add_argument("--admin-user", default=os.environ.get("DB_ADMIN_USER", "postgres"))
    args = p.parse_args()

    try:
        cfg_app = db.cargar_config()
    except db.ErrorConfiguracion as e:
        sys.exit(f"✘ {e}")

    if args.reiniciar:
        if cfg_app.host.endswith(".rds.amazonaws.com"):
            sys.exit("✘ --reiniciar está desactivado contra RDS para no borrar datos reales.")
        if input("Esto BORRA todos los datos del esquema taller. Escribe 'borrar' para seguir: ") != "borrar":
            sys.exit("Cancelado.")

    print(f"Base de datos: {cfg_app.host}:{cfg_app.port}/{cfg_app.dbname} (sslmode={cfg_app.sslmode})")
    admin_user = args.admin_user
    if sys.stdin.isatty():
        admin_user = input(f"Usuario ADMINISTRADOR [{admin_user}]: ").strip() or admin_user
    admin_pw = os.environ.get("DB_ADMIN_PASSWORD") or getpass.getpass(f"Contraseña de {admin_user}: ")

    if admin_user == cfg_app.user:
        sys.exit("✘ El administrador y el usuario de la aplicación deben ser distintos.")

    try:
        with conectar_admin(cfg_app, admin_user, admin_pw) as conn:
            print(f"Conectado como '{admin_user}'. Preparando la base en una sola transacción…")
            with conn.transaction():
                crear_esquema(conn, args.reiniciar)
                crear_usuario_app(conn, cfg_app)
                if not args.sin_datos:
                    cargar_datos(conn)
    except psycopg.OperationalError as e:
        sys.exit(f"✘ No se pudo conectar como administrador: {e}")
    except psycopg.Error as e:
        sys.exit(f"✘ Error preparando la base (no se ha aplicado nada): {e}")

    print("\n✔ Base preparada. Ya puedes arrancar la aplicación y ejecutar comprobar.py.")


if __name__ == "__main__":
    main()