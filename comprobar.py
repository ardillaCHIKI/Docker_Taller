"""
comprobar.py · Pruebas automáticas de la aplicación contra la base configurada.

Usa EXACTAMENTE la misma configuración y el mismo usuario que la aplicación
(el usuario limitado del .env). Sirve para rellenar la tabla de evidencias:
cada fila tiene fecha, acción, resultado esperado, observado y OK/FALLO.
No muestra contraseñas.

Uso (dentro del contenedor):
    Local: docker compose --profile local run --rm app python comprobar.py
    AWS:   docker compose run --rm app python comprobar.py
Opciones:
    --markdown        salida en tabla Markdown (para pegar en el informe)
    --sin-escritura   solo pruebas de lectura, permisos y TLS (no crea registros)

Las pruebas de escritura crean un cliente "PRUEBA comprobar.py <fecha>" con un
vehículo de matrícula aleatoria. El usuario de la app no puede borrar, así que
el registro queda como evidencia identificable (es lo que pide la práctica).
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import sys

import psycopg
from psycopg import errors

import db

RESULTADOS: list[dict] = []


def anotar(prueba: str, accion: str, esperado: str, observado: str, ok: bool) -> None:
    RESULTADOS.append({
        "fecha": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prueba": prueba, "accion": accion, "esperado": esperado,
        "observado": observado, "ok": ok,
    })
    marca = "OK   " if ok else "FALLO"
    print(f"[{marca}] {prueba} · {accion}\n         esperado: {esperado}\n         observado: {observado}\n")


def contar() -> dict:
    r = db.resumen()
    return {"clientes": r["clientes"], "vehiculos": r["vehiculos"], "ordenes": r["ordenes"]}


def matricula_libre() -> str:
    for _ in range(50):
        m = f"{random.randint(0, 9999):04d}" + "".join(random.choices(db.LETRAS_MATRICULA, k=3))
        if db.buscar_vehiculo(m) is None:
            return m
    raise RuntimeError("No se encontró una matrícula libre")


# --------------------------------------------------------------------------
def p_configuracion() -> db.Config | None:
    try:
        cfg = db.cargar_config()
    except db.ErrorConfiguracion as e:
        anotar("Configuración", "Leer variables DB_* del entorno", "Configuración completa", str(e), False)
        return None
    r = cfg.resumen()
    es_rds = cfg.host.endswith(".rds.amazonaws.com")
    entorno = "AWS (RDS)" if es_rds else "local / desarrollo"
    ok = cfg.sslmode == "verify-full" if es_rds else True
    anotar("Configuración", f"Leer .env · entorno {entorno}",
           "Variables completas" + (" y sslmode=verify-full contra RDS" if es_rds else ""),
           f"host={r['host']} puerto={r['puerto']} base={r['base']} usuario={r['usuario']} "
           f"sslmode={r['sslmode']} sslrootcert={r['sslrootcert']}", ok)
    return cfg


def p_conexion_tls(cfg: db.Config) -> bool:
    try:
        i = db.info_conexion()
    except psycopg.Error as e:
        anotar("Conexión y TLS", "Conectar con el usuario de la app", "Conexión establecida",
               f"Error: {e}".strip(), False)
        return False
    tls = f"TLS={'sí' if i['ssl'] else 'no'}"
    if i["ssl"]:
        tls += f" ({i['tls_version']}, {i['tls_cifrado']})"
    exige_tls = cfg.sslmode in ("require", "verify-ca", "verify-full")
    anotar("Conexión y TLS", "Conectar y consultar pg_stat_ssl de la propia sesión",
           "Conexión cifrada con verificación de identidad (verify-full)" if exige_tls
           else "Conexión correcta (base de desarrollo sin TLS)",
           f"usuario={i['usuario']} base={i['base']} servidor={i['ip_servidor']}:{i['puerto_servidor']} "
           f"PostgreSQL {i['version_postgres']} · {tls} · sslmode={i['sslmode']}",
           bool(i["ssl"]) if exige_tls else True)
    return True


def p_permisos() -> None:
    with db.conectar() as conn:
        rol = conn.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        filas = conn.execute(
            """
            SELECT t AS tabla,
                   has_table_privilege(current_user, 'taller.' || t, 'SELECT')   AS s,
                   has_table_privilege(current_user, 'taller.' || t, 'INSERT')   AS i,
                   has_table_privilege(current_user, 'taller.' || t, 'UPDATE')   AS u,
                   has_table_privilege(current_user, 'taller.' || t, 'DELETE')   AS d,
                   has_table_privilege(current_user, 'taller.' || t, 'TRUNCATE') AS tr
              FROM unnest(ARRAY['clientes','vehiculos','ordenes_reparacion']) AS t
            """
        ).fetchall()
    ok = not any(rol.values()) and all(f["s"] and f["i"] and not (f["u"] or f["d"] or f["tr"]) for f in filas)
    detalle = "; ".join(
        f"{f['tabla']}: " + ",".join(n for n, k in (("SELECT", "s"), ("INSERT", "i"), ("UPDATE", "u"),
                                                     ("DELETE", "d"), ("TRUNCATE", "tr")) if f[k])
        for f in filas
    )
    anotar("Permisos SQL", "has_table_privilege y atributos del usuario de la app",
           "Solo SELECT e INSERT; no superusuario, no CREATEDB, no CREATEROLE",
           f"{detalle} · superuser={rol['rolsuper']} createdb={rol['rolcreatedb']} "
           f"createrole={rol['rolcreaterole']}", ok)

    # Intentos que DEBEN fallar (dentro de una transacción que se revierte)
    intentos = [
        ("CREATE TABLE taller.intruso (x int)", "Crear una tabla"),
        ("UPDATE taller.vehiculos SET marca = marca WHERE false", "UPDATE sobre vehiculos"),
        ("DELETE FROM taller.clientes WHERE false", "DELETE sobre clientes"),
    ]
    for sentencia, accion in intentos:
        try:
            with db.conectar() as conn:
                conn.execute(sentencia)
                conn.rollback()
            anotar("Permisos SQL", accion, "Denegado (permission denied)", "¡Se permitió!", False)
        except errors.InsufficientPrivilege as e:
            anotar("Permisos SQL", accion, "Denegado (permission denied)",
                   e.diag.message_primary or "permission denied", True)


def p_lectura() -> None:
    try:
        r = db.resumen()
        v = db.listar_vehiculos()
        ok = r["vehiculos"] > 0
        anotar("Lectura", "Consultar resumen y listado de vehículos",
               "Se leen datos de la base (al menos 1 vehículo)",
               f"{r['clientes']} clientes, {r['vehiculos']} vehículos, {r['ordenes']} órdenes; "
               f"primer vehículo: {db.formatear_matricula(v[0]['matricula']) if v else '—'}", ok)
    except psycopg.Error as e:
        anotar("Lectura", "Consultar datos", "Se leen datos", f"Error: {e}", False)


def p_escritura_y_validaciones() -> None:
    marca_tiempo = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    matricula = matricula_libre()
    hoy = dt.date.today()
    cliente = {"nombre": f"PRUEBA comprobar.py {marca_tiempo}", "telefono": "600000000"}
    vehiculo = {"matricula": matricula, "marca": "Prueba", "modelo": "Comprobación", "anio": 2020}
    orden = {"kilometros": 12345, "fecha_entrada": hoy, "fecha_entrega": hoy + dt.timedelta(days=2),
             "descripcion": "Orden creada por comprobar.py", "presupuesto": "99.90"}

    # 1 · Escritura válida (cliente + vehículo + orden en UNA transacción)
    antes = contar()
    try:
        r = db.registrar_vehiculo(vehiculo, cliente_nuevo=cliente, orden=orden)
        despues = contar()
        ok = all(despues[k] == antes[k] + 1 for k in antes)
        anotar("Escritura", f"Alta de cliente + vehículo {db.formatear_matricula(matricula)} + orden",
               "Se crean 1 cliente, 1 vehículo y 1 orden",
               f"ids cliente={r['cliente_id']} vehículo={r['vehiculo_id']} orden={r['orden_id']} · "
               f"recuentos {antes} → {despues}", ok)
    except Exception as e:  # noqa: BLE001
        anotar("Escritura", "Alta válida", "Se guarda", f"Error: {e}", False)
        return

    # 2 · Persistencia: recuperar desde una conexión NUEVA (otra sesión)
    v = db.buscar_vehiculo(matricula)
    h = db.historial(matricula)
    ok = v is not None and v["cliente"] == cliente["nombre"] and len(h) == 1
    anotar("Persistencia", f"Buscar {db.formatear_matricula(matricula)} desde una sesión nueva",
           "Se recupera el vehículo, su cliente y 1 orden",
           f"cliente='{v['cliente'] if v else None}', órdenes={len(h)}", ok)

    # 3 · Dato inválido rechazado por la APP (km negativos)
    antes = contar()
    try:
        db.crear_orden(matricula, {**orden, "kilometros": -5})
        anotar("Validación", "Orden con kilómetros negativos (app)", "Rechazo", "¡Se guardó!", False)
    except db.ErrorValidacion as e:
        anotar("Validación", "Orden con kilómetros negativos (app)",
               "Rechazo y sin cambios en la base", f"{e.errores[0]} · recuentos sin cambio: {contar() == antes}",
               contar() == antes)

    # 4 · Fecha de entrega anterior a la de entrada
    antes = contar()
    try:
        db.crear_orden(matricula, {**orden, "kilometros": 20000, "fecha_entrega": hoy - dt.timedelta(days=1)})
        anotar("Validación", "Entrega anterior a la entrada", "Rechazo", "¡Se guardó!", False)
    except db.ErrorValidacion as e:
        anotar("Validación", "Entrega anterior a la entrada", "Rechazo y sin cambios",
               f"{e.errores[0]} · recuentos sin cambio: {contar() == antes}", contar() == antes)

    # 5 · Dato inválido rechazado por la BASE (saltándose la validación de la app)
    antes = contar()
    try:
        with db.conectar() as conn:
            conn.execute(
                "INSERT INTO vehiculos (matricula, cliente_id, marca, modelo, anio) VALUES (%s, %s, %s, %s, %s)",
                ("12AEIOU", r["cliente_id"], "X", "Y", 1800),
            )
        anotar("Validación", "INSERT directo con matrícula y año inválidos", "Rechazo por CHECK",
               "¡Se guardó!", False)
    except errors.CheckViolation as e:
        anotar("Validación", "INSERT directo con matrícula y año inválidos (sin pasar por la app)",
               "La base lo rechaza (CHECK) y no cambia nada",
               f"CheckViolation {e.diag.constraint_name} · recuentos sin cambio: {contar() == antes}",
               contar() == antes)

    # 6 · Duplicado: cliente nuevo + matrícula YA existente → no debe quedar el cliente suelto
    antes = contar()
    original = db.buscar_vehiculo(matricula)
    intento_cli = {"nombre": f"PRUEBA duplicado {marca_tiempo}", "telefono": "611111111"}
    intento_veh = {"matricula": db.formatear_matricula(matricula).lower(),  # misma matrícula escrita distinta
                   "marca": "Otra", "modelo": "Sobrescritura", "anio": 2010}
    try:
        db.registrar_vehiculo(intento_veh, cliente_nuevo=intento_cli)
        anotar("Unicidad", "Alta con matrícula repetida", "Rechazo", "¡Se guardó!", False)
    except db.ErrorDuplicado as e:
        despues = contar()
        tras = db.buscar_vehiculo(matricula)
        intacto = all(tras[k] == original[k] for k in ("id", "cliente_id", "marca", "modelo", "anio"))
        sin_parcial = despues == antes
        anotar("Unicidad",
               f"Alta de cliente nuevo + vehículo '{intento_veh['matricula']}' (ya existe)",
               "Rechazo; NO se crea el cliente (sin escritura parcial); el original no cambia",
               f"{e} · recuentos {antes} → {despues} · original intacto: {intacto} "
               f"(marca={tras['marca']}, modelo={tras['modelo']})",
               sin_parcial and intacto)

    # 7 · Coherencia entre tablas: el cuentakilómetros no retrocede
    try:
        db.crear_orden(matricula, {**orden, "kilometros": 100})
        anotar("Coherencia", "Orden con menos km que la anterior", "Rechazo", "¡Se guardó!", False)
    except db.ErrorValidacion as e:
        anotar("Coherencia", "Orden con menos km que la visita anterior del mismo vehículo",
               "Rechazo", e.errores[0], True)


# --------------------------------------------------------------------------
def imprimir_markdown() -> None:
    print("| Fecha | Prueba | Acción | Resultado esperado | Resultado observado | Estado |")
    print("|---|---|---|---|---|---|")
    for r in RESULTADOS:
        celdas = [r["fecha"], r["prueba"], r["accion"], r["esperado"], r["observado"],
                  "✅ OK" if r["ok"] else "❌ FALLO"]
        print("| " + " | ".join(str(c).replace("|", "\\|") for c in celdas) + " |")


def main() -> None:
    p = argparse.ArgumentParser(description="Comprobaciones de la app Taller mecánico.")
    p.add_argument("--markdown", action="store_true", help="imprimir también una tabla Markdown")
    p.add_argument("--sin-escritura", action="store_true", help="no crear registros")
    args = p.parse_args()

    print("=== Comprobaciones Taller mecánico ===\n")
    cfg = p_configuracion()
    if cfg and p_conexion_tls(cfg):
        try:
            p_permisos()
            p_lectura()
            if not args.sin_escritura:
                p_escritura_y_validaciones()
        except psycopg.errors.UndefinedTable:
            anotar("Esquema", "Buscar tablas del esquema taller", "Tablas creadas",
                   "No existen: ejecuta antes inicializar.py", False)

    fallos = sum(not r["ok"] for r in RESULTADOS)
    print(f"=== {len(RESULTADOS) - fallos}/{len(RESULTADOS)} pruebas OK ===")
    if args.markdown:
        print()
        imprimir_markdown()
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()