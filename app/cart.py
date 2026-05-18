from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import pandas as pd

from app import db
from app import home

router = APIRouter()


def _to_int(valor):
    """Convierte cualquier representación de precio/dcto a int (acepta '$89.950', '20%', '0.20', etc.)."""
    if valor is None:
        return 0
    s = str(valor).strip()
    if not s:
        return 0
    s = s.replace("$", "").replace("%", "").replace(".", "").replace(",", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except Exception:
        return 0


def _dcto_promocional(num_items: int) -> int:
    """Devuelve el % de descuento promocional según la cantidad total de items en el carrito."""
    if num_items >= 4:
        return 40
    if num_items == 3:
        return 30
    if num_items == 2:
        return 20
    return 0


def calcular_carrito(items):
    """
    Aplica la lógica de promociones por cantidad sobre los items del carrito.

    Reglas:
      - 2 items en total → 20% mínimo por item
      - 3 items en total → 30% mínimo por item
      - 4+ items en total → 40% mínimo por item
      - Si el item ya tiene un %DCTO original mayor, se respeta el más alto.

    Devuelve un dict con:
      items: lista enriquecida (precio_antes_int, dcto_original_int, dcto_aplicado, precio_final, ahorro)
      subtotal: suma de precios "antes" (precio sin ningún descuento)
      total: suma de precios finales (con descuentos aplicados)
      ahorro: subtotal - total
      dcto_promocional: % promocional vigente por cantidad
      cantidad: número total de items
    """
    cantidad = len(items)
    dcto_promo = _dcto_promocional(cantidad)

    subtotal = 0
    total = 0
    items_calc = []

    for it in items:
        precio_antes = _to_int(it.get("precio_antes") or it.get("precio"))
        precio_ahora = _to_int(it.get("precio"))
        dcto_original = _to_int(it.get("dcto_original"))

        # Si no hay precio "antes" (item viejo o sin dcto original), tomamos el precio "ahora" como base
        if precio_antes <= 0:
            precio_antes = precio_ahora

        # El descuento aplicado es el MAYOR entre el original y el promocional
        dcto_aplicado = max(dcto_original, dcto_promo)

        # Precio final: precio_antes * (1 - dcto/100), redondeado a entero
        if dcto_aplicado > 0:
            precio_final = int(round(precio_antes * (100 - dcto_aplicado) / 100))
        else:
            precio_final = precio_antes

        ahorro_item = precio_antes - precio_final
        subtotal += precio_antes
        total += precio_final

        enriched = dict(it)
        enriched.update({
            "precio_antes_int": precio_antes,
            "precio_ahora_int": precio_ahora,
            "dcto_original_int": dcto_original,
            "dcto_aplicado": dcto_aplicado,
            "dcto_es_promocional": dcto_aplicado == dcto_promo and dcto_promo > dcto_original,
            "precio_final": precio_final,
            "ahorro_item": ahorro_item,
        })
        items_calc.append(enriched)

    return {
        "items": items_calc,
        "cantidad": cantidad,
        "dcto_promocional": dcto_promo,
        "subtotal": subtotal,
        "total": total,
        "ahorro": subtotal - total,
    }


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


@router.post("/carrito/agregar")
async def agregar_al_carrito(
    request: Request,
    referencia: str = Form(...),
    talla: str = Form(...),
    cantidad: int = Form(1),
):
    usuario = request.session.get("user")
    ciudad = request.session.get("city")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)
    if not ciudad:
        return JSONResponse({"ok": False, "mensaje": "Tu usuario no tiene ciudad asignada."}, status_code=400)

    if cantidad < 1:
        return JSONResponse({"ok": False, "mensaje": "La cantidad debe ser al menos 1."}, status_code=400)

    df = request.app.state.df
    if df is None or df.empty:
        return JSONResponse({"ok": False, "mensaje": "Catálogo no disponible."}, status_code=500)

    producto_row = df[df["Referencia"].astype(str) == str(referencia)]
    if producto_row.empty:
        return JSONResponse({"ok": False, "mensaje": "Producto no encontrado."}, status_code=404)

    stock = _stock_disponible_ciudad(df, referencia, talla, ciudad)
    ya_en_carrito = db.contar_items_en_carrito(usuario, referencia, talla, ciudad)
    disponible = stock - ya_en_carrito
    if disponible <= 0:
        if ya_en_carrito > 0:
            return JSONResponse({
                "ok": False,
                "mensaje": f"Ya tienes {ya_en_carrito} unidad(es) de esa talla en tu carrito y no hay más stock disponible en {ciudad}."
            }, status_code=400)
        return JSONResponse({
            "ok": False,
            "mensaje": f"No hay stock disponible de esa talla en {ciudad}."
        }, status_code=400)
    if disponible < cantidad:
        return JSONResponse({
            "ok": False,
            "mensaje": (
                f"Solo puedes agregar {disponible} unidad(es) más de esa talla en {ciudad} "
                f"(stock: {stock}, ya en tu carrito: {ya_en_carrito})."
            ),
        }, status_code=400)

    nombre = producto_row.iloc[0].get("nombre", "")
    precio = producto_row.iloc[0].get("Precio Ahora", "")
    precio_antes = producto_row.iloc[0].get("precio Antes", "")
    dcto_original = producto_row.iloc[0].get("%DCTO", "")
    imagen = producto_row.iloc[0].get("Imagen", "")

    last_id = None
    for _ in range(int(cantidad)):
        last_id = db.agregar_item_carrito(
            usuario=usuario,
            referencia=referencia,
            talla=talla,
            ciudad=ciudad,
            nombre=nombre,
            precio=precio,
            imagen=imagen,
            precio_antes=precio_antes,
            dcto_original=dcto_original,
        )

    total_items = db.contar_items(usuario)
    stock_restante = max(disponible - int(cantidad), 0)

    if cantidad == 1:
        mensaje = f"'{nombre}' (Talla {talla}) agregado al carrito."
    else:
        mensaje = f"{cantidad} unidades de '{nombre}' (Talla {talla}) agregadas al carrito."

    return JSONResponse({
        "ok": True,
        "mensaje": mensaje,
        "item_id": last_id,
        "cantidad_agregada": int(cantidad),
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
    resumen = calcular_carrito(items)

    return templates.TemplateResponse(request, "carrito.html", {
        "items": resumen["items"],
        "cantidad": resumen["cantidad"],
        "subtotal": resumen["subtotal"],
        "total": resumen["total"],
        "ahorro": resumen["ahorro"],
        "dcto_promocional": resumen["dcto_promocional"],
    })


@router.post("/carrito/eliminar")
async def eliminar_del_carrito(request: Request, item_id: str = Form(...)):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)

    item = db.eliminar_item(item_id, usuario)
    if not item:
        return JSONResponse({"ok": False, "mensaje": "Item no encontrado."}, status_code=404)

    return JSONResponse({"ok": True, "mensaje": "Item eliminado.", "total_items": db.contar_items(usuario)})


@router.post("/carrito/vaciar")
async def vaciar_el_carrito(request: Request):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)

    db.vaciar_carrito(usuario)
    return JSONResponse({"ok": True, "mensaje": "Carrito vaciado.", "total_items": 0})


@router.post("/carrito/reservar")
async def reservar_carrito(
    request: Request,
    nombre: str = Form(...),
    apellido: str = Form(...),
    cedula: str = Form(...),
    correo: str = Form(...),
    celular: str = Form(...),
    direccion: str = Form(...),
    ciudad: str = Form(...),
    acepta_tratamiento: str = Form(""),
):
    """
    Confirma la reserva del carrito: valida los datos del cliente y el
    consentimiento de tratamiento de datos, valida stock contra data.csv,
    persiste la reserva en reservas.csv, descuenta el inventario y vacía
    el carrito del usuario.
    """
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)

    # Consentimiento de tratamiento de datos (obligatorio)
    if str(acepta_tratamiento).strip().lower() not in ("on", "true", "1", "yes", "si"):
        return JSONResponse({
            "ok": False,
            "mensaje": "Debes aceptar el tratamiento de datos para hacer la reserva.",
        }, status_code=400)

    # Validar campos básicos no vacíos
    campos = {
        "nombre": nombre, "apellido": apellido, "cedula": cedula,
        "correo": correo, "celular": celular, "direccion": direccion, "ciudad": ciudad,
    }
    faltantes = [k for k, v in campos.items() if not str(v).strip()]
    if faltantes:
        return JSONResponse({
            "ok": False,
            "mensaje": f"Faltan campos por completar: {', '.join(faltantes)}.",
        }, status_code=400)

    items = db.obtener_carrito(usuario)
    if not items:
        return JSONResponse({"ok": False, "mensaje": "Tu carrito está vacío."}, status_code=400)

    df = request.app.state.df
    if df is None or df.empty:
        return JSONResponse({"ok": False, "mensaje": "Catálogo no disponible."}, status_code=500)

    # Calcular precios finales (con descuentos promocionales aplicados)
    resumen = calcular_carrito(items)
    items_calc = resumen["items"]

    # Agrupar por (referencia, talla, ciudad) sumando cantidades para validar stock
    agrupado = {}
    nombres = {}
    for it in items_calc:
        key = (str(it["referencia"]), str(it["talla"]), str(it["ciudad"]))
        agrupado[key] = agrupado.get(key, 0) + int(it.get("cantidad", 1) or 1)
        nombres.setdefault(key, it.get("nombre", str(it["referencia"])))

    # Validar stock de TODOS antes de descontar nada (todo-o-nada)
    for (ref, talla, ciudad_item), cantidad in agrupado.items():
        stock = _stock_disponible_ciudad(df, ref, talla, ciudad_item)
        if stock < cantidad:
            return JSONResponse({
                "ok": False,
                "mensaje": (
                    f"No hay stock suficiente para '{nombres[(ref, talla, ciudad_item)]}' "
                    f"(Talla {talla}) en {ciudad_item}. Disponible: {stock}, solicitado: {cantidad}."
                ),
            }, status_code=400)

    # Persistir la reserva en reservas.csv ANTES de mutar inventario
    datos_cliente = {
        "nombre": nombre.strip(),
        "apellido": apellido.strip(),
        "cedula": cedula.strip(),
        "correo": correo.strip(),
        "celular": celular.strip(),
        "direccion": direccion.strip(),
        "ciudad_envio": ciudad.strip(),
    }
    items_reserva = [{
        "referencia": it["referencia"],
        "talla": it["talla"],
        "ciudad_item": it["ciudad"],
        "nombre_producto": it.get("nombre", ""),
        "precio_unitario": it.get("precio_final", 0),
        "cantidad": int(it.get("cantidad", 1) or 1),
    } for it in items_calc]
    reserva_id = db.guardar_reserva(usuario, datos_cliente, items_reserva)

    # Descontar inventario en disco y en memoria
    for (ref, talla, ciudad_item), cantidad in agrupado.items():
        db.descontar_inventario(ref, talla, ciudad_item, cantidad)

        mask_mem = (
            (df["Referencia"].astype(str) == ref) &
            (df["Talla"].astype(str) == talla) &
            (df["Ciudad"].astype(str) == ciudad_item)
        )
        restante = int(cantidad)
        for i in df.index[mask_mem]:
            if restante <= 0:
                break
            inv = int(df.at[i, "Inventario"])
            if inv <= 0:
                continue
            quitar = min(inv, restante)
            df.at[i, "Inventario"] = inv - quitar
            restante -= quitar

    db.vaciar_carrito(usuario)

    return JSONResponse({
        "ok": True,
        "mensaje": f"¡Reserva #{reserva_id} confirmada! Tu pedido ha sido reservado.",
        "reserva_id": reserva_id,
        "total_items": 0,
    })


@router.get("/api/carrito/contador")
async def api_contador(request: Request):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"total": 0})
    return JSONResponse({"total": db.contar_items(usuario)})
