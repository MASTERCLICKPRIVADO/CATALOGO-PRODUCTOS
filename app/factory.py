from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from app import home, auth, cart, powerbi, db
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
        path = request.url.path

        # 1) Rutas SIEMPRE públicas (no requieren ni sesión ni ciudad).
        #    /api/powerbi se autentica con X-API-Key dentro de su router.
        #    /catalogo es lo que GUARDA la ciudad del invitado en sesión.
        always_public = [
            "/login", "/registro", "/static", "/api/powerbi", "/catalogo",
        ]
        # 2) Rutas de catálogo de "solo lectura": un invitado puede verlas,
        #    pero SOLO si ya eligió ciudad en la tarjeta "Catálogo" del login
        #    (o está logueado). Sin ninguna de las dos → vuelve a /login para
        #    que elija primero. "/" se compara exacto: con startswith() TODA
        #    ruta empieza con "/" y dejaría la app entera sin protección.
        guest_catalog = [
            "/buscar", "/api/productos", "/api/sugerencias", "/producto",
        ]

        is_always_public = any(path.startswith(p) for p in always_public)
        is_catalog = path == "/" or any(path.startswith(p) for p in guest_catalog)

        if is_always_public:
            pass  # acceso libre
        elif is_catalog:
            # Invitado debe haber elegido ciudad; logueado siempre pasa.
            if "user" not in request.session and not request.session.get("guest_city"):
                return RedirectResponse(url="/login")
        elif "user" not in request.session:
            return RedirectResponse(url="/login")

        response = await call_next(request)

        # Tras servir el PRIMER HTML después del login, limpiamos la bandera
        # `fresh_login`. Así, en cualquier request posterior, el cliente sabe
        # que debe validar el marcador de sessionStorage (y deslogear si no
        # existe, lo cual indica que el navegador/pestaña fue cerrado y
        # reabierto). Sólo limpiamos en respuestas HTML para no perder la
        # bandera por culpa de calls a APIs (/api/..., /static, etc.).
        if request.session.get("fresh_login"):
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                request.session.pop("fresh_login", None)

        return response

    # Sesiones
    app.add_middleware(SessionMiddleware, secret_key="una_clave_secreta_muy_segura", max_age=None)

    # Estáticos
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Rutas
    app.include_router(home.router)
    app.include_router(auth.router)
    app.include_router(cart.router)
    app.include_router(powerbi.router)

    # Plantillas y filtros
    templates = Jinja2Templates(directory="templates")
    templates.env.filters["currency"] = format_currency
    app.state.templates = templates

    return app