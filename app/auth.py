import re

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _render_login(request: Request, *, error: str = None, form: dict = None):
    """Helper para renderizar login.html re-inyectando los valores ya tipeados."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "form": form or {}},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return _render_login(request)


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    codigo_referido: str = Form(...),
):
    """
    Flujo de login con código de referido obligatorio:
      1. Validar usuario + contraseña.
      2. Validar que el código de referido existe en directorio_empleados.
      3. Sobreescribir codigo_referido y ciudad en usuarios (la ciudad la
         determina el código ingresado en este login, no la que tenía antes).
      4. Setear sesión con los valores NUEVOS y redirigir al catálogo.
    """
    # No exponemos password en form_data por seguridad — el usuario reescribe.
    form_data = {"username": username, "codigo_referido": codigo_referido}

    user = db.obtener_usuario(username)
    if not user or str(user.get("contrasenia")) != str(password):
        return _render_login(
            request,
            error="Usuario o contraseña incorrectos",
            form=form_data,
        )

    cod = (codigo_referido or "").strip()
    if not cod:
        return _render_login(
            request,
            error="Debes ingresar un código de referido",
            form=form_data,
        )

    referido = db.obtener_referido(cod)
    if not referido:
        return _render_login(
            request,
            error="El codigo de referido ingresado no esta registrado",
            form=form_data,
        )

    ciudad = (referido.get("ciudad") or "").strip()
    if not ciudad:
        return _render_login(
            request,
            error="El código de referido no tiene una ciudad asignada. Contacta al administrador.",
            form=form_data,
        )

    # Persistir en BD: el código actual reemplaza al anterior, y la ciudad
    # del usuario pasa a ser la del código (filtra todo el catálogo).
    db.actualizar_referido_y_ciudad(user["usuario"], cod, ciudad)

    # Setear sesión con los datos NUEVOS (no los previos del usuario).
    request.session["user"] = user["usuario"]
    request.session["city"] = ciudad
    request.session["referral_code"] = cod
    request.session["show_promo"] = True
    # Bandera que indica "este es el primer pageview tras el login";
    # el cliente la usa para inicializar el marcador de sessionStorage.
    request.session["fresh_login"] = True
    return RedirectResponse(url="/", status_code=303)


@router.get("/perfil", response_class=HTMLResponse)
async def perfil(request: Request):
    """
    Renderiza el perfil del usuario logueado. La fuente de verdad es la
    BD (no la sesión), así el código de referido se ve correcto aunque
    la sesión sea antigua o no tenga el campo cacheado.
    """
    templates = request.app.state.templates
    usuario_id = request.session.get("user")

    # Fetch fresco del usuario para que `codigo_referido` venga de la BD
    # actual y no dependa de qué se guardó en la sesión.
    perfil_data = db.obtener_usuario(usuario_id) or {}

    # Refrescamos la sesión por si estaba desactualizada (login viejo
    # sin el campo, por ejemplo). Así otras vistas también se ven bien.
    if perfil_data.get("codigo_referido"):
        request.session["referral_code"] = perfil_data["codigo_referido"]
    if perfil_data.get("ciudad"):
        request.session["city"] = perfil_data["ciudad"]

    return templates.TemplateResponse(
        request,
        "perfil.html",
        {"perfil": perfil_data},
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


# ----------------------- REGISTRO -----------------------

def _render_registro(request: Request, *, error: str = None, form: dict = None):
    """Helper para renderizar registro.html re-inyectando los valores ya tipeados."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "registro.html",
        {"error": error, "form": form or {}},
    )


@router.get("/registro", response_class=HTMLResponse)
async def registro_get(request: Request):
    return _render_registro(request)


@router.post("/registro")
async def registro_post(
    request: Request,
    correo: str = Form(...),
    contrasenia: str = Form(...),
    contrasenia2: str = Form(...),
    codigo_referido: str = Form(...),
):
    """
    Flujo de registro:
      1. Validar formato de los campos.
      2. Validar que el código de referido EXISTE en directorio_empleados.
         Si no existe → error "El codigo de referido ingresado no esta registrado".
      3. Validar que el correo no esté ya registrado.
      4. Crear el usuario heredando la ciudad del código de referido.
      5. Loguear al usuario automáticamente y redirigir al catálogo.
    """
    form_data = {"correo": correo, "codigo_referido": codigo_referido}

    correo_norm = (correo or "").strip().lower()
    pwd = (contrasenia or "")
    pwd2 = (contrasenia2 or "")
    cod = (codigo_referido or "").strip()

    # 1) Validaciones de formato
    if not correo_norm or not pwd or not cod:
        return _render_registro(request, error="Todos los campos son obligatorios.", form=form_data)

    if not _EMAIL_RE.match(correo_norm):
        return _render_registro(request, error="El correo no tiene un formato válido.", form=form_data)

    if len(pwd) < 6:
        return _render_registro(
            request,
            error="La contraseña debe tener al menos 6 caracteres.",
            form=form_data,
        )

    if pwd != pwd2:
        return _render_registro(request, error="Las contraseñas no coinciden.", form=form_data)

    # 2) El código de referido DEBE existir antes de tocar `usuarios`
    referido = db.obtener_referido(cod)
    if not referido:
        return _render_registro(
            request,
            error="El codigo de referido ingresado no esta registrado",
            form=form_data,
        )

    ciudad = (referido.get("ciudad") or "").strip()
    if not ciudad:
        # Defensa por si la fila existe pero no tiene ciudad definida
        return _render_registro(
            request,
            error="El código de referido no tiene una ciudad asignada. Contacta al administrador.",
            form=form_data,
        )

    # 3 + 4) Crear usuario (la función devuelve False si el correo ya existe)
    creado = db.crear_usuario(
        correo=correo_norm,
        contrasenia=pwd,
        ciudad=ciudad,
        codigo_referido=cod,
    )
    if not creado:
        return _render_registro(
            request,
            error="Este correo ya está registrado. Inicia sesión.",
            form=form_data,
        )

    # 5) Login automático del usuario recién creado
    request.session["user"] = correo_norm
    request.session["city"] = ciudad
    request.session["referral_code"] = cod
    request.session["show_promo"] = True
    request.session["fresh_login"] = True
    return RedirectResponse(url="/", status_code=303)
