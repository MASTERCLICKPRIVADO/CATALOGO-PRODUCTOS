"""
PANEL DE ADMINISTRACIÓN  —  plantillas de descuento
===================================================

Apartado web para que el usuario con permiso 'admin' gestione los descuentos
por cantidad sin tocar el código ni volver a desplegar. Antes esto se editaba
a mano en app/logicas_descuento.py.

El admin guarda varias PLANTILLAS ("Normal", "Navidad", "Black Friday"), cada
una con sus tramos y con su propio modo acumulativo, y activa la que quiera.
El carrito usa siempre la plantilla activa.

ENCENDIDO Y APAGADO — y por qué hay que desactivar para tocar algo
------------------------------------------------------------------
Que haya o no una plantilla activa es lo único que enciende la promoción;
no existe otro interruptor. Y mientras una está activa no se puede editar
ni reemplazar por otra: primero se desactiva.

Es un paso de más a propósito. Esto cambia el precio que ve el cliente en
el momento, así que apagar la promoción y cambiarla son dos decisiones
distintas, cada una con su confirmación, en vez de un resbalón.

SEGURIDAD — cómo se autoriza esto
---------------------------------
1. `requiere_admin` es una dependencia de FastAPI aplicada a TODAS las rutas
   de este router (GET y POST). No hay ninguna ruta aquí sin ella.

2. Esa dependencia NO le cree a la cookie: lee el permiso de Postgres en cada
   petición (`home.permisos_en_bd`). Dos razones:
      - /registro es abierto, así que "estar logueado" no significa nada.
      - Si se revoca el rol con un UPDATE, deja de valer en el momento, sin
        esperar a que la persona vuelva a iniciar sesión.

3. El botón del navbar es solo cosmético (mira la sesión). Quien conozca la
   URL igual choca contra el 403 de aquí: la autorización vive en el servidor.

4. Los rangos se validan dos veces: aquí (`validar_reglas`, con mensajes
   entendibles) y en la propia BD con los CHECK de la tabla, que es lo que
   impide un 400% por un dedazo aunque este archivo tuviera un bug.

5. Cada cambio queda en `descuentos_auditoria` con el estado anterior y el
   nuevo. Esto toca el precio que ve el cliente: tiene que quedar rastro.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import db
from app import home
from app import logicas_descuento

router = APIRouter(prefix="/admin")


# Límites de lo que se puede configurar desde el panel. Los mismos valores
# están como CHECK en la tabla (ver migraciones/migracion_descuentos.sql).
MAX_TRAMOS = 10
MIN_ITEMS_MIN = 1
MIN_ITEMS_MAX = 99
PORCENTAJE_MIN = 0
PORCENTAJE_MAX = 90
MAX_LARGO_NOMBRE = 40

# Mensajes de resultado. Viajan como `?msg=` en el redirect posterior a cada
# POST, para que un F5 no reenvíe el formulario.
MENSAJES = {
    "creada": "Plantilla creada. Ya puedes configurar sus tramos.",
    "guardada": "Plantilla guardada.",
    "activada": "Plantilla activada: es la que se aplica en el carrito.",
    "duplicada": "Plantilla duplicada. La copia queda inactiva.",
    "borrada": "Plantilla borrada.",
    "desactivada": ("Promoción desactivada: ninguna plantilla se está "
                    "aplicando y los artículos se cobran a su precio normal."),
}


def _candado_activa(nombre: str) -> str:
    """
    El mismo aviso en todos lados, para que no haya dos redacciones.

    Mientras una plantilla está activa no se toca: ni se edita, ni se
    reemplaza por otra. Primero se desactiva. Así nunca se le cambia el
    precio por debajo a un cliente que ya tiene el carrito abierto.
    """
    return (f"«{nombre}» está activa en este momento. Desactiva la promoción "
            f"antes de modificarla o de activar otra.")


async def requiere_admin(request: Request):
    """
    Dependencia de autorización: solo continúa si el usuario en sesión tiene
    `permisos = 'admin'` EN LA BASE DE DATOS.

    Cualquier otro caso (invitado, usuario normal, master, o una cookie
    falsificada que diga 'admin') recibe 403.
    """
    if home.permisos_en_bd(request) != "admin":
        raise HTTPException(
            status_code=403,
            detail="No tienes permiso para entrar al panel de administración.",
        )


def validar_nombre(nombre: str):
    """Devuelve `(nombre_limpio, error)`."""
    limpio = str(nombre or "").strip()
    if not limpio:
        return "", "La plantilla necesita un nombre."
    if len(limpio) > MAX_LARGO_NOMBRE:
        return "", f"El nombre no puede pasar de {MAX_LARGO_NOMBRE} caracteres."
    return limpio, None


def validar_reglas(minimos_bruto, porcentajes_bruto, activos_bruto):
    """
    Convierte lo que llegó del formulario en una lista de tramos válida.

    Devuelve `(reglas, error, avisos)`:
      - reglas -> [{"min_items", "porcentaje", "activo"}, ...] ordenadas
      - error  -> mensaje que BLOQUEA el guardado, o None
      - avisos -> mensajes que NO bloquean (configuraciones raras pero legales)

    Las filas vacías se descartan en silencio: el formulario deja agregar
    filas y es normal que quede alguna sin llenar.
    """
    avisos = []

    if not (len(minimos_bruto) == len(porcentajes_bruto) == len(activos_bruto)):
        return [], "El formulario llegó incompleto. Vuelve a cargar la página.", avisos

    reglas = []
    vistos = set()

    for minimo_txt, pct_txt, activo_txt in zip(minimos_bruto, porcentajes_bruto, activos_bruto):
        minimo_txt = str(minimo_txt or "").strip()
        pct_txt = str(pct_txt or "").strip()

        # Fila vacía -> se ignora.
        if not minimo_txt and not pct_txt:
            continue

        if not minimo_txt or not pct_txt:
            return [], "Hay un tramo con la cantidad o el porcentaje sin llenar.", avisos

        try:
            minimo = int(minimo_txt)
            pct = int(pct_txt)
        except ValueError:
            return [], "La cantidad y el porcentaje deben ser números enteros.", avisos

        if not (MIN_ITEMS_MIN <= minimo <= MIN_ITEMS_MAX):
            return [], (
                f"La cantidad de artículos debe estar entre {MIN_ITEMS_MIN} y "
                f"{MIN_ITEMS_MAX}. Recibí {minimo}."
            ), avisos

        if not (PORCENTAJE_MIN <= pct <= PORCENTAJE_MAX):
            return [], (
                f"El porcentaje debe estar entre {PORCENTAJE_MIN} y {PORCENTAJE_MAX}. "
                f"Recibí {pct}. Ese techo existe para que un error de tipeo no "
                f"regale la mercancía."
            ), avisos

        if minimo in vistos:
            return [], (
                f"Hay dos tramos para la misma cantidad ({minimo} artículos). "
                f"Deja solo uno."
            ), avisos
        vistos.add(minimo)

        reglas.append({
            "min_items": minimo,
            "porcentaje": pct,
            "activo": str(activo_txt).strip() in ("1", "true", "on", "si"),
        })

    if len(reglas) > MAX_TRAMOS:
        return [], f"Máximo {MAX_TRAMOS} tramos. Tienes {len(reglas)}.", avisos

    reglas.sort(key=lambda r: r["min_items"])

    # Avisos: no bloquean, pero casi siempre son un descuido.
    activas = [r for r in reglas if r["activo"]]
    for anterior, siguiente in zip(activas, activas[1:]):
        if siguiente["porcentaje"] <= anterior["porcentaje"]:
            avisos.append(
                f"Ojo: con {siguiente['min_items']} artículos el descuento "
                f"({siguiente['porcentaje']}%) no mejora el de "
                f"{anterior['min_items']} artículos ({anterior['porcentaje']}%). "
                f"Se aplicará igual, pero al cliente no le conviene llevar más."
            )

    return reglas, None, avisos


def _error_bd(e: Exception) -> str:
    """Traduce los fallos de BD más probables a algo que se entienda."""
    texto = str(e)
    if "plantillas_descuento_nombre_key" in texto:
        return "Ya existe una plantilla con ese nombre."
    if "does not exist" in texto and "plantillas_descuento" in texto:
        return (
            "Falta ejecutar migraciones/migracion_plantillas.sql en Supabase: "
            "la tabla de plantillas todavía no existe."
        )
    if "does not exist" in texto:
        return (
            "Faltan tablas en la base de datos. Ejecuta los archivos de "
            f"migraciones/ en Supabase. Detalle: {texto}"
        )
    return f"La base de datos rechazó el cambio: {texto}"


def _pide_json(request: Request) -> bool:
    """
    ¿La petición viene del JavaScript del panel?

    El panel manda la cabecera `X-Panel-Json: 1` y espera JSON, para
    actualizar la pantalla sin recargarla. Sin JavaScript (o si el fetch
    falla) el mismo formulario se envía normal y se responde HTML, así que
    el panel sigue funcionando igual. Una sola ruta por acción, dos formas
    de contestar.
    """
    return request.headers.get("x-panel-json") == "1"


# ──────────────────────────────────────────────────────────────────────────
# LISTA DE PLANTILLAS
# ──────────────────────────────────────────────────────────────────────────

def _render_lista(request: Request, *, error=None, msg=None):
    templates = request.app.state.templates

    try:
        estado = db.listar_plantillas()
    except Exception as e:
        estado = {"activo": False, "plantillas": []}
        error = error or _error_bd(e)

    activa = next((p for p in estado["plantillas"] if p["activa"]), None)
    inactivas = [p for p in estado["plantillas"] if not p["activa"]]

    try:
        auditoria = db.obtener_auditoria_descuentos(limite=8)
    except Exception as e:
        print(f"⚠️  No se pudo leer la auditoría de descuentos: {e}")
        auditoria = []

    return templates.TemplateResponse(
        request,
        "admin_descuentos.html",
        {
            "plantillas": estado["plantillas"],
            "descuento_activo": estado["activo"],
            # La activa aparte: la pantalla la trata distinto (se anuncia
            # arriba, no se puede editar y solo se le ofrece desactivar).
            "plantilla_activa": activa,
            "inactivas": inactivas,
            "error": error,
            "msg": msg,
            "auditoria": auditoria,
            "origen": logicas_descuento.origen_de_las_reglas(),
            "max_largo_nombre": MAX_LARGO_NOMBRE,
        },
    )


@router.get("", dependencies=[Depends(requiere_admin)])
async def admin_inicio():
    """Por ahora el panel solo tiene descuentos, así que entra directo ahí."""
    return RedirectResponse(url="/admin/descuentos", status_code=303)


@router.get("/descuentos", response_class=HTMLResponse,
            dependencies=[Depends(requiere_admin)])
async def ver_descuentos(request: Request, msg: str = "", error: str = ""):
    return _render_lista(request, error=error or None, msg=MENSAJES.get(msg))


@router.post("/descuentos/desactivar", dependencies=[Depends(requiere_admin)])
async def desactivar(request: Request):
    """
    Apaga la promoción dejando todas las plantillas inactivas.

    Es el paso obligatorio antes de editar la plantilla que está aplicándose
    o de poner otra en su lugar.

    Va por envío normal, no por fetch: al cambiar la promoción cambia media
    pantalla, y recargarla una vez sale más barato que pedir JSON y después
    recargar igual.
    """
    try:
        db.desactivar_plantillas(str(request.session.get("user") or ""))
    except Exception as e:
        return _render_lista(request, error=_error_bd(e))

    logicas_descuento.invalidar_cache()
    return RedirectResponse(url="/admin/descuentos?msg=desactivada", status_code=303)


@router.post("/descuentos/plantillas/nueva", dependencies=[Depends(requiere_admin)])
async def crear_plantilla(request: Request, nombre: str = Form(""),
                          acumulativo: str = Form("")):
    """Crea una plantilla vacía e inactiva, y abre su editor."""
    limpio, error = validar_nombre(nombre)
    if error:
        return _render_lista(request, error=error)

    try:
        plantilla_id = db.crear_plantilla(
            limpio,
            str(acumulativo).strip() in ("1", "true", "on", "si"),
            [],
            str(request.session.get("user") or ""),
        )
    except Exception as e:
        return _render_lista(request, error=_error_bd(e))

    return RedirectResponse(
        url=f"/admin/descuentos/plantillas/{plantilla_id}?msg=creada",
        status_code=303,
    )


@router.post("/descuentos/activar", dependencies=[Depends(requiere_admin)])
async def activar_plantilla(request: Request, plantilla_id: str = Form("")):
    """
    Pone en marcha la plantilla que el admin eligió en el selector.

    Solo se puede si NO hay ninguna activa: para cambiar de promoción hay
    que desactivar primero. Es un paso de más a propósito, porque esto le
    cambia el precio en el momento a quien tenga el carrito abierto.

    Como `desactivar`, va por envío normal y termina en una recarga.
    """
    try:
        elegida = int(str(plantilla_id).strip())
    except ValueError:
        return _render_lista(request, error="Elige una plantilla para activar.")

    try:
        estado = db.listar_plantillas()
        activa = next((p for p in estado["plantillas"] if p["activa"]), None)
        if activa:
            return _render_lista(request, error=_candado_activa(activa["nombre"]))

        existe = db.activar_plantilla(elegida, str(request.session.get("user") or ""))
    except Exception as e:
        return _render_lista(request, error=_error_bd(e))

    if not existe:
        return _render_lista(request, error="Esa plantilla ya no existe.")

    logicas_descuento.invalidar_cache()
    return RedirectResponse(url="/admin/descuentos?msg=activada", status_code=303)


@router.post("/descuentos/plantillas/{plantilla_id}/duplicar",
             dependencies=[Depends(requiere_admin)])
async def duplicar_plantilla(request: Request, plantilla_id: int):
    """Copia la plantilla con otro nombre, para probar cambios sin arriesgar."""
    try:
        nueva_id = db.duplicar_plantilla(plantilla_id, str(request.session.get("user") or ""))
    except Exception as e:
        return _render_lista(request, error=_error_bd(e))

    if not nueva_id:
        return _render_lista(request, error="Esa plantilla ya no existe.")

    return RedirectResponse(
        url=f"/admin/descuentos/plantillas/{nueva_id}?msg=duplicada",
        status_code=303,
    )


@router.post("/descuentos/plantillas/{plantilla_id}/borrar",
             dependencies=[Depends(requiere_admin)])
async def borrar_plantilla(request: Request, plantilla_id: int):
    """
    Borra una plantilla y sus tramos.

    No se puede borrar la que está activa: primero hay que activar otra. Así
    nunca se queda el carrito sin la plantilla que estaba aplicando por un
    clic de más.
    """
    def _no(mensaje):
        if _pide_json(request):
            return JSONResponse({"ok": False, "error": mensaje})
        return _render_lista(request, error=mensaje)

    try:
        plantilla = db.obtener_plantilla(plantilla_id)
        if not plantilla:
            return _no("Esa plantilla ya no existe.")
        if plantilla["activa"]:
            return _no(
                f"\"{plantilla['nombre']}\" es la plantilla activa. Activa otra "
                f"antes de borrarla."
            )

        db.borrar_plantilla(plantilla_id, str(request.session.get("user") or ""))
    except Exception as e:
        return _no(_error_bd(e))

    logicas_descuento.invalidar_cache()

    if _pide_json(request):
        return JSONResponse({"ok": True, "plantilla_id": plantilla_id,
                             "msg": MENSAJES["borrada"]})

    return RedirectResponse(url="/admin/descuentos?msg=borrada", status_code=303)


# ──────────────────────────────────────────────────────────────────────────
# EDITOR DE UNA PLANTILLA
# ──────────────────────────────────────────────────────────────────────────

def _render_editor(request: Request, *, plantilla, error=None, avisos=None, msg=None):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "admin_plantilla.html",
        {
            "plantilla": plantilla,
            "error": error,
            "avisos": avisos or [],
            "msg": msg,
            "max_tramos": MAX_TRAMOS,
            "porcentaje_max": PORCENTAJE_MAX,
            "min_items_max": MIN_ITEMS_MAX,
            "max_largo_nombre": MAX_LARGO_NOMBRE,
        },
    )


@router.get("/descuentos/plantillas/{plantilla_id}", response_class=HTMLResponse,
            dependencies=[Depends(requiere_admin)])
async def ver_plantilla(request: Request, plantilla_id: int, msg: str = ""):
    try:
        plantilla = db.obtener_plantilla(plantilla_id)
    except Exception as e:
        return _render_lista(request, error=_error_bd(e))

    if not plantilla:
        return _render_lista(request, error="Esa plantilla ya no existe.")

    return _render_editor(request, plantilla=plantilla, msg=MENSAJES.get(msg))


@router.post("/descuentos/plantillas/{plantilla_id}",
             dependencies=[Depends(requiere_admin)])
async def guardar_plantilla(request: Request, plantilla_id: int):
    """
    Guarda nombre, modo acumulativo y tramos. No cambia cuál está activa.

    Se niega si la plantilla está activa: primero hay que desactivarla. Este
    es el candado de verdad, el del servidor; que la pantalla además esconda
    el formulario es solo cortesía.
    """
    formulario = await request.form()

    try:
        actual = db.obtener_plantilla(plantilla_id)
    except Exception as e:
        if _pide_json(request):
            return JSONResponse({"ok": False, "error": _error_bd(e)})
        return _render_lista(request, error=_error_bd(e))

    if actual and actual["activa"]:
        mensaje = _candado_activa(actual["nombre"])
        if _pide_json(request):
            return JSONResponse({"ok": False, "error": mensaje})
        return _render_editor(request, plantilla=actual, error=mensaje)

    acumulativo = str(formulario.get("acumulativo") or "").strip() != ""
    nombre, error_nombre = validar_nombre(formulario.get("nombre"))
    reglas, error_reglas, avisos = validar_reglas(
        formulario.getlist("min_items"),
        formulario.getlist("porcentaje"),
        formulario.getlist("activo"),
    )

    error = error_nombre or error_reglas

    if error and _pide_json(request):
        # El JavaScript conserva lo que el admin tiene escrito; solo necesita
        # saber qué estuvo mal.
        return JSONResponse({"ok": False, "error": error})

    if error:
        # Re-dibujamos con lo que el admin acababa de escribir, para que no
        # tenga que volver a tipearlo. `actual` ya se leyó arriba.
        tecleado = dict(actual or {})
        tecleado.update({
            "nombre": str(formulario.get("nombre") or ""),
            "acumulativo": acumulativo,
            "reglas": [
                {
                    "min_items": m,
                    "porcentaje": p,
                    "activo": str(a).strip() in ("1", "true", "on", "si"),
                }
                for m, p, a in zip(
                    formulario.getlist("min_items"),
                    formulario.getlist("porcentaje"),
                    formulario.getlist("activo"),
                )
            ],
        })
        return _render_editor(request, plantilla=tecleado, error=error)

    try:
        db.guardar_plantilla(
            plantilla_id, nombre, acumulativo, reglas,
            str(request.session.get("user") or ""),
        )
    except Exception as e:
        if _pide_json(request):
            return JSONResponse({"ok": False, "error": _error_bd(e)})
        plantilla = {
            "id": plantilla_id, "nombre": nombre, "acumulativo": acumulativo,
            "activa": False, "reglas": reglas,
        }
        return _render_editor(request, plantilla=plantilla, error=_error_bd(e))

    # Que el cambio se vea YA en este proceso. Si Render corre más de un
    # worker, los demás se ponen al día solos cuando expire su TTL (segundos).
    logicas_descuento.invalidar_cache()

    if _pide_json(request):
        # Devolvemos la plantilla ya guardada: el editor la usa para quedar
        # exactamente igual a lo que hay en la base (y para que "Descartar"
        # vuelva a este estado sin pedirle nada al servidor).
        try:
            guardada = db.obtener_plantilla(plantilla_id)
        except Exception:
            guardada = None
        return JSONResponse({
            "ok": True,
            "msg": MENSAJES["guardada"],
            "avisos": avisos,
            "plantilla": guardada or {
                "id": plantilla_id, "nombre": nombre,
                "acumulativo": acumulativo, "activa": False, "reglas": reglas,
            },
        })

    if avisos:
        # Se guardó, pero hay algo que conviene que vea antes de irse.
        plantilla = db.obtener_plantilla(plantilla_id)
        return _render_editor(request, plantilla=plantilla, avisos=avisos,
                              msg=MENSAJES["guardada"])

    return RedirectResponse(
        url=f"/admin/descuentos/plantillas/{plantilla_id}?msg=guardada",
        status_code=303,
    )
