"""
app.py · Interfaz web del Taller mecánico (Streamlit).

Recorrido formulario → base de datos:
  formulario (Streamlit) → db.py valida → transacción con SQL parametrizado
  → PostgreSQL (CHECK / UNIQUE / FK) → commit o rollback → mensaje al usuario.

Toda la lógica de datos está en db.py; aquí solo hay interfaz.
"""
from __future__ import annotations

import datetime as dt

import psycopg
import streamlit as st

import db

st.set_page_config(page_title="Taller · Órdenes de reparación", page_icon="🔧", layout="wide")

ETIQUETA_ESTADO = {
    "abierta": "🟡 Abierta",
    "en_reparacion": "🔧 En reparación",
    "terminada": "🟢 Terminada",
    "entregada": "✅ Entregada",
}
COLUMNAS_FECHA = {
    "fecha_entrada": st.column_config.DateColumn("Entrada", format="DD/MM/YYYY"),
    "fecha_entrega": st.column_config.DateColumn("Entrega", format="DD/MM/YYYY"),
    "ultima_visita": st.column_config.DateColumn("Última visita", format="DD/MM/YYYY"),
}


def mostrar_errores(e: Exception) -> None:
    if isinstance(e, db.ErrorValidacion):
        st.error("**No se ha guardado nada.** Revisa estos datos:\n\n" + "\n".join(f"- {x}" for x in e.errores))
    elif isinstance(e, db.ErrorDuplicado):
        st.error(f"**Matrícula duplicada.** {e}")
    elif isinstance(e, db.ErrorNoEncontrado):
        st.error(f"**No se ha guardado nada.** {e}")
    else:
        st.error(f"Error inesperado (no se ha guardado nada): {e}")


def para_tabla(filas: list[dict]) -> list[dict]:
    """Formatea matrícula y estado para mostrarlos."""
    salida = []
    for f in filas:
        f = dict(f)
        if "matricula" in f:
            f["matricula"] = db.formatear_matricula(f["matricula"])
        if "estado" in f:
            f["estado"] = ETIQUETA_ESTADO.get(f["estado"], f["estado"])
        salida.append(f)
    return salida


# --------------------------------------------------------------------------
# Barra lateral: a qué base estamos conectados (sin secretos)
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("🔌 Conexión")
    try:
        info = db.info_conexion()
    except db.ErrorConfiguracion as e:
        st.error(f"Configuración incompleta: {e}")
        st.stop()
    except psycopg.OperationalError as e:
        st.error("No se puede conectar con la base de datos.")
        st.caption(str(e).strip())
        st.info("Comprueba DB_HOST, el grupo de seguridad de RDS y el certificado TLS. "
                "Diagnóstico: `docker compose run --rm app python comprobar.py`")
        st.stop()

    st.markdown(
        f"**Host:** `{info['host']}`  \n"
        f"**Base:** `{info['base']}`  \n"
        f"**Usuario:** `{info['usuario']}`  \n"
        f"**PostgreSQL:** {info['version_postgres']}"
    )
    if info["ssl"]:
        st.success(f"🔒 TLS activo · {info['tls_version']}  \nsslmode = `{info['sslmode']}`")
    else:
        st.warning(f"🔓 Sin TLS (sslmode = `{info['sslmode']}`). Aceptable solo en desarrollo local.")
    st.caption(f"Hora de la página: {dt.datetime.now():%d/%m/%Y %H:%M:%S}")

st.title("🔧 Taller mecánico · Órdenes de reparación")

try:
    r = db.resumen()
except psycopg.errors.UndefinedTable:
    st.error("Las tablas no existen todavía. Ejecuta antes: "
             "`docker compose run --rm app python inicializar.py`")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Clientes", r["clientes"])
c2.metric("Vehículos", r["vehiculos"])
c3.metric("Órdenes totales", r["ordenes"])
c4.metric("Coches en el taller", r["en_taller"])

tab_consulta, tab_alta, tab_orden = st.tabs(["🔎 Consultar", "🚗 Alta de vehículo", "🛠️ Nueva orden"])

# --------------------------------------------------------------------------
# 1 · Consultar
# --------------------------------------------------------------------------
with tab_consulta:
    st.subheader("Historial de un vehículo")
    buscada = st.text_input("Matrícula", placeholder="1234 BCD", key="buscar_matricula")
    if buscada:
        v = db.buscar_vehiculo(buscada)
        if v is None:
            st.warning(f"No hay ningún vehículo con matrícula **{db.formatear_matricula(db.normalizar_matricula(buscada))}**.")
        else:
            st.markdown(
                f"### {db.formatear_matricula(v['matricula'])} · {v['marca']} {v['modelo']} ({v['anio']})\n"
                f"**Cliente:** {v['cliente']} · 📞 {v['telefono']}"
                + (f" · ✉️ {v['email']}" if v["email"] else "")
                + f"  \n**Registrado:** {v['creado_en']:%d/%m/%Y %H:%M}"
            )
            h = db.historial(v["matricula"])
            if h:
                st.dataframe(
                    para_tabla(h), hide_index=True,
                    column_config={**COLUMNAS_FECHA,
                                   "id": "Orden", "kilometros": st.column_config.NumberColumn("Km", format="%d"),
                                   "descripcion": "Descripción",
                                   "presupuesto": st.column_config.NumberColumn("Presupuesto", format="%.2f €"),
                                   "estado": "Estado"},
                )
            else:
                st.info("Este vehículo todavía no tiene órdenes.")

    st.divider()
    st.subheader("Vehículos registrados")
    st.dataframe(
        para_tabla(db.listar_vehiculos()), hide_index=True,
        column_config={**COLUMNAS_FECHA, "matricula": "Matrícula", "marca": "Marca", "modelo": "Modelo",
                       "anio": st.column_config.NumberColumn("Año", format="%d"), "cliente": "Cliente",
                       "telefono": "Teléfono", "ordenes": "Órdenes",
                       "ultimos_km": st.column_config.NumberColumn("Últimos km", format="%d")},
    )

    st.subheader("Últimas órdenes")
    st.dataframe(
        para_tabla(db.ultimas_ordenes(20)), hide_index=True,
        column_config={**COLUMNAS_FECHA, "id": "Orden", "matricula": "Matrícula", "marca": "Marca",
                       "modelo": "Modelo", "cliente": "Cliente",
                       "kilometros": st.column_config.NumberColumn("Km", format="%d"),
                       "estado": "Estado", "descripcion": "Descripción"},
    )

# --------------------------------------------------------------------------
# 2 · Alta de vehículo (cliente + vehículo + orden opcional en UNA transacción)
# --------------------------------------------------------------------------
with tab_alta:
    st.subheader("Alta de vehículo")
    st.caption("Se guarda todo junto o nada: si la matrícula ya existe o hay un dato inválido, "
               "no se crea ni el cliente ni la orden, y el vehículo existente no se modifica.")

    tipo_cliente = st.radio("Cliente", ["Cliente existente", "Cliente nuevo"], horizontal=True)
    clientes = db.listar_clientes()

    with st.form("form_alta", clear_on_submit=False):
        cliente_id, cliente_nuevo = None, None
        if tipo_cliente == "Cliente existente":
            if not clientes:
                st.info("No hay clientes: elige «Cliente nuevo».")
            cliente_id = st.selectbox(
                "Selecciona el cliente", [c["id"] for c in clientes],
                format_func=lambda i: next(f"{c['nombre']} · {c['telefono']}" for c in clientes if c["id"] == i),
            )
        else:
            a, b, c = st.columns(3)
            cliente_nuevo = {
                "nombre": a.text_input("Nombre y apellidos *"),
                "telefono": b.text_input("Teléfono *", placeholder="612345678"),
                "email": c.text_input("Email", placeholder="opcional"),
            }

        st.markdown("**Vehículo**")
        a, b, c, d = st.columns(4)
        vehiculo = {
            "matricula": a.text_input("Matrícula *", placeholder="1234 BCD"),
            "marca": b.text_input("Marca *"),
            "modelo": c.text_input("Modelo *"),
            "anio": d.number_input("Año *", value=dt.date.today().year, step=1),
        }

        st.markdown("**Orden de entrada** (opcional)")
        con_orden = st.checkbox("Abrir también una orden de reparación ahora", value=True)
        a, b, c, d = st.columns(4)
        orden = {
            "kilometros": a.number_input("Kilómetros *", value=0, step=1, key="alta_km"),
            "fecha_entrada": b.date_input("Fecha de entrada", value=dt.date.today(),
                                          format="DD/MM/YYYY", key="alta_entrada"),
            "fecha_entrega": c.date_input("Entrega prevista", value=None, format="DD/MM/YYYY", key="alta_entrega"),
            "presupuesto": d.number_input("Presupuesto (€)", value=None, step=10.0, format="%.2f", key="alta_pres"),
            "descripcion": st.text_area("Avería / trabajo a realizar", key="alta_desc"),
        }

        if st.form_submit_button("💾 Guardar", type="primary"):
            try:
                res = db.registrar_vehiculo(
                    vehiculo, cliente_id=cliente_id, cliente_nuevo=cliente_nuevo,
                    orden=orden if con_orden else None,
                )
                st.success(
                    f"✔ Guardado: vehículo **{db.formatear_matricula(res['matricula'])}** "
                    f"(id {res['vehiculo_id']}, cliente id {res['cliente_id']}"
                    + (f", orden nº {res['orden_id']}" if res["orden_id"] else "") + "). "
                    "Búscalo en la pestaña Consultar."
                )
            except (db.ErrorValidacion, db.ErrorDuplicado, db.ErrorNoEncontrado, psycopg.Error) as e:
                mostrar_errores(e)

# --------------------------------------------------------------------------
# 3 · Nueva orden para un vehículo existente
# --------------------------------------------------------------------------
with tab_orden:
    st.subheader("Nueva orden de reparación")
    vehiculos = db.listar_vehiculos()
    if not vehiculos:
        st.info("No hay vehículos. Da uno de alta primero.")
    else:
        por_matricula = {v["matricula"]: v for v in vehiculos}
        sel = st.selectbox(
            "Vehículo", list(por_matricula),
            format_func=lambda m: f"{db.formatear_matricula(m)} · {por_matricula[m]['marca']} "
                                  f"{por_matricula[m]['modelo']} · {por_matricula[m]['cliente']}",
        )
        ultimos = por_matricula[sel]["ultimos_km"]
        st.caption(f"Últimos km registrados: **{ultimos:,}**".replace(",", ".") if ultimos is not None
                   else "Sin órdenes previas.")

        with st.form("form_orden", clear_on_submit=False):
            a, b, c, d = st.columns(4)
            datos = {
                "kilometros": a.number_input("Kilómetros *", value=int(ultimos or 0), step=1),
                "fecha_entrada": b.date_input("Fecha de entrada", value=dt.date.today(), format="DD/MM/YYYY"),
                "fecha_entrega": c.date_input("Entrega prevista", value=None, format="DD/MM/YYYY"),
                "estado": d.selectbox("Estado", db.ESTADOS, format_func=ETIQUETA_ESTADO.get),
                "presupuesto": st.number_input("Presupuesto (€)", value=None, step=10.0, format="%.2f"),
                "descripcion": st.text_area("Avería / trabajo a realizar *"),
            }
            if st.form_submit_button("💾 Abrir orden", type="primary"):
                try:
                    oid = db.crear_orden(sel, datos)
                    st.success(f"✔ Orden nº **{oid}** creada para {db.formatear_matricula(sel)}.")
                except (db.ErrorValidacion, db.ErrorNoEncontrado, psycopg.Error) as e:
                    mostrar_errores(e)

st.caption("Usuario de base de datos con permisos mínimos (SELECT e INSERT). "
           "Los registros no se sobrescriben ni se borran desde la aplicación.")