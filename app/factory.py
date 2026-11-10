from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from app import home, auth, cart, powerbi, admin, db, temporada
import os
import re

load_dotenv()


# ──────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN DE LA SESIÓN
# ──────────────────────────────────────────────────────────────────────────
# Clave con la que se FIRMA la cookie de sesión. Nunca va escrita en el
# código: quien la conozca puede fabricarse una cookie con el usuario y los
# permisos que quiera (por ejemplo `permisos: master`, que ve todo el
# inventario de todas las ciudades). Se define en el entorno: en el `.env`
# para local y como variable de entorno en Render.
#
# Para generar una nueva:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
#
# Si la cambias, todas las sesiones abiertas se invalidan y los usuarios
# vuelven a /login una vez. Eso es normal y esperado.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "Falta SECRET_KEY en el entorno. Genera una con "
        '`python -c "import secrets; print(secrets.token_urlsafe(48))"` '
        "y definela en tu .env (local) y en las variables de entorno de Render."
    )

# La cookie se marca `Secure` → el navegador solo la envía por HTTPS, así no
# viaja en claro. Los navegadores actuales aceptan cookies `Secure` en
# http://localhost, por lo que el desarrollo local funciona igual; si tu
# entorno no lo hiciera, pon SESSION_COOKIE_SECURE=false en el .env.
SESSION_COOKIE_SECURE = (
    os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower()
    not in ("false", "0", "no")
)

# Vida máxima de la sesión, en segundos (8 horas por defecto). Acota cuánto
# sirve una cookie robada: pasado ese tiempo la firma caduca y hay que volver
# a entrar. El marcador por pestaña de templates/base.html sigue cerrando la
# sesión cuando se cierra el navegador, así que la experiencia no cambia.
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", "28800"))


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
        # Anotamos qué versión de la temporada corresponde a lo que
        # acabamos de cargar, para que la primera petición no lo recargue
        # otra vez sin motivo.
        temporada.marcar_catalogo_cargado()
        print("✅ CSV cargado en memoria correctamente")
        yield
        # Cleanup al apagar (opcional)
        app.state.df = None

    app = FastAPI(lifespan=lifespan)

    # Middleware de Autenticación
    # Rutas que siguen funcionando con la TEMPORADA CERRADA. El admin tiene
    # que poder entrar (y para eso, iniciar sesión) para preparar y abrir la
    # siguiente. Lo demás se queda detrás del aviso de cerrado.
    ABIERTAS_SIEMPRE = (
        "/admin", "/login", "/logout", "/static", "/favicon.ico", "/api/powerbi",
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # ── Temporada cerrada ─────────────────────────────────────────
        # Va ANTES que el control de sesión: con la tienda cerrada no hay
        # nada que mirar, tengas cuenta o no.
        #
        # `asegurar_catalogo` aprovecha el paso para comprobar, como mucho
        # cada 10 segundos, si otra copia del servidor publicó una
        # temporada nueva; si es así, recarga el catálogo de esta copia.
        estado_temporada = temporada.estado_vigente()
        temporada.asegurar_catalogo(request.app)

        if not estado_temporada["abierta"] and not path.startswith(ABIERTAS_SIEMPRE):
            return request.app.state.templates.TemplateResponse(
                request, "cerrado.html", {}, status_code=503,
            )

        # 1) Rutas SIEMPRE públicas (no requieren ni sesión ni ciudad).
        #    /api/powerbi se autentica con X-API-Key dentro de su router.
        #    /catalogo es lo que GUARDA la ciudad del invitado en sesión.
        always_public = [
            "/login", "/registro", "/static", "/api/powerbi", "/catalogo",
            "/cambiar-ciudad", "/favicon.ico",
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
    app.add_middleware(
        SessionMiddleware,
        secret_key=SECRET_KEY,
        max_age=SESSION_MAX_AGE,
        # `lax`: la cookie no se envía en peticiones POST que vengan de otro
        # sitio, lo que corta los ataques CSRF más comunes contra los
        # formularios de la app (login, carrito, reserva).
        same_site="lax",
        https_only=SESSION_COOKIE_SECURE,
    )

    # Estáticos
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Rutas
    app.include_router(home.router)
    app.include_router(auth.router)
    app.include_router(cart.router)
    app.include_router(powerbi.router)
    # Panel de admin (/admin/descuentos). Cada ruta del router exige el
    # permiso 'admin' verificado contra la BD, no solo sesión.
    app.include_router(admin.router)

    # Plantillas y filtros
    templates = Jinja2Templates(directory="templates")
    templates.env.filters["currency"] = format_currency
    app.state.templates = templates

    return app