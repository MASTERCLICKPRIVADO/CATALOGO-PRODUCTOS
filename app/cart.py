from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
import pandas as pd

from app import db
from app import home
from app import documentos

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
      - El % promocional (20/30/40) se aplica SOBRE `precio_ahora` (es decir,
        sobre el precio que ya viene con el descuento original de la prenda).
        Se ENCADENA con el descuento original (no se compara como antes).

          precio_final = precio_ahora * (1 - dcto_promo / 100)

      - 2 items → 20% adicional sobre precio_ahora
      - 3 items → 30% adicional sobre precio_ahora
      - 4+ items → 40% adicional sobre precio_ahora

      - El "descuento total" mostrado al usuario es la diferencia porcentual
        entre `precio_final` y `precio_antes` (es decir, todo el ahorro
        acumulado vs el precio original sin ningún descuento).

          dcto_total = round((precio_antes - precio_final) / precio_antes * 100)

    Devuelve un dict con:
      items: lista enriquecida (precio_antes_int, precio_ahora_int,
             dcto_original_int, dcto_aplicado [= dcto_total visible],
             precio_final, ahorro)
      subtotal: suma de precios "antes" (sin ningún descuento)
      total: suma de precios finales (con descuentos encadenados aplicados)
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
        # Leemos precio_antes "puro" (sin fallback inmediato) para poder
        # reconstruirlo desde precio_ahora + dcto_original si está vacío.
        precio_antes = _to_int(it.get("precio_antes"))
        precio_ahora = _to_int(it.get("precio"))
        dcto_original = _to_int(it.get("dcto_original"))

        # Flag de la tabla `data` (columna `aplica`, "si" o "no"). Si vale
        # "no", el artículo SIGUE contando para el tier de promo (2/3/4+
        # unidades), pero NO recibe el % adicional sobre su precio_ahora.
        # Default permisivo: cualquier cosa distinta de "no" → aplica.
        aplica_str = str(it.get("aplica") or "").strip().lower()
        no_aplica_promo = (aplica_str == "no")

        # Defensa / retro-compat para items que se guardaron antes del fix
        # del column-name en agregar_al_carrito (cuando precio_antes quedaba
        # vacío en la BD). Si tenemos el % de descuento original y el precio
        # actual, podemos recalcular el precio antes:
        #     precio_antes = precio_ahora / (1 - dcto_original/100)
        if precio_antes <= 0:
            if precio_ahora > 0 and 0 < dcto_original < 100:
                precio_antes = int(round(precio_ahora * 100 / (100 - dcto_original)))
            else:
                precio_antes = precio_ahora
        if precio_ahora <= 0:
            precio_ahora = precio_antes

        # Promo EFECTIVO para este ítem: si no aplica, lo neutralizamos a 0.
        # (El tier global sigue calculado sobre len(items), así que un ítem
        # "no aplica" sí inflama el tier para los demás ítems.)
        promo_item = 0 if no_aplica_promo else dcto_promo

        # El descuento promocional se aplica SOBRE precio_ahora
        # (encadenado, no sustituye al original).
        if promo_item > 0:
            precio_final = int(round(precio_ahora * (100 - promo_item) / 100))
        else:
            precio_final = precio_ahora

        # Descuento total visible = diferencia porcentual entre precio_final
        # y precio_antes (original sin ningún descuento). Para ítems que no
        # aplican al promo, esto refleja solo el descuento original de la
        # prenda (si lo tiene); para los que sí aplican, refleja la suma.
        if precio_antes > 0:
            dcto_total_visible = int(round(
                (precio_antes - precio_final) * 100 / precio_antes
            ))
        else:
            dcto_total_visible = 0

        ahorro_item = precio_antes - precio_final
        subtotal += precio_antes
        total += precio_final

        enriched = dict(it)
        enriched.update({
            "precio_antes_int": precio_antes,
            "precio_ahora_int": precio_ahora,
            "dcto_original_int": dcto_original,
            # `dcto_aplicado` ahora representa el descuento TOTAL acumulado
            # respecto al precio_antes (lo que el cliente debe ver).
            "dcto_aplicado": dcto_total_visible,
            # True solo cuando el promo está activo Y se aplicó a este ítem.
            "dcto_es_promocional": (dcto_promo > 0) and (not no_aplica_promo),
            # Para que el template y el JS sepan si este ítem está excluido
            # del descuento acumulativo (UX claro al cliente).
            "aplica_promo": (not no_aplica_promo),
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
    ciudad_sel: str = Form(""),
):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"ok": False, "mensaje": "Debes iniciar sesión."}, status_code=401)

    # El usuario master puede reservar desde cualquier ciudad: usa la ciudad
    # de la ubicación elegida en el detalle. El usuario normal SIEMPRE usa la
    # ciudad de su sesión (ignoramos `ciudad_sel` por seguridad).
    if home.es_master(request):
        ciudad = (ciudad_sel or "").strip() or request.session.get("city")
    else:
        ciudad = request.session.get("city")
    if not ciudad:
        return JSONResponse({"ok": False, "mensaje": "No se pudo determinar la ciudad del artículo."}, status_code=400)

    if cantidad < 1:
        return JSONResponse({"ok": False, "mensaje": "La cantidad debe ser al menos 1."}, status_code=400)

    df = request.app.state.df
    if df is None or df.empty:
        return JSONResponse({"ok": False, "mensaje": "Catálogo no disponible."}, status_code=500)

    producto_row = df[df["Referencia"].astype(str) == str(referencia)]
    if producto_row.empty:
        return JSONResponse({"ok": False, "mensaje": "Producto no encontrado."}, status_code=404)

    # Preferir la fila de la ciudad elegida para tomar nombre/precio/imagen
    # correctos (un master puede agregar desde una ciudad distinta a la suya).
    producto_row_ciudad = producto_row[producto_row["Ciudad"].astype(str) == str(ciudad)]
    if not producto_row_ciudad.empty:
        producto_row = producto_row_ciudad

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
    # OJO: el DataFrame mantiene la columna como 'precio_antes' (ver
    # _DATA_COLUMN_MAP en home.py). Anteriormente buscábamos "precio Antes",
    # que no existía y caía como "" → entonces calcular_carrito hacía
    # `precio_antes = precio_ahora` por fallback, y el descuento mostrado
    # quedaba solo con el % promocional en vez del acumulado.
    precio_antes = producto_row.iloc[0].get("precio_antes", "")
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
        "es_master": home.es_master(request),
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
    codigo_vendedor: str = Form(""),
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

    es_master = home.es_master(request)

    # Los usuarios master (tiendas) reservan a nombre de un cliente pero
    # deben identificar qué vendedor hizo la venta. Este código NO se valida
    # contra directorio_empleados (texto libre) y es obligatorio solo para
    # master; se guarda en reservas_tiendas.codigo_referido.
    codigo_vendedor = (codigo_vendedor or "").strip()
    if es_master and not codigo_vendedor:
        return JSONResponse({
            "ok": False,
            "mensaje": "Debes ingresar el código de vendedor para reservar.",
        }, status_code=400)

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
    # Consolidar artículos idénticos (misma referencia + talla + ciudad) en una
    # sola línea sumando la cantidad. El carrito guarda una fila por unidad, así
    # que sin esto la reserva quedaría con varias líneas de cantidad 1 para el
    # mismo artículo. (El precio_final es igual entre unidades idénticas.)
    items_agrupados = {}
    for it in items_calc:
        key = (str(it["referencia"]), str(it["talla"]), str(it["ciudad"]))
        if key not in items_agrupados:
            items_agrupados[key] = {
                "referencia": it["referencia"],
                "talla": it["talla"],
                "ciudad_item": it["ciudad"],
                "nombre_producto": it.get("nombre", ""),
                "precio_unitario": it.get("precio_final", 0),
                "cantidad": 0,
            }
        items_agrupados[key]["cantidad"] += int(it.get("cantidad", 1) or 1)
    items_reserva = list(items_agrupados.values())

    if es_master:
        # Para master, el código guardado es el del vendedor tipeado en este
        # formulario (no el de su sesión/perfil, que se ignora por diseño).
        codigo_referido = codigo_vendedor
    else:
        # Código de referido: lo tomamos de la sesión (lo seteamos en login/registro).
        # Fallback: leerlo desde la tabla `usuarios` por si la sesión es antigua y
        # no tiene el campo cacheado.
        codigo_referido = (request.session.get("referral_code") or "").strip()
        if not codigo_referido:
            u = db.obtener_usuario(usuario) or {}
            codigo_referido = str(u.get("codigo_referido") or "").strip()

    reserva_id = db.guardar_reserva(
        usuario, datos_cliente, items_reserva,
        codigo_referido=codigo_referido,
        es_master=es_master,
    )

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


@router.get("/carrito/reserva/{reserva_id}/pdf")
async def descargar_comprobante_reserva(request: Request, reserva_id: int):
    """
    Genera EN TIEMPO REAL el comprobante de la reserva en formato PDF y lo
    envía directo al navegador. El archivo nunca se guarda en disco ni en la
    base de datos: se construye en memoria (BytesIO) con la plantilla fija
    `comprobante_reserva.html` y los datos actuales de la reserva.

    Solo el dueño de la reserva (usuario en sesión) puede descargarla.
    """
    usuario = request.session.get("user")
    if not usuario:
        return RedirectResponse(url="/login")

    reserva = db.obtener_reserva(reserva_id, usuario=usuario)
    if not reserva:
        return JSONResponse(
            {"ok": False, "mensaje": "Reserva no encontrada."},
            status_code=404,
        )

    # La tabla `reservas` no guarda ni la imagen ni el precio ORIGINAL del
    # artículo (solo `precio_unitario`, que es el precio FINAL con descuento).
    # Ambos los resolvemos desde el catálogo en memoria (app.state.df) por
    # Referencia (preferiendo la fila de la ciudad del ítem) para poder mostrar
    # en el comprobante PDF el descuento de cada artículo.
    df = request.app.state.df
    if df is not None and not df.empty and "Referencia" in df.columns:
        for it in reserva["items"]:
            ref = str(it.get("referencia", ""))
            ciudad_item = str(it.get("ciudad_item", ""))
            fila = df[df["Referencia"].astype(str) == ref]
            if "Ciudad" in df.columns and ciudad_item:
                fila_ciudad = fila[fila["Ciudad"].astype(str) == ciudad_item]
                if not fila_ciudad.empty:
                    fila = fila_ciudad
            if fila.empty:
                it["imagen"] = ""
                it["precio_antes"] = 0
            else:
                it["imagen"] = str(fila.iloc[0].get("Imagen", "") or "")
                it["precio_antes"] = _to_int(fila.iloc[0].get("precio_antes", ""))

    try:
        pdf_buffer = documentos.generar_comprobante_reserva_pdf(reserva)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "mensaje": f"No se pudo generar el comprobante: {e}"},
            status_code=500,
        )

    filename = f"reserva_{reserva_id}.pdf"
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/carrito/contador")
async def api_contador(request: Request):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"total": 0})
    return JSONResponse({"total": db.contar_items(usuario)})
