from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from app import home, auth
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

    # Middleware de Autenticación
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Rutas permitidas sin login
        public_paths = ["/login", "/static"]
        
        path = request.url.path
        is_public = any(path.startswith(p) for p in public_paths)
        
        if not is_public and "user" not in request.session:
            return RedirectResponse(url="/login")
            
        response = await call_next(request)
        return response

    # Configuración de Sesiones (Debe ir después del middleware de auth para que se ejecute primero)
    # max_age=None hace que la cookie expire al cerrar el navegador
    app.add_middleware(SessionMiddleware, secret_key="una_clave_secreta_muy_segura", max_age=None)

    # Montar estáticos
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Rutas
    app.include_router(home.router)
    app.include_router(auth.router)

    # Plantillas y Filtros Personalizados
    templates = Jinja2Templates(directory="templates")
    templates.env.filters["currency"] = format_currency

    # Guardar templates en el estado de la app para que el router pueda acceder a los filtros
    app.state.templates = templates

    return app
