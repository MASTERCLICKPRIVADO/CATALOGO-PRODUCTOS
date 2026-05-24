from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    user = db.obtener_usuario(username)
    if user and str(user.get("contrasenia")) == str(password):
        request.session["user"] = user["usuario"]
        request.session["city"] = user.get("ciudad", "")
        request.session["show_promo"] = True
        # Bandera que indica "este es el primer pageview tras el login";
        # el cliente la usa para inicializar el marcador de sessionStorage.
        request.session["fresh_login"] = True
        return RedirectResponse(url="/", status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": "Usuario o contraseña incorrectos"})


@router.get("/perfil", response_class=HTMLResponse)
async def perfil(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "perfil.html", {})


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")
