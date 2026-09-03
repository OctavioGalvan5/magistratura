import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from minio import Minio

load_dotenv()

DB = os.environ["DB_CONNECTION_STRING"]
MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET = os.environ["MINIO_SECRET_KEY"]
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").lower() == "true"

print("=== POSTGRES ===")
engine = create_engine(DB)
with engine.connect() as c:
    v = c.execute(text("select version()")).scalar()
    print("Version:", v)
    schemas = c.execute(text(
        "select schema_name from information_schema.schemata "
        "where schema_name not in ('pg_catalog','information_schema','pg_toast')"
    )).scalars().all()
    print("Schemas:", schemas)
    tables = c.execute(text(
        "select table_schema, table_name from information_schema.tables "
        "where table_schema not in ('pg_catalog','information_schema') "
        "order by table_schema, table_name"
    )).all()
    print("Tables existentes:")
    for t in tables:
        print(" -", t[0] + "." + t[1])

print()
print("=== MINIO ===")
client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS, secret_key=MINIO_SECRET, secure=MINIO_SECURE)
buckets = client.list_buckets()
print("Buckets existentes:")
for b in buckets:
    print(f" - {b.name}  (creado {b.creation_date})")
