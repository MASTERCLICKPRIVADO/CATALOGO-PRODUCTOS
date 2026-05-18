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
RESERVAS_CSV = "reservas.csv"

CARRITO_COLUMNS = [
    "id",
    "usuario",
    "referencia",
    "talla",
    "ciudad",
    "nombre",
    "precio",          # Precio "ahora" del catálogo (con dcto original ya aplicado)
    "precio_antes",    # Precio antes del descuento original (precio Antes del catálogo)
    "dcto_original",   # %DCTO del catálogo al momento de añadirse
    "imagen",
    "cantidad",
    "fecha_agregado",
]

RESERVAS_COLUMNS = [
    "reserva_id",
    "fecha",
    "usuario",
    "nombre",
    "apellido",
    "cedula",
    "correo",
    "celular",
    "direccion",
    "ciudad_envio",
    "referencia",
    "talla",
    "ciudad_item",
    "nombre_producto",
    "precio_unitario",
    "cantidad",
    "subtotal",
]

# Lock para serializar escrituras concurrentes sobre los CSV.
_lock = Lock()


def init_storage():
    """
    Crea los archivos carrito.csv y reservas.csv si no existen, o los migra
    agregando columnas faltantes sin perder los datos existentes.
    """
    _init_csv(CARRITO_CSV, CARRITO_COLUMNS)
    _init_csv(RESERVAS_CSV, RESERVAS_COLUMNS)


def _init_csv(path: str, columns: list):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, delimiter=";")
            writer.writeheader()
        return

    try:
        df = pd.read_csv(path, sep=";", encoding="utf-8", dtype=str).fillna("")
    except Exception:
        df = pd.DataFrame(columns=columns)

    cambios = False
    for col in columns:
        if col not in df.columns:
            df[col] = ""
            cambios = True
    if cambios:
        df = df[columns]
        df.to_csv(path, sep=";", encoding="utf-8", index=False)


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

def _leer_data_df():
    df = pd.read_csv(DATA_CSV, sep=";", encoding="latin-1", low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    return df


def descontar_inventario(referencia: str, talla: str, ciudad: str, cantidad: int = 1):
    """
    Descuenta `cantidad` unidades del inventario en data.csv, distribuyendo
    entre las tiendas que coincidan con (Referencia, Talla, Ciudad).
    Devuelve True solo si se pudo descontar la cantidad completa.
    """
    if not os.path.exists(DATA_CSV):
        return False

    df = _leer_data_df()
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

    restante = int(cantidad)
    for i in df.index[mask]:
        if restante <= 0:
            break
        inv = int(df.at[i, "Inventario"])
        if inv <= 0:
            continue
        quitar = min(inv, restante)
        df.at[i, "Inventario"] = inv - quitar
        restante -= quitar

    df.to_csv(DATA_CSV, sep=";", encoding="latin-1", index=False)
    return restante == 0


def reponer_inventario(referencia: str, talla: str, ciudad: str, cantidad: int = 1):
    """
    Suma `cantidad` unidades al inventario en data.csv (sobre la primera
    tienda que coincida con (Referencia, Talla, Ciudad)).
    """
    if not os.path.exists(DATA_CSV):
        return False

    df = _leer_data_df()
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

    first_idx = df.index[mask][0]
    df.at[first_idx, "Inventario"] = int(df.at[first_idx, "Inventario"]) + int(cantidad)
    df.to_csv(DATA_CSV, sep=";", encoding="latin-1", index=False)
    return True


# ----------------------- CARRITO -----------------------

def agregar_item_carrito(usuario, referencia, talla, ciudad, nombre, precio, imagen,
                          precio_antes="", dcto_original=""):
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
            "precio_antes": str(precio_antes) if precio_antes is not None else "",
            "dcto_original": str(dcto_original) if dcto_original is not None else "",
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


def contar_items_en_carrito(usuario, referencia, talla, ciudad) -> int:
    """Cantidad total que el usuario ya tiene en su carrito para (ref, talla, ciudad)."""
    df = _leer_carrito_df()
    if df.empty:
        return 0
    sub = df[
        (df["usuario"] == str(usuario)) &
        (df["referencia"] == str(referencia)) &
        (df["talla"] == str(talla)) &
        (df["ciudad"] == str(ciudad))
    ]
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


# ----------------------- RESERVAS -----------------------

def _leer_reservas_df():
    if not os.path.exists(RESERVAS_CSV) or os.path.getsize(RESERVAS_CSV) == 0:
        return pd.DataFrame(columns=RESERVAS_COLUMNS)
    try:
        df = pd.read_csv(RESERVAS_CSV, sep=";", encoding="utf-8", dtype=str).fillna("")
        for col in RESERVAS_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[RESERVAS_COLUMNS]
    except Exception:
        return pd.DataFrame(columns=RESERVAS_COLUMNS)


def _siguiente_reserva_id(df: pd.DataFrame) -> int:
    if df.empty:
        return 1
    try:
        return int(pd.to_numeric(df["reserva_id"], errors="coerce").fillna(0).max()) + 1
    except Exception:
        return len(df) + 1


def guardar_reserva(usuario, datos_cliente: dict, items: list) -> int:
    """
    Persiste una reserva en reservas.csv: una fila por item, todas comparten
    el mismo `reserva_id` y los datos del cliente.

    `datos_cliente` debe traer: nombre, apellido, cedula, correo, celular,
                                direccion, ciudad_envio.
    `items` debe traer dicts con: referencia, talla, ciudad_item,
                                  nombre_producto, precio_unitario, cantidad.
    """
    with _lock:
        df = _leer_reservas_df()
        reserva_id = _siguiente_reserva_id(df)
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        nuevas_filas = []
        for it in items:
            cantidad = int(it.get("cantidad", 1) or 1)
            precio_unit = int(it.get("precio_unitario", 0) or 0)
            nuevas_filas.append({
                "reserva_id": str(reserva_id),
                "fecha": fecha,
                "usuario": str(usuario),
                "nombre": str(datos_cliente.get("nombre", "")),
                "apellido": str(datos_cliente.get("apellido", "")),
                "cedula": str(datos_cliente.get("cedula", "")),
                "correo": str(datos_cliente.get("correo", "")),
                "celular": str(datos_cliente.get("celular", "")),
                "direccion": str(datos_cliente.get("direccion", "")),
                "ciudad_envio": str(datos_cliente.get("ciudad_envio", "")),
                "referencia": str(it.get("referencia", "")),
                "talla": str(it.get("talla", "")),
                "ciudad_item": str(it.get("ciudad_item", "")),
                "nombre_producto": str(it.get("nombre_producto", "")),
                "precio_unitario": str(precio_unit),
                "cantidad": str(cantidad),
                "subtotal": str(precio_unit * cantidad),
            })

        df = pd.concat([df, pd.DataFrame(nuevas_filas)], ignore_index=True)
        df = df[RESERVAS_COLUMNS]
        df.to_csv(RESERVAS_CSV, sep=";", encoding="utf-8", index=False)
        return reserva_id
