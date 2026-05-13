from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import pandas as pd
import os
import csv

router = APIRouter()
USUARIOS_CSV = "usuarios.csv"

def load_users():
    if not os.path.exists(USUARIOS_CSV):
        return pd.DataFrame(columns=['Usuario', 'Contrasenia', 'Ciudad'])
    try:
        df = pd.read_csv(USUARIOS_CSV, sep=';', encoding='utf-8')
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception:
        return pd.DataFrame(columns=['Usuario', 'Contrasenia', 'Ciudad'])

@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": None})

@router.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    df = load_users()
    
    # Buscar usuario
    user_row = df[df['Usuario'] == username]
    
    if not user_row.empty:
        # Verificar contraseña (en un entorno real usaríamos hashing)
        if str(user_row.iloc[0]['Contrasenia']) == str(password):
            request.session["user"] = username
            request.session["city"] = user_row.iloc[0]['Ciudad']
            return RedirectResponse(url="/", status_code=303)
    
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": "Usuario o contraseña incorrectos"})

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")
