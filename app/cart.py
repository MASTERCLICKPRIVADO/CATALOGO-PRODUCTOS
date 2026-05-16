from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import pandas as pd

from app import db
from app import home

router = APIRouter()


def _stock_disponible_ciudad(df, referencia, talla, ciudad) -> int:
    """Inventario disponible para (Referencia, Talla, Ciudad), sumando todas las tiendas."""
    if df is None or df.empty:
        return 0
    sub = df[
        (df["Referencia"].astype(str) == str(referencia)) &
        (df["Talla"].astype(str) == str(talla)) &
        (df["Ciudad"].astype(str) == str(ciudad))
    ]
    if sub.empty:
        return 0
    return int(pd.to_numeric(sub["Inventario"], errors="coerce").fillna(0).sum())


def _fila_para_descuento(df, referencia, talla, ciudad):
    """
    Devuelve (Ciudad real, Talla) de la primera tienda con stock > 0 dentro de la ciudad
    del usuario. Como una misma (referencia, talla, ciudad) puede tener varias tiendas,
    descontamos de la primera con stock disponible.
    """
    sub = df[
        (df["Referencia"].astype(str) == str(referencia)) &
        (df["Talla"].astype(str) == str(talla)) &
        (df["Ciudad"].astype(str) == str(ciudad))
    ]
    sub = sub[pd.to_numeric(sub["Inventario"], errors="coerce").fillna(0) > 0]
    if sub.empty:
        return None
    return sub.iloc[0].to_dict()


@router.post("/carrito/agregar")
async def agregar_al_carrito(
    request: Request,
    referencia: str = Form(...),
    talla: str = Form(...),
):
    usuario = request.session.get("user")
    ciudad = request.session.get("city")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)
    if not ciudad:
        return JSONResponse({"ok": False, "mensaje": "Tu usuario no tiene ciudad asignada."}, status_code=400)

    df = request.app.state.df
    if df is None or df.empty:
        return JSONResponse({"ok": False, "mensaje": "Catálogo no disponible."}, status_code=500)

    producto_row = df[df["Referencia"].astype(str) == str(referencia)]
    if producto_row.empty:
        return JSONResponse({"ok": False, "mensaje": "Producto no encontrado."}, status_code=404)

    stock = _stock_disponible_ciudad(df, referencia, talla, ciudad)
    if stock <= 0:
        return JSONResponse({
            "ok": False,
            "mensaje": f"No hay stock disponible de esa talla en {ciudad}."
        }, status_code=400)

    fila = _fila_para_descuento(df, referencia, talla, ciudad)
    if fila is None:
        return JSONResponse({
            "ok": False,
            "mensaje": f"No hay stock disponible de esa talla en {ciudad}."
        }, status_code=400)

    # Descontar en data.csv y en el DataFrame en memoria
    ok_persist = db.descontar_inventario(referencia, talla, ciudad, 1)
    if not ok_persist:
        return JSONResponse({"ok": False, "mensaje": "No se pudo actualizar el inventario."}, status_code=500)

    # Descontar la primera fila con stock en memoria (mismo criterio que en disco)
    mask_mem = (
        (df["Referencia"].astype(str) == str(referencia)) &
        (df["Talla"].astype(str) == str(talla)) &
        (df["Ciudad"].astype(str) == str(ciudad)) &
        (pd.to_numeric(df["Inventario"], errors="coerce").fillna(0) > 0)
    )
    idx = df.index[mask_mem]
    if len(idx) > 0:
        df.at[idx[0], "Inventario"] = int(df.at[idx[0], "Inventario"]) - 1

    nombre = producto_row.iloc[0].get("nombre", "")
    precio = producto_row.iloc[0].get("Precio Ahora", "")
    imagen = producto_row.iloc[0].get("Imagen", "")

    item_id = db.agregar_item_carrito(
        usuario=usuario,
        referencia=referencia,
        talla=talla,
        ciudad=ciudad,
        nombre=nombre,
        precio=precio,
        imagen=imagen,
    )

    total_items = db.contar_items(usuario)
    stock_restante = _stock_disponible_ciudad(df, referencia, talla, ciudad)

    return JSONResponse({
        "ok": True,
        "mensaje": f"'{nombre}' (Talla {talla}) agregado al carrito.",
        "item_id": item_id,
        "total_items": total_items,
        "stock_restante": stock_restante,
    })


@router.get("/carrito", response_class=HTMLResponse)
async def ver_carrito(request: Request):
    templates = request.app.state.templates
    usuario = request.session.get("user")
    if not usuario:
        return RedirectResponse(url="/login")

    items = db.obtener_carrito(usuario)

    total = 0
    for it in items:
        try:
            total += int(float(str(it.get("precio", "0")).replace("$", "").replace(".", "").replace(",", "").strip() or 0))
        except Exception:
            pass

    return templates.TemplateResponse(request, "carrito.html", {
        "items": items,
        "total": total,
        "cantidad": len(items),
    })


@router.post("/carrito/eliminar")
async def eliminar_del_carrito(request: Request, item_id: str = Form(...)):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)

    item = db.eliminar_item(item_id, usuario)
    if not item:
        return JSONResponse({"ok": False, "mensaje": "Item no encontrado."}, status_code=404)

    # Reponer inventario en data.csv y en memoria
    db.reponer_inventario(item["referencia"], item["talla"], item["ciudad"], int(item.get("cantidad", 1)))

    df = request.app.state.df
    if df is not None and not df.empty:
        mask_mem = (
            (df["Referencia"].astype(str) == str(item["referencia"])) &
            (df["Talla"].astype(str) == str(item["talla"])) &
            (df["Ciudad"].astype(str) == str(item["ciudad"]))
        )
        idx = df.index[mask_mem]
        if len(idx) > 0:
            df.at[idx[0], "Inventario"] = int(df.at[idx[0], "Inventario"]) + int(item.get("cantidad", 1))

    return JSONResponse({"ok": True, "mensaje": "Item eliminado.", "total_items": db.contar_items(usuario)})


@router.post("/carrito/vaciar")
async def vaciar_el_carrito(request: Request):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)

    items = db.vaciar_carrito(usuario)
    df = request.app.state.df
    for it in items:
        db.reponer_inventario(it["referencia"], it["talla"], it["ciudad"], int(it.get("cantidad", 1)))
        if df is not None and not df.empty:
            mask_mem = (
                (df["Referencia"].astype(str) == str(it["referencia"])) &
                (df["Talla"].astype(str) == str(it["talla"])) &
                (df["Ciudad"].astype(str) == str(it["ciudad"]))
            )
            idx = df.index[mask_mem]
            if len(idx) > 0:
                df.at[idx[0], "Inventario"] = int(df.at[idx[0], "Inventario"]) + int(it.get("cantidad", 1))

    return JSONResponse({"ok": True, "mensaje": "Carrito vaciado.", "total_items": 0})


@router.get("/api/carrito/contador")
async def api_contador(request: Request):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"total": 0})
    return JSONResponse({"total": db.contar_items(usuario)})
