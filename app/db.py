"""
Persistencia en CSV para el carrito de compras.

- carrito.csv : items agregados al carrito por usuario.
- data.csv    : se actualiza directamente para descontar inventario por (Referencia, Talla, Ciudad).

Toda la persistencia se hace en CSV para mantener la consistencia con el resto del proyecto.
"""

import os
import csv
from datetime import datetime
from threading import Lock
import pandas as pd

CARRITO_CSV = "carrito.csv"
DATA_CSV = "data.csv"

CARRITO_COLUMNS = [
    "id",
    "usuario",
    "referencia",
    "talla",
    "ciudad",
    "nombre",
    "precio",
    "imagen",
    "cantidad",
    "fecha_agregado",
]

# Lock para serializar escrituras concurrentes sobre los CSV.
_lock = Lock()


def init_storage():
    """Crea el archivo carrito.csv si no existe."""
    if not os.path.exists(CARRITO_CSV):
        with open(CARRITO_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CARRITO_COLUMNS, delimiter=";")
            writer.writeheader()


def _leer_carrito_df():
    if not os.path.exists(CARRITO_CSV) or os.path.getsize(CARRITO_CSV) == 0:
        return pd.DataFrame(columns=CARRITO_COLUMNS)
    try:
        df = pd.read_csv(CARRITO_CSV, sep=";", encoding="utf-8", dtype=str).fillna("")
        for col in CARRITO_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[CARRITO_COLUMNS]
    except Exception:
        return pd.DataFrame(columns=CARRITO_COLUMNS)


def _escribir_carrito_df(df: pd.DataFrame):
    df = df[CARRITO_COLUMNS]
    df.to_csv(CARRITO_CSV, sep=";", encoding="utf-8", index=False)


def _siguiente_id(df: pd.DataFrame) -> int:
    if df.empty:
        return 1
    try:
        return int(pd.to_numeric(df["id"], errors="coerce").fillna(0).max()) + 1
    except Exception:
        return len(df) + 1


# ----------------------- INVENTARIO -----------------------

def _actualizar_inventario_csv(referencia: str, talla: str, ciudad: str, delta: int):
    """
    Modifica directamente data.csv sumando `delta` (puede ser negativo) al campo
    Inventario de la fila que coincida con (Referencia, Talla, Ciudad).
    Devuelve True si se aplicó el cambio.
    """
    if not os.path.exists(DATA_CSV):
        return False

    df = pd.read_csv(DATA_CSV, sep=";", encoding="latin-1")
    df.columns = [c.strip() for c in df.columns]

    if "Inventario" not in df.columns:
        return False

    df["Inventario"] = pd.to_numeric(df["Inventario"], errors="coerce").fillna(0).astype(int)

    mask = (
        (df["Referencia"].astype(str) == str(referencia)) &
        (df["Talla"].astype(str) == str(talla)) &
        (df["Ciudad"].astype(str) == str(ciudad))
    )

    if not mask.any():
        return False

    df.loc[mask, "Inventario"] = (df.loc[mask, "Inventario"] + int(delta)).clip(lower=0)
    df.to_csv(DATA_CSV, sep=";", encoding="latin-1", index=False)
    return True


def descontar_inventario(referencia: str, talla: str, ciudad: str, cantidad: int = 1):
    """Descuenta `cantidad` unidades del inventario en data.csv."""
    return _actualizar_inventario_csv(referencia, talla, ciudad, -int(cantidad))


def reponer_inventario(referencia: str, talla: str, ciudad: str, cantidad: int = 1):
    """Suma `cantidad` unidades de vuelta al inventario en data.csv."""
    return _actualizar_inventario_csv(referencia, talla, ciudad, int(cantidad))


# ----------------------- CARRITO -----------------------

def agregar_item_carrito(usuario, referencia, talla, ciudad, nombre, precio, imagen):
    """Agrega una unidad al carrito del usuario."""
    with _lock:
        df = _leer_carrito_df()
        nuevo_id = _siguiente_id(df)
        nuevo = {
            "id": str(nuevo_id),
            "usuario": str(usuario),
            "referencia": str(referencia),
            "talla": str(talla),
            "ciudad": str(ciudad),
            "nombre": str(nombre),
            "precio": str(precio),
            "imagen": str(imagen),
            "cantidad": "1",
            "fecha_agregado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        df = pd.concat([df, pd.DataFrame([nuevo])], ignore_index=True)
        _escribir_carrito_df(df)
        return nuevo_id


def obtener_carrito(usuario):
    """Devuelve la lista de items del carrito del usuario."""
    df = _leer_carrito_df()
    if df.empty:
        return []
    items = df[df["usuario"] == str(usuario)].to_dict(orient="records")
    items.sort(key=lambda r: r.get("fecha_agregado", ""), reverse=True)
    return items


def contar_items(usuario) -> int:
    df = _leer_carrito_df()
    if df.empty:
        return 0
    sub = df[df["usuario"] == str(usuario)]
    if sub.empty:
        return 0
    return int(pd.to_numeric(sub["cantidad"], errors="coerce").fillna(0).sum())


def obtener_item(item_id, usuario):
    df = _leer_carrito_df()
    if df.empty:
        return None
    sub = df[(df["id"] == str(item_id)) & (df["usuario"] == str(usuario))]
    if sub.empty:
        return None
    return sub.iloc[0].to_dict()


def eliminar_item(item_id, usuario):
    """Elimina un item del carrito. Devuelve el item eliminado o None."""
    with _lock:
        df = _leer_carrito_df()
        if df.empty:
            return None
        mask = (df["id"] == str(item_id)) & (df["usuario"] == str(usuario))
        if not mask.any():
            return None
        item = df[mask].iloc[0].to_dict()
        df = df[~mask]
        _escribir_carrito_df(df)
        return item


def vaciar_carrito(usuario):
    """Vacía el carrito del usuario. Devuelve los items eliminados."""
    with _lock:
        df = _leer_carrito_df()
        if df.empty:
            return []
        mask = df["usuario"] == str(usuario)
        items = df[mask].to_dict(orient="records")
        df = df[~mask]
        _escribir_carrito_df(df)
        return items
