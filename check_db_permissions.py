"""Chequea si el usuario actual puede crear databases nuevas."""
import os, sys
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)

with engine.connect() as c:
    who = c.execute(text(
        "SELECT current_user AS usuario, current_database() AS db"
    )).mappings().first()
    print(f"Usuario: {who['usuario']}")
    print(f"DB actual: {who['db']}")

    role = c.execute(text(
        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole "
        "FROM pg_roles WHERE rolname = current_user"
    )).mappings().first()
    if role:
        print(f"  superuser: {role['rolsuper']}")
        print(f"  puede CREATE DATABASE: {role['rolcreatedb']}")
        print(f"  puede CREATE ROLE: {role['rolcreaterole']}")

    print("\nDatabases existentes en el servidor:")
    for r in c.execute(text(
        "SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size "
        "FROM pg_database WHERE datistemplate = false ORDER BY datname"
    )).mappings():
        print(f"  - {r['datname']:30s} {r['size']}")
