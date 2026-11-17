#!/usr/bin/env python3
"""
configurar.py · Genera el archivo .env (configuración externa al código).

Se ejecuta en la MÁQUINA (tu portátil o la EC2), no dentro del contenedor.
Solo usa la biblioteca estándar de Python: no hace falta instalar nada.

    python3 configurar.py local      # desarrollo: PostgreSQL en un contenedor
    python3 configurar.py aws        # producción: RDS privado con TLS verify-full
    python3 configurar.py mostrar    # enseña el .env actual SIN contraseñas y lo valida

Cambiar de entorno = volver a ejecutar configurar.py. El código no se toca.

El .env queda con permisos 600 y está excluido de la imagen (.dockerignore)
y del repositorio (añadid ".env" y "certs/" a .gitignore). NO lo subáis,
NO lo pongáis en el informe ni en capturas.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import sys
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ENV = RAIZ / ".env"
CERTS = RAIZ / "certs"
CERT_RDS = "global-bundle.pem"
URL_CERT_RDS = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
RUTA_CERT_EN_CONTENEDOR = f"/app/certs/{CERT_RDS}"  # compose.yaml monta ./certs en /app/certs

CLAVES = [  # (variable, comentario)
    ("DB_HOST", "Host de PostgreSQL ('db' en local, endpoint de RDS en AWS)"),
    ("DB_PORT", "Puerto de PostgreSQL"),
    ("DB_NAME", "Nombre de la base de datos"),
    ("DB_USER", "Usuario LIMITADO de la aplicación (solo SELECT e INSERT)"),
    ("DB_PASSWORD", "Contraseña del usuario de la aplicación"),
    ("DB_SSLMODE", "TLS: disable en local, verify-full en AWS (cifra y verifica la identidad del servidor)"),
    ("DB_SSLROOTCERT", "Certificado de la CA de RDS, ruta DENTRO del contenedor"),
    ("APP_PORT", "Puerto donde se publica la web en la máquina"),
]
CLAVES_SOLO_LOCAL = [
    ("DB_ADMIN_USER", "Administrador de la base de DESARROLLO (solo local)"),
    ("DB_ADMIN_PASSWORD", "Contraseña del administrador de DESARROLLO (solo local)"),
]
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


def generar_password(n: int = 24) -> str:
    """Solo letras y números: no da problemas en .env ni en cadenas de conexión."""
    alfabeto = string.ascii_letters + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(n))


def preguntar(texto: str, defecto: str | None = None, obligatorio: bool = True) -> str:
    sufijo = f" [{defecto}]" if defecto else ""
    while True:
        valor = input(f"{texto}{sufijo}: ").strip()
        if not valor and defecto is not None:
            return defecto
        if valor or not obligatorio:
            return valor
        print("  (obligatorio)")


def leer_env(ruta: Path = ENV) -> dict[str, str]:
    valores: dict[str, str] = {}
    if not ruta.exists():
        return valores
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valores[clave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def escribir_env(valores: dict[str, str], entorno: str, forzar: bool) -> None:
    if ENV.exists() and not forzar:
        if not sys.stdin.isatty():
            sys.exit(f"Ya existe {ENV.name}. Usa --forzar para sobrescribirlo.")
        if input(f"Ya existe {ENV.name}. ¿Sobrescribir? [s/N]: ").strip().lower() not in ("s", "si", "sí"):
            sys.exit("Cancelado. No se ha cambiado nada.")

    lineas = [
        f"# Taller mecánico · configuración del entorno: {entorno.upper()}",
        "# Generado por configurar.py. CONTIENE SECRETOS: no subir al repositorio ni a capturas.",
        "",
    ]
    for clave, comentario in CLAVES + (CLAVES_SOLO_LOCAL if entorno == "local" else []):
        lineas += [f"# {comentario}", f"{clave}={valores.get(clave, '')}", ""]

    ENV.write_text("\n".join(lineas), encoding="utf-8")
    os.chmod(ENV, 0o600)
    print(f"\n✔ {ENV.name} creado (permisos 600).")


def descargar_certificado_rds() -> bool:
    CERTS.mkdir(exist_ok=True)
    destino = CERTS / CERT_RDS
    if destino.exists() and destino.stat().st_size > 0:
        print(f"✔ Certificado de RDS ya presente: certs/{CERT_RDS}")
        return True
    print(f"Descargando el certificado de la CA de RDS desde {URL_CERT_RDS} …")
    try:
        with urllib.request.urlopen(URL_CERT_RDS, timeout=20) as r:
            datos = r.read()
        if b"BEGIN CERTIFICATE" not in datos:
            raise ValueError("la respuesta no parece un certificado PEM")
        destino.write_bytes(datos)
        print(f"✔ Guardado en certs/{CERT_RDS}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"✘ No se pudo descargar ({e}). Hazlo a mano:\n"
              f"    mkdir -p certs && curl -fsSL -o certs/{CERT_RDS} {URL_CERT_RDS}")
        return False


# --------------------------------------------------------------------------
def cmd_local(args) -> None:
    admin_pw = generar_password()
    valores = {
        "DB_HOST": "db",               # nombre del servicio PostgreSQL en compose.yaml
        "DB_PORT": "5432",
        "DB_NAME": args.base,
        "DB_USER": args.usuario,
        "DB_PASSWORD": generar_password(),
        "DB_SSLMODE": "disable",       # la base de desarrollo no tiene TLS; RDS sí
        "DB_SSLROOTCERT": "",
        "APP_PORT": str(args.app_port),
        "DB_ADMIN_USER": "postgres",
        "DB_ADMIN_PASSWORD": admin_pw,
    }
    escribir_env(valores, "local", args.forzar)
    CERTS.mkdir(exist_ok=True)
    print(
        "\nSiguientes pasos (local):\n"
        "  docker compose --profile local build\n"
        "  docker compose --profile local run --rm app python inicializar.py\n"
        "      → pide la contraseña del administrador: es DB_ADMIN_PASSWORD del .env\n"
        "  docker compose --profile local up -d\n"
        "  docker compose --profile local run --rm app python comprobar.py\n"
        f"  Abrir http://localhost:{args.app_port}"
    )


def cmd_aws(args) -> None:
    print("Configuración para AWS (RDS PostgreSQL privado). Intro = valor por defecto.\n")
    host = args.host or preguntar("Endpoint de RDS (p. ej. taller-db.xxxx.eu-west-1.rds.amazonaws.com)")
    if not host.endswith(".rds.amazonaws.com"):
        print("  ⚠ No parece un endpoint de RDS; revisa que sea correcto.")
    valores = {
        "DB_HOST": host,
        "DB_PORT": args.puerto or preguntar("Puerto", "5432"),
        "DB_NAME": args.base or preguntar("Nombre de la base de datos", "taller"),
        "DB_USER": args.usuario or preguntar("Usuario de la aplicación (limitado)", "taller_app"),
        "DB_SSLMODE": "verify-full",
        "DB_SSLROOTCERT": RUTA_CERT_EN_CONTENEDOR,
        "APP_PORT": str(args.app_port or preguntar("Puerto web publicado en la EC2", "8501")),
    }
    if args.password_app:
        valores["DB_PASSWORD"] = getpass.getpass("Contraseña del usuario de la aplicación: ")
    else:
        valores["DB_PASSWORD"] = generar_password()
        print("  Contraseña del usuario de la aplicación generada automáticamente.\n"
              "  inicializar.py crea el usuario en RDS con esta misma contraseña.")

    escribir_env(valores, "aws", args.forzar)
    descargar_certificado_rds()
    print(
        "\nLa credencial de ADMINISTRADOR (usuario maestro de RDS) NO se guarda en .env:\n"
        "inicializar.py la pide por teclado y no queda en ningún archivo.\n\n"
        "Siguientes pasos (EC2):\n"
        "  docker compose build\n"
        "  docker compose run --rm app python inicializar.py\n"
        "  docker compose up -d\n"
        "  docker compose run --rm app python comprobar.py\n"
        f"  Abrir http://<IP-pública-EC2>:{valores['APP_PORT']}"
    )


def cmd_mostrar(_args) -> None:
    valores = leer_env()
    if not valores:
        sys.exit("No hay .env. Ejecuta 'python3 configurar.py local' o 'python3 configurar.py aws'.")

    print(f"Configuración en {ENV.name} (contraseñas ocultas):\n")
    for clave, _ in CLAVES + CLAVES_SOLO_LOCAL:
        if clave in valores:
            v = valores[clave]
            if "PASSWORD" in clave:
                v = "********" if v else "(vacía)"
            print(f"  {clave:<17} = {v}")

    problemas = []
    for clave in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE", "APP_PORT"):
        if not valores.get(clave):
            problemas.append(f"Falta {clave}.")
    sslmode = valores.get("DB_SSLMODE", "")
    if sslmode and sslmode not in SSLMODES:
        problemas.append(f"DB_SSLMODE='{sslmode}' no es válido.")
    if valores.get("DB_HOST", "").endswith(".rds.amazonaws.com") and sslmode != "verify-full":
        problemas.append("Contra RDS se exige DB_SSLMODE=verify-full.")
    if sslmode in ("verify-ca", "verify-full"):
        ruta = valores.get("DB_SSLROOTCERT", "")
        local = CERTS / Path(ruta).name if ruta.startswith("/app/certs/") else Path(ruta)
        if not ruta:
            problemas.append("Falta DB_SSLROOTCERT.")
        elif not local.exists():
            problemas.append(f"No existe {local} (se monta en el contenedor como {ruta}).")
    for clave in ("DB_PORT", "APP_PORT"):
        if valores.get(clave) and not valores[clave].isdigit():
            problemas.append(f"{clave} debe ser un número.")

    print()
    if problemas:
        print("✘ Problemas:\n  - " + "\n  - ".join(problemas))
        sys.exit(1)
    print("✔ Configuración completa.")


def main() -> None:
    p = argparse.ArgumentParser(description="Genera o revisa el .env del Taller mecánico.")
    sub = p.add_subparsers(dest="comando", required=True)

    pl = sub.add_parser("local", help="desarrollo con PostgreSQL en contenedor")
    pl.add_argument("--base", default="taller")
    pl.add_argument("--usuario", default="taller_app")
    pl.add_argument("--app-port", type=int, default=8501)
    pl.add_argument("--forzar", action="store_true", help="sobrescribir .env sin preguntar")
    pl.set_defaults(func=cmd_local)

    pa = sub.add_parser("aws", help="EC2 + RDS privado con TLS verify-full")
    pa.add_argument("--host", help="endpoint de RDS")
    pa.add_argument("--puerto")
    pa.add_argument("--base")
    pa.add_argument("--usuario")
    pa.add_argument("--app-port", type=int)
    pa.add_argument("--password-app", action="store_true",
                    help="escribir la contraseña del usuario de la app en vez de generarla")
    pa.add_argument("--forzar", action="store_true", help="sobrescribir .env sin preguntar")
    pa.set_defaults(func=cmd_aws)

    pm = sub.add_parser("mostrar", help="mostrar el .env sin contraseñas y validarlo")
    pm.set_defaults(func=cmd_mostrar)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()