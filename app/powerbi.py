"""
Endpoints de solo lectura para conectar Power BI Service a los datos de
Supabase. Power BI Service no puede conectarse directo al pooler de
Supabase (rechaza su certificado SSL), así que estos endpoints sirven los
mismos datos como JSON plano desde esta API (que sí tiene SSL válido).

Todos los endpoints (excepto /health) requieren el header `X-API-Key` con
el valor de la variable de entorno `POWERBI_API_KEY`.
"""

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from app import db

router = APIRouter(prefix="/api/powerbi", tags=["PowerBI"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_powerbi_api_key(api_key: Optional[str] = Security(_api_key_header)) -> None:
    """Dependencia reutilizable: exige X-API-Key == POWERBI_API_KEY."""
    expected = os.getenv("POWERBI_API_KEY")
    if not expected or not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="API Key inválida o ausente")


@router.get(
    "/health",
    summary="Estado del servicio",
    description="Healthcheck público (sin autenticación) para probar rápido que la API responde.",
)
async def powerbi_health():
    return {"status": "ok"}


@router.get(
    "/carrito",
    summary="Listado completo de carrito",
    description=(
        "Devuelve todos los items de carrito de todos los usuarios, en JSON plano. "
        "Soporta paginación opcional con `?offset=&limit=`; sin `limit` trae todo."
    ),
    dependencies=[Depends(verify_powerbi_api_key)],
)
async def powerbi_carrito(offset: int = 0, limit: Optional[int] = None):
    return db.fetch_table_rows("carrito", offset=offset, limit=limit)


@router.get(
    "/data",
    summary="Listado completo de catálogo/inventario",
    description=(
        "Devuelve todas las filas de la tabla `data` (catálogo + inventario por tienda), "
        "en JSON plano. Esta tabla es grande (~93k filas): usa `?offset=&limit=` para paginar "
        "si lo necesitas; sin `limit` trae todo."
    ),
    dependencies=[Depends(verify_powerbi_api_key)],
)
async def powerbi_data(offset: int = 0, limit: Optional[int] = None):
    return db.fetch_table_rows("data", offset=offset, limit=limit)


@router.get(
    "/directorio_empleados",
    summary="Listado completo de directorio de empleados",
    description=(
        "Devuelve todos los códigos de referido y su ciudad asociada, en JSON plano. "
        "Soporta paginación opcional con `?offset=&limit=`; sin `limit` trae todo."
    ),
    dependencies=[Depends(verify_powerbi_api_key)],
)
async def powerbi_directorio_empleados(offset: int = 0, limit: Optional[int] = None):
    return db.fetch_table_rows("directorio_empleados", offset=offset, limit=limit)


@router.get(
    "/excluidos",
    summary="Listado completo de artículos excluidos de promociones",
    description=(
        "Devuelve todas las referencias excluidas de promociones, en JSON plano. "
        "Soporta paginación opcional con `?offset=&limit=`; sin `limit` trae todo."
    ),
    dependencies=[Depends(verify_powerbi_api_key)],
)
async def powerbi_excluidos(offset: int = 0, limit: Optional[int] = None):
    return db.fetch_table_rows("excluidos", offset=offset, limit=limit)


@router.get(
    "/promociones",
    summary="Listado completo de promociones",
    description=(
        "Devuelve todas las promociones registradas, en JSON plano. "
        "Soporta paginación opcional con `?offset=&limit=`; sin `limit` trae todo."
    ),
    dependencies=[Depends(verify_powerbi_api_key)],
)
async def powerbi_promociones(offset: int = 0, limit: Optional[int] = None):
    return db.fetch_table_rows("promociones", offset=offset, limit=limit)


@router.get(
    "/reservas",
    summary="Listado completo de reservas",
    description=(
        "Devuelve todas las reservas registradas (una fila por item reservado), en JSON plano. "
        "Soporta paginación opcional con `?offset=&limit=`; sin `limit` trae todo."
    ),
    dependencies=[Depends(verify_powerbi_api_key)],
)
async def powerbi_reservas(offset: int = 0, limit: Optional[int] = None):
    return db.fetch_table_rows("reservas", offset=offset, limit=limit)


@router.get(
    "/usuarios",
    summary="Listado completo de usuarios",
    description=(
        "Devuelve todos los usuarios registrados, en JSON plano, EXCLUYENDO la columna de "
        "contraseña (`contrasenia`) por seguridad. "
        "Soporta paginación opcional con `?offset=&limit=`; sin `limit` trae todo."
    ),
    dependencies=[Depends(verify_powerbi_api_key)],
)
async def powerbi_usuarios(offset: int = 0, limit: Optional[int] = None):
    rows = db.fetch_table_rows("usuarios", offset=offset, limit=limit)
    for row in rows:
        row.pop("contrasenia", None)
    return rows
