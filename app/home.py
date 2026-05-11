from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import pandas as pd

router = APIRouter()
DATA_CSV = "data.csv"

def load_data():
    try:
        df = pd.read_csv(DATA_CSV, encoding='utf-8')
        df.columns = [c.strip() for c in df.columns]
        
        # Asegurar que Inventario sea numérico
        if 'Inventario' in df.columns:
            df['Inventario'] = pd.to_numeric(df['Inventario'], errors='coerce').fillna(0).astype(int)

        # Mapeo de nombres si es necesario (para que coincidan con los templates)
        if 'Division' in df.columns:
            df['División'] = df['Division']
        if 'Genero' in df.columns:
            df['Género'] = df['Genero']

        # Limpieza de nulos
        df['Division'] = df['Division'].fillna('Sin Categoría').astype(str)
        df['Genero'] = df['Genero'].fillna('Unisex').astype(str)
        df['Deporte'] = df['Deporte'].fillna('General').astype(str)
        df['Edad'] = df['Edad'].fillna('Todas').astype(str)
        df['Talla'] = df['Talla'].fillna('N/A').astype(str)
        df['nombre'] = df['nombre'].fillna('Sin Nombre').astype(str)
        
        return df
    except FileNotFoundError:
        return pd.DataFrame()

@router.get("/", response_class=HTMLResponse)
async def ver_catalogo(request: Request, page: int = 1):
    templates = request.app.state.templates
    df = load_data()
    if df.empty:
        return templates.TemplateResponse(request, "home.html", {"productos": [], "mensaje": "No hay productos disponibles."})
    
    df_unique = df.drop_duplicates(subset=['Referencia'])
    
    limit = 12
    start = (page - 1) * limit
    end = start + limit
    productos = df_unique.iloc[start:end].to_dict(orient="records")
    
    has_more = len(df_unique) > end
    
    filtros = {
        "categorias": sorted([str(x) for x in df["Division"].unique()]),
        "generos": sorted([str(x) for x in df["Genero"].unique()]),
        "deportes": sorted([str(x) for x in df["Deporte"].unique()]),
        "edades": sorted([str(x) for x in df["Edad"].unique()]),
        "tallas": sorted([str(x) for x in df["Talla"].unique()])
    }
    
    return templates.TemplateResponse(request, "home.html", {
        "productos": productos,
        "filtros": filtros,
        "page": page,
        "has_more": has_more
    })

@router.get("/api/productos")
async def api_productos(
    page: int = 1,
    q: str = "",
    categoria: str = "",
    genero: str = "",
    deporte: str = "",
    edad: str = "",
    talla: str = ""
):
    df = load_data()
    if q:
        df = df[
            df['nombre'].str.contains(q, case=False) | 
            df['Referencia'].astype(str).str.contains(q, case=False) |
            df['Talla'].astype(str).str.contains(q, case=False)
        ]
    if categoria:
        df = df[df['Division'] == categoria]
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
    
    return JSONResponse({
        "productos": productos,
        "has_more": has_more
    })

@router.get("/buscar", response_class=HTMLResponse)
async def buscar_productos(
    request: Request, 
    q: str = "", 
    categoria: str = "", 
    genero: str = "", 
    deporte: str = "",
    edad: str = "",
    talla: str = "",
    page: int = 1
):
    templates = request.app.state.templates
    df_all = load_data()
    df = df_all.copy()
    
    if q:
        df = df[
            df['nombre'].str.contains(q, case=False) | 
            df['Referencia'].astype(str).str.contains(q, case=False) |
            df['Talla'].astype(str).str.contains(q, case=False)
        ]
    if categoria:
        df = df[df['Division'] == categoria]
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
    
    # Calcular opciones de filtros siempre sobre el total de datos (Independientes)
    def get_filtros_independientes(df_f):
        return {
            "categorias": sorted([str(x) for x in df_f["Division"].unique()]),
            "generos": sorted([str(x) for x in df_f["Genero"].unique()]),
            "deportes": sorted([str(x) for x in df_f["Deporte"].unique()]),
            "edades": sorted([str(x) for x in df_f["Edad"].unique()]),
            "tallas": sorted([str(x) for x in df_f["Talla"].unique()])
        }

    filtros = get_filtros_independientes(df_all)
    
    return templates.TemplateResponse(request, "home.html", {
        "productos": productos, 
        "filtros": filtros,
        "query": q,
        "sel_cat": categoria,
        "sel_gen": genero,
        "sel_dep": deporte,
        "sel_edad": edad,
        "sel_talla": talla,
        "page": page,
        "has_more": has_more
    })

@router.get("/producto/{referencia}", response_class=HTMLResponse)
async def detalle_producto(request: Request, referencia: str):
    templates = request.app.state.templates
    df = load_data()
    variantes = df[df['Referencia'].astype(str) == str(referencia)]
    if variantes.empty:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    
    producto = variantes.iloc[0].to_dict()
    
    # Unificar cantidad disponible por talla sumando el inventario de todas las tiendas
    tallas_agrupadas = variantes.groupby('Talla', sort=False)['Inventario'].sum().reset_index()
    tallas = tallas_agrupadas.to_dict(orient="records")
    
    return templates.TemplateResponse(request, "info.html", {
        "producto": producto,
        "tallas": tallas
    })

@router.get("/api/sugerencias")
async def api_sugerencias(q: str = ""):
    if not q or len(q) < 2:
        return JSONResponse([])
    
    df = load_data()
    
    # Buscar en múltiples campos
    nombres = df[df['nombre'].str.contains(q, case=False)]['nombre'].unique().tolist()
    referencias = df[df['Referencia'].astype(str).str.contains(q, case=False)]['Referencia'].unique().tolist()
    tallas = df[df['Talla'].astype(str).str.contains(q, case=False)]['Talla'].unique().tolist()
    divisiones = df[df['Division'].str.contains(q, case=False)]['Division'].unique().tolist()
    deportes = df[df['Deporte'].str.contains(q, case=False)]['Deporte'].unique().tolist()
    
    # Combinar todas las sugerencias, eliminar duplicados y limitar resultados
    sugerencias = list(set(
        nombres + 
        [str(r) for r in referencias] + 
        [str(t) for t in tallas] + 
        divisiones + 
        deportes
    ))[:10]
    
    return JSONResponse(sugerencias)
