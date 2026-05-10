from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import pandas as pd

router = APIRouter()
DATA_CSV = "data.csv"

def load_data():
    try:
        df = pd.read_csv(DATA_CSV, encoding='utf-8')
        df.columns = [c.strip() for c in df.columns]
        
        # Mapeo de nombres si es necesario
        if 'Division' not in df.columns and 'División' in df.columns:
            df['Division'] = df['División']
        if 'Genero' not in df.columns and 'Género' in df.columns:
            df['Genero'] = df['Género']

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
    
    filtros = {
        "categorias": sorted([str(x) for x in df_all["Division"].unique()]),
        "generos": sorted([str(x) for x in df_all["Genero"].unique()]),
        "deportes": sorted([str(x) for x in df_all["Deporte"].unique()]),
        "edades": sorted([str(x) for x in df_all["Edad"].unique()]),
        "tallas": sorted([str(x) for x in df_all["Talla"].unique()])
    }
    
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
    tallas = variantes[['Talla', 'Inventario']].to_dict(orient="records")
    
    return templates.TemplateResponse(request, "info.html", {
        "producto": producto,
        "tallas": tallas
    })
