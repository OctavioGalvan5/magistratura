# Avales — Consejo de la Magistratura 2026

Sistema para gestionar avales de abogados: carga los datos del padrón (Excel),
recibe fotos y PDFs de DNIs / planillas por WhatsApp, extrae los datos con
visión multimodal (Claude Opus 4.7 + fallback OpenAI), vincula automáticamente
cada foto a la persona correspondiente por DNI, y genera los PDFs finales por
jurisdicción listos para presentar.

## Stack

- **Backend**: Flask + SQLAlchemy + PostgreSQL
- **Storage**: MinIO / S3
- **Visión**: Anthropic Claude Opus 4.7 (fallback OpenAI GPT-4o-mini)
- **PDF**: fpdf2 + Pillow + pypdfium2

## Deploy con Docker

```bash
git clone https://github.com/OctavioGalvan5/magistratura.git
cd magistratura
cp .env.example .env
# editar .env con las credenciales reales de Postgres, MinIO y las API keys
docker compose up -d --build
```

La app queda expuesta en el puerto **5057**. Apuntá tu reverse proxy
(nginx / caddy / traefik) al puerto `5057` del host.

Health check: `GET /personas` responde 200 cuando la app está lista.

## Rutas principales

- `/personas` — listado con filtros y export a Excel.
- `/persona/<id>` — ficha editable de una persona con sus fotos vinculadas.
- `/fotos` — grilla de fotos con filtros por estado y tipo.
- `/fotos/revisar` — fotos sin match (para curaduría manual).
- `/fotos/upload` — subida masiva con análisis automático.
- `/reportes` — 5 reportes de auditoría (aval completo, sin DNI, sin foto, etc).
- `/entregables` — listado de PDFs generados por jurisdicción con links a MinIO.
- `/duplicados` — pares con DNI repetido en el Excel original.

## Scripts batch (correr dentro del contenedor)

```bash
# Procesar carpeta de WhatsApp con visión (idempotente por SHA)
docker compose exec app python process_whatsapp.py

# Normalizar variantes de escritura de jurisdicciones
docker compose exec app python normalizar_jurisdicciones.py --yes

# Generar los PDFs por jurisdicción (4 variantes)
docker compose exec app python generar_pdfs_jurisdiccion.py

# Solo la variante "entrega" (formato del instructivo)
docker compose exec app python generar_pdfs_jurisdiccion.py --tipo entrega

# Backup completo (schema clonado en DB + CSVs locales)
docker compose exec app python backup_db.py
```

## Variables de entorno

Ver `.env.example`.

## Licencia

Uso interno.
