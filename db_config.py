"""
Selector de database para los scripts.
Uso:
    from db_config import resolve_db_url, DB_CHOICES
    engine = create_engine(resolve_db_url("nacion"))  # o "julia"/"default"

Convencion:
    --db julia  (default)  → DB_CONNECTION_STRING (env)
    --db nacion            → DB_NACION_CONNECTION_STRING (env)
"""
import os

DB_CHOICES = ("julia", "nacion")

def resolve_db_url(choice: str | None = None) -> str:
    choice = (choice or "julia").lower().strip()
    if choice in ("julia", "default", ""):
        v = os.environ.get("DB_CONNECTION_STRING")
    elif choice == "nacion":
        v = os.environ.get("DB_NACION_CONNECTION_STRING")
    else:
        raise ValueError(f"--db invalido: {choice!r}. Valores validos: {DB_CHOICES}")
    if not v:
        raise RuntimeError(
            f"Falta la variable de entorno para --db={choice}. "
            f"Definila en .env (DB_CONNECTION_STRING o DB_NACION_CONNECTION_STRING)."
        )
    return v
