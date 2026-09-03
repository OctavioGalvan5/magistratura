FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencias del sistema (Pillow ya trae libjpeg/zlib en su wheel, pero por si acaso)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Código de la app + scripts batch
COPY app/ ./app/
COPY generar_pdfs_jurisdiccion.py ./
COPY process_whatsapp.py ./
COPY normalizar_jurisdicciones.py ./
COPY reset_batch.py ./
COPY backup_db.py ./

EXPOSE 5057

# Gunicorn en modo producción — 2 workers, timeout amplio por si sube archivos grandes
CMD ["gunicorn", \
     "--chdir", "app", \
     "-w", "2", \
     "-b", "0.0.0.0:5057", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
