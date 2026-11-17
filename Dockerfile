# Taller mecánico · imagen de la aplicación (interfaz web + lógica en un único contenedor)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Dependencias primero: si solo cambia el código, Docker reutiliza esta capa
COPY requirements.txt .
RUN pip install -r requirements.txt

# Solo el código. El .env y los certificados NO entran en la imagen:
# la configuración llega por variables de entorno (Compose) y ./certs se monta como volumen.
COPY app.py db.py inicializar.py comprobar.py ./

# Usuario sin privilegios: el proceso no se ejecuta como root
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin taller \
    && mkdir -p /app/certs
USER taller

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).status == 200 else 1)"

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]