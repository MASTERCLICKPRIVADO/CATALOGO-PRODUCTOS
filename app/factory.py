from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from app import home, auth, cart, db
import re


def format_currency(value):
    """Convierte un valor como '$89950' o '89950' a '$89.950'"""
    if not value:
        return ""
    str_val = str(value).replace('$', '').replace('.', '').replace(',', '').strip()
    if not str_val.isdigit():
        return value
    formatted = "{:,}".format(int(str_val)).replace(',', '.')
    return f"${formatted}"


def create_app():

    # Cargar el CSV UNA sola vez al arrancar el servidor
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_storage()
        app.state.df = home.load_data()
        print("✅ CSV cargado en memoria correctamente")
        yield
        # Cleanup al apagar (opcional)
        app.state.df = None

    app = FastAPI(lifespan=lifespan)

    # Middleware de Autenticación
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        public_paths = ["/login", "/static"]
        path = request.url.path
        is_public = any(path.startswith(p) for p in public_paths)

        if not is_public and "user" not in request.session:
            return RedirectResponse(url="/login")

        response = await call_next(request)
        return response

    # Sesiones
    app.add_middleware(SessionMiddleware, secret_key="una_clave_secreta_muy_segura", max_age=None)

    # Estáticos
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Rutas
    app.include_router(home.router)
    app.include_router(auth.router)
    app.include_router(cart.router)

    # Plantillas y filtros
    templates = Jinja2Templates(directory="templates")
    templates.env.filters["currency"] = format_currency
    app.state.templates = templates

    return app