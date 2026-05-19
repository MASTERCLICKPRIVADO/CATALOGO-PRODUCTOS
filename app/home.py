from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
import pandas as pd

from app import db

router = APIRouter()

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
    "precio_antes": "precio antes",
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
                              categoria, subcategoria, talla_cm, talla_co, "talla_u.s_co"
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
            df_f = df_f[df_f['Talla'] == talla]
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
        "tallas":         _valores_unicos(filtrar(df, 'talla'),         "Talla"),
    }


@router.get("/", response_class=HTMLResponse)
async def ver_catalogo(request: Request, page: int = 1):
    templates = request.app.state.templates
    df = request.app.state.df  # ✅ Desde memoria, no desde disco

    if df is None or df.empty:
        return templates.TemplateResponse(request, "home.html", {"productos": [], "mensaje": "No hay productos disponibles."})

    # Filtrar por ciudad del usuario logueado
    ciudad_usuario = request.session.get("city", "")
    df = filtrar_por_ciudad(df, ciudad_usuario)

    df_unique = df.drop_duplicates(subset=['Referencia'])

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
    talla: str = ""
):
    df = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado
    ciudad_usuario = request.session.get("city", "")
    df = filtrar_por_ciudad(df, ciudad_usuario)

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
        df = df[df['Talla'] == talla]

    df_unique = df.drop_duplicates(subset=['Referencia'])

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
    page: int = 1
):
    templates = request.app.state.templates
    df_all = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado
    ciudad_usuario = request.session.get("city", "")
    df_all = filtrar_por_ciudad(df_all, ciudad_usuario)

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
        df = df[df['Talla'] == talla]

    df_unique = df.drop_duplicates(subset=['Referencia'])

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
        "page": page,
        "has_more": has_more,
        "mensaje": mensaje
    })


@router.get("/producto/{referencia}", response_class=HTMLResponse)
async def detalle_producto(request: Request, referencia: str):
    templates = request.app.state.templates
    df = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado
    ciudad_usuario = request.session.get("city", "")
    df = filtrar_por_ciudad(df, ciudad_usuario)

    variantes = df[df['Referencia'].astype(str) == str(referencia)]
    if variantes.empty:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    producto = variantes.iloc[0].to_dict()

    # Tallas con su stock en la ciudad del usuario
    tallas_agrupadas = variantes.groupby(['Talla', 'TallaCM', 'TallaUSCO'], sort=False)['Inventario'].sum().reset_index()
    tallas = tallas_agrupadas.to_dict(orient="records")
    for t in tallas:
        t['stock_ciudad'] = int(t['Inventario'])

    filtros = get_filtros_completos(df)

    return templates.TemplateResponse(request, "info.html", {
        "producto": producto,
        "tallas": tallas,
        "filtros": filtros,
        "ciudad_usuario": ciudad_usuario,
    })


@router.get("/api/sugerencias")
async def api_sugerencias(request: Request, q: str = ""):  # ✅ Request agregado
    if not q or len(q) < 2:
        return JSONResponse([])

    df = request.app.state.df  # ✅ Desde memoria

    # Filtrar por ciudad del usuario logueado
    ciudad_usuario = request.session.get("city", "")
    df = filtrar_por_ciudad(df, ciudad_usuario)

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