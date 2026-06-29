import os
import time

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.encoders import jsonable_encoder
import pandas as pd

from app import db

router = APIRouter()


def get_promo_image_url() -> str:
    """
    Construye la URL pública del banner de promoción almacenado en Supabase
    Storage. Estructura: <SUPABASE_URL>/storage/v1/object/public/<bucket>/<file>.

    Se agrega un cache-buster `?t=<timestamp>` para que, al actualizar la
    imagen en Supabase, el navegador la traiga al instante sin tener que
    hacer hard-refresh. (La imagen de promoción es ligera, no nos preocupa
    el extra de no cachearla.)

    Si faltara SUPABASE_URL (entorno mal configurado), caemos a la imagen
    local en /static como fallback para no romper la home.
    """
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    bucket = (os.getenv("SUPABASE_STORAGE_BUCKET") or "static").strip("/")
    file_name = (os.getenv("SUPABASE_PROMO_FILE") or "promocion.jpeg").lstrip("/")
    if not base:
        return f"/static/img/{file_name}"
    return f"{base}/storage/v1/object/public/{bucket}/{file_name}?t={int(time.time())}"

# Mapeo de columnas snake_case de Postgres -> nombres "legacy" del CSV
# que el resto de la app (templates, filtros) ya conoce.
_DATA_COLUMN_MAP = {
    "tienda": "Tienda",
    "inventario": "Inventario",
    "ciudad": "Ciudad",
    "referencia": "Referencia",
    "talla": "Talla",
    "nombre": "nombre",
    "division": "Division",
    "precio_antes": "precio_antes",
    "genero": "Genero",
    "edad": "Edad",
    "deporte": "Deporte",
    "tipo_producto": "Tipo producto",
    "dcto": "%DCTO",
    "imagen": "Imagen",
    "precio_ahora": "Precio Ahora",
    "categoria": "Categoria",
    "subcategoria": "Subcategoria",
    "talla_cm": "TallaCM",
    "talla_co": "TallaCO",
    "talla_u.s_co": "TallaUSCO",
    # "si" / "no": indica si el artículo recibe el % de descuento
    # acumulativo del carrito. "no" → sigue contando para el tier
    # pero NO se le aplica el extra encima de su precio_ahora.
    "aplica": "aplica",
}


def load_data():
    """
    Lee la tabla `data` de Supabase y la devuelve como DataFrame con los
    nombres de columna "legacy" que ya usan los templates y filtros.
    Solo se llama UNA vez al iniciar el servidor (desde factory.py lifespan).
    """
    try:
        with db._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT tienda, inventario, ciudad, referencia, talla, nombre,
                              division, precio_antes, genero, edad, deporte,
                              tipo_producto, dcto, imagen, precio_ahora,
                              categoria, subcategoria, talla_cm, talla_co, "talla_u.s_co",
                              aplica
                         FROM data"""
                )
                rows = cur.fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df.rename(columns=_DATA_COLUMN_MAP)

        if 'Inventario' in df.columns:
            df['Inventario'] = pd.to_numeric(df['Inventario'], errors='coerce').fillna(0).astype(int)

        if 'Division' in df.columns:
            df['División'] = df['Division']
        if 'Genero' in df.columns:
            df['Género'] = df['Genero']

        df['Division'] = df['Division'].fillna('Sin Categoría').astype(str)
        df['Genero'] = df['Genero'].fillna('Unisex').astype(str)
        df['Deporte'] = df['Deporte'].fillna('General').astype(str)
        df['Edad'] = df['Edad'].fillna('Todas').astype(str)
        df['Talla'] = df['Talla'].fillna('N/A').astype(str)
        df['nombre'] = df['nombre'].fillna('Sin Nombre').astype(str)

        df = df.fillna("")
        return df
    except Exception as e:
        print(f"⚠️  Error cargando data desde Supabase: {e}")
        return pd.DataFrame()


def filtrar_por_ciudad(df, ciudad):
    """
    Devuelve solo las filas cuya Ciudad coincide con la del usuario.
    Si no hay ciudad o el df está vacío, devuelve el df original.
    """
    if df is None or df.empty or not ciudad or 'Ciudad' not in df.columns:
        return df
    return df[df['Ciudad'].astype(str) == str(ciudad)]


def es_master(request) -> bool:
    """
    True si el usuario en sesión tiene permiso 'master': ve TODO el inventario
    (todas las ciudades y tiendas), no solo el de su ciudad.
    """
    return str(request.session.get("permisos") or "").strip().lower() == "master"


def aplicar_scope_ciudad(request, df):
    """
    Aplica el filtro de ciudad SALVO que el usuario sea master.
    - Usuario normal → solo el inventario de la ciudad de su sesión.
    - Usuario master → el inventario completo, sin filtrar por ciudad.
    - Invitado (sin cuenta) → la ciudad que eligió en la tarjeta "Catálogo"
      del login (`guest_city`), si la eligió.
    """
    if es_master(request):
        return df
    ciudad = request.session.get("city") or request.session.get("guest_city") or ""
    return filtrar_por_ciudad(df, ciudad)


def aplicar_orden_dcto(df, orden):
    """
    Ordena el DataFrame por el % de descuento (`%DCTO`) cuando el usuario
    selecciona el filtro de orden. Valores aceptados:
      - "asc"  → de menor a mayor descuento
      - "desc" → de mayor a menor descuento
      - cualquier otro → no se altera el orden

    `%DCTO` puede venir como "20%", "20", "0.20", "" → normalizamos a número.
    Usamos `kind="mergesort"` (estable) para preservar el orden relativo
    entre productos con el mismo descuento.
    """
    if df is None or df.empty or orden not in ("asc", "desc"):
        return df
    if "%DCTO" not in df.columns:
        return df
    df = df.copy()
    df["_dcto_num"] = pd.to_numeric(
        df["%DCTO"].astype(str)
                   .str.replace("%", "", regex=False)
                   .str.replace(",", ".", regex=False)
                   .str.strip(),
        errors="coerce",
    ).fillna(0)
    df = df.sort_values("_dcto_num", ascending=(orden == "asc"), kind="mergesort")
    df = df.drop(columns=["_dcto_num"])
    return df


def get_filtros_completos(df, q=None, tipo_producto=None, categoria=None,
                          genero=None, deporte=None, edad=None, talla=None):
    def filtrar(df_in, skip=None):
        df_f = df_in
        if q:
            df_f = df_f[
                df_f['nombre'].str.contains(q, case=False) |
                df_f['Referencia'].astype(str).str.contains(q, case=False)
            ]
        if tipo_producto and skip != 'tipo_producto':
            df_f = df_f[df_f['Division'] == tipo_producto]
        if categoria and skip != 'categoria' and 'Categoria' in df_f.columns:
            df_f = df_f[df_f['Categoria'] == categoria]
        if genero and skip != 'genero':
            df_f = df_f[df_f['Genero'] == genero]
        if deporte and skip != 'deporte':
            df_f = df_f[df_f['Deporte'] == deporte]
        if edad and skip != 'edad':
            df_f = df_f[df_f['Edad'] == edad]
        if talla and skip != 'talla':
            df_f = df_f[df_f['TallaUSCO'] == talla]
        return df_f

    def _valores_unicos(df_in, columna):
        if columna not in df_in.columns:
            return []
        return sorted([
            str(x) for x in df_in[columna].unique()
            if str(x).strip() and str(x).strip().lower() != 'nan'
        ])

    return {
        "tipos_producto": _valores_unicos(filtrar(df, 'tipo_producto'), "Division"),
        "categorias":     _valores_unicos(filtrar(df, 'categoria'),     "Categoria"),
        "generos":        _valores_unicos(filtrar(df, 'genero'),        "Genero"),
        "deportes":       _valores_unicos(filtrar(df, 'deporte'),       "Deporte"),
        "edades":         _valores_unicos(filtrar(df, 'edad'),          "Edad"),
        "tallas":         _valores_unicos(filtrar(df, 'talla'),         "TallaUSCO"),
    }


@router.get("/catalogo")
async def seleccionar_ciudad_invitado(request: Request, ciudad: str = ""):
    """
    Acceso de invitado (sin cuenta): guarda en sesión la ciudad elegida en
    la tarjeta "Catálogo" del login y redirige al catálogo filtrado por
    esa ciudad. No requiere login (ver `public_paths` en app/factory.py).
    """
    ciudad = (ciudad or "").strip()
    if ciudad:
        request.session["guest_city"] = ciudad
    return RedirectResponse(url="/", status_code=303)


@router.get("/", response_class=HTMLResponse)
async def ver_catalogo(request: Request, page: int = 1, orden_dcto: str = ""):
    templates = request.app.state.templates
    df = request.app.state.df  # ✅ Desde memoria, no desde disco

    if df is None or df.empty:
        return templates.TemplateResponse(request, "home.html", {"productos": [], "mensaje": "No hay productos disponibles."})

    # Filtrar por ciudad del usuario logueado (el master ve todas las ciudades)
    df = aplicar_scope_ciudad(request, df)

    # Solo mostrar productos con stock disponible
    if df is not None and not df.empty and 'Inventario' in df.columns:
        df = df[df['Inventario'] > 0]

    df_unique = df.drop_duplicates(subset=['Referencia'])

    # Ordenar por % de descuento si el usuario lo solicitó.
    # Lo hacemos sobre el df ya deduplicado para no inflar las comparaciones.
    df_unique = aplicar_orden_dcto(df_unique, orden_dcto)

    limit = 12
    start = (page - 1) * limit
    end = start + limit
    productos = df_unique.iloc[start:end].to_dict(orient="records")
    has_more = len(df_unique) > end
    filtros = get_filtros_completos(df)

    show_promo_on_load = bool(request.session.pop("show_promo", False))

    return templates.TemplateResponse(request, "home.html", {
        "productos": productos,
        "filtros": filtros,
        "page": page,
        "has_more": has_more,
        "show_promo_on_load": show_promo_on_load,
        "sel_orden_dcto": orden_dcto,
        "promo_image_url": get_promo_image_url(),
    })


@router.get("/api/productos")
async def api_productos(
    request: Request,  # ✅ Agregado para acceder al caché
    page: int = 1,
    q: str = "",
    tipo_producto: str = "",
    categoria: str = "",
    genero: str = "",
    deporte: str = "",
    edad: str = "",
    talla: str = "",
    orden_dcto: str = "",
):
    df = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado (el master ve todas las ciudades)
    df = aplicar_scope_ciudad(request, df)

    # Solo mostrar productos con stock disponible
    if df is not None and not df.empty and 'Inventario' in df.columns:
        df = df[df['Inventario'] > 0]

    if q:
        df = df[
            df['nombre'].str.contains(q, case=False) |
            df['Referencia'].astype(str).str.contains(q, case=False)
        ]
    if tipo_producto:
        df = df[df['Division'] == tipo_producto]
    if categoria and 'Categoria' in df.columns:
        df = df[df['Categoria'] == categoria]
    if genero:
        df = df[df['Genero'] == genero]
    if deporte:
        df = df[df['Deporte'] == deporte]
    if edad:
        df = df[df['Edad'] == edad]
    if talla:
        df = df[df['TallaUSCO'] == talla]

    df_unique = df.drop_duplicates(subset=['Referencia'])
    df_unique = aplicar_orden_dcto(df_unique, orden_dcto)

    limit = 12
    start = (page - 1) * limit
    end = start + limit
    productos = df_unique.iloc[start:end].to_dict(orient="records")
    has_more = len(df_unique) > end

    return JSONResponse(jsonable_encoder({
        "productos": productos,
        "has_more": has_more
    }))


@router.get("/buscar", response_class=HTMLResponse)
async def buscar_productos(
    request: Request,
    q: str = "",
    tipo_producto: str = "",
    categoria: str = "",
    genero: str = "",
    deporte: str = "",
    edad: str = "",
    talla: str = "",
    orden_dcto: str = "",
    page: int = 1
):
    templates = request.app.state.templates
    df_all = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado (el master ve todas las ciudades)
    df_all = aplicar_scope_ciudad(request, df_all)

    # Solo mostrar productos con stock disponible
    if df_all is not None and not df_all.empty and 'Inventario' in df_all.columns:
        df_all = df_all[df_all['Inventario'] > 0]

    df = df_all.copy()
    mensaje = None

    if q:
        df_search = df[
            df['nombre'].str.contains(q, case=False) |
            df['Referencia'].astype(str).str.contains(q, case=False)
        ]
        if df_search.empty:
            mensaje = f"No se encontraron resultados para '{q}'. Recuerda que el buscador solo funciona por nombre o referencia."
            q_for_filters = ""
        else:
            df = df_search
            q_for_filters = q
    else:
        q_for_filters = ""

    if tipo_producto:
        df = df[df['Division'] == tipo_producto]
    if categoria and 'Categoria' in df.columns:
        df = df[df['Categoria'] == categoria]
    if genero:
        df = df[df['Genero'] == genero]
    if deporte:
        df = df[df['Deporte'] == deporte]
    if edad:
        df = df[df['Edad'] == edad]
    if talla:
        df = df[df['TallaUSCO'] == talla]

    df_unique = df.drop_duplicates(subset=['Referencia'])
    df_unique = aplicar_orden_dcto(df_unique, orden_dcto)

    limit = 12
    start = (page - 1) * limit
    end = start + limit
    productos = df_unique.iloc[start:end].to_dict(orient="records")
    has_more = len(df_unique) > end

    filtros = get_filtros_completos(df_all, q_for_filters, tipo_producto, categoria, genero, deporte, edad, talla)

    return templates.TemplateResponse(request, "home.html", {
        "productos": productos,
        "filtros": filtros,
        "query": q,
        "sel_tipo": tipo_producto,
        "sel_cat": categoria,
        "sel_gen": genero,
        "sel_dep": deporte,
        "sel_edad": edad,
        "sel_talla": talla,
        "sel_orden_dcto": orden_dcto,
        "page": page,
        "has_more": has_more,
        "mensaje": mensaje,
        "promo_image_url": get_promo_image_url(),
    })


@router.get("/terminos", response_class=HTMLResponse)
async def terminos(request: Request):
    templates = request.app.state.templates
    
    # Obtenemos la promoción activa
    promocion = db.obtener_promocion_activa()
    
    # Filtramos las exclusiones que pertenecen a esta promoción específica
    id_promo = promocion["id_promocion"] if promocion else None
    excluidos = db.obtener_referencias_excluidas(id_promo)
    
    return templates.TemplateResponse(request, "terminos.html", {
        "promocion": promocion,
        "excluidos": excluidos
    })


@router.get("/producto/{referencia}", response_class=HTMLResponse)
async def detalle_producto(request: Request, referencia: str):
    templates = request.app.state.templates
    df = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado (el master ve todas las ciudades).
    # Para un invitado sin cuenta, cae a la ciudad elegida en el login.
    ciudad_usuario = request.session.get("city") or request.session.get("guest_city") or ""
    master = es_master(request)
    df = aplicar_scope_ciudad(request, df)

    # Solo mostrar variantes con stock disponible
    if df is not None and not df.empty and 'Inventario' in df.columns:
        df = df[df['Inventario'] > 0]

    variantes = df[df['Referencia'].astype(str) == str(referencia)]
    if variantes.empty:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    producto = variantes.iloc[0].to_dict()

    # Tallas con su stock (para el master, sumado entre TODAS las ciudades).
    tallas_agrupadas = variantes.groupby(['Talla', 'TallaCM', 'TallaUSCO'], sort=False)['Inventario'].sum().reset_index()
    tallas = tallas_agrupadas.to_dict(orient="records")
    for t in tallas:
        t['stock_ciudad'] = int(t['Inventario'])

    # Para el master: desglose por ciudad + tienda + talla, así sabe en qué
    # ciudad y tienda está cada unidad. Cada fila es seleccionable para
    # reservar desde esa ciudad concreta.
    ubicaciones = []
    if master:
        cols = ['Ciudad', 'Tienda', 'Talla', 'TallaCM', 'TallaUSCO']
        cols = [c for c in cols if c in variantes.columns]
        agrup = variantes.groupby(cols, sort=False)['Inventario'].sum().reset_index()
        agrup = agrup.sort_values(['Ciudad', 'Tienda'], kind='mergesort')
        for r in agrup.to_dict(orient="records"):
            ubicaciones.append({
                "ciudad": str(r.get("Ciudad", "")),
                "tienda": str(r.get("Tienda", "")),
                "talla": str(r.get("Talla", "")),
                "talla_cm": str(r.get("TallaCM", "")),
                "talla_usco": str(r.get("TallaUSCO", "")),
                "stock": int(r.get("Inventario", 0) or 0),
            })

    filtros = get_filtros_completos(df)

    return templates.TemplateResponse(request, "info.html", {
        "producto": producto,
        "tallas": tallas,
        "filtros": filtros,
        "ciudad_usuario": ciudad_usuario,
        "es_master": master,
        "ubicaciones": ubicaciones,
    })


@router.get("/api/sugerencias")
async def api_sugerencias(request: Request, q: str = ""):  # ✅ Request agregado
    if not q or len(q) < 2:
        return JSONResponse([])

    df = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado (el master ve todas las ciudades)
    df = aplicar_scope_ciudad(request, df)

    # Solo mostrar productos con stock disponible
    if df is not None and not df.empty and 'Inventario' in df.columns:
        df = df[df['Inventario'] > 0]

    nombres = df[df['nombre'].str.contains(q, case=False)]['nombre'].unique().tolist()
    referencias = df[df['Referencia'].astype(str).str.contains(q, case=False)]['Referencia'].unique().tolist()

    sugerencias = list(set(
        nombres +
        [str(r) for r in referencias]
    ))[:10]

    return JSONResponse(sugerencias)


@router.post("/admin/recargar-csv")
async def recargar_csv(request: Request):
    """
    Recarga el catálogo desde Supabase en memoria sin reiniciar el servidor.
    Útil cuando el inventario cambia desde fuera de la app.
    Protegido por el middleware de sesión (requiere login).
    """
    try:
        nuevo_df = load_data()
        if nuevo_df.empty:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "mensaje": "La tabla `data` está vacía o no se pudo leer."}
            )
        request.app.state.df = nuevo_df
        total = len(nuevo_df)
        return JSONResponse({"ok": True, "mensaje": f"Catálogo recargado desde Supabase. {total} filas en memoria."})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "mensaje": f"Error al recargar: {str(e)}"}
        )