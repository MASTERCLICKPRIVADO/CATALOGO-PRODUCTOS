from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app import home
import re

def format_currency(value):
    """Convierte un valor como '$89950' o '89950' a '$89.950'"""
    if not value:
        return ""
    # Quitar símbolos actuales y convertir a string limpio
    str_val = str(value).replace('$', '').replace('.', '').replace(',', '').strip()
    if not str_val.isdigit():
        return value
    # Formatear con separadores de miles
    formatted = "{:,}".format(int(str_val)).replace(',', '.')
    return f"${formatted}"

def create_app():
    app = FastAPI()

    # Montar estáticos
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Rutas
    app.include_router(home.router)

    # Plantillas y Filtros Personalizados
    templates = Jinja2Templates(directory="templates")
    templates.env.filters["currency"] = format_currency

    # Guardar templates en el estado de la app para que el router pueda acceder a los filtros
    app.state.templates = templates

    return app
