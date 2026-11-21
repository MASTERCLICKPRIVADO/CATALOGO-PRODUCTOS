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

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)

from app import db
from app import home
from app import imagen_promocion
from app import logicas_descuento
# `temporada_mod` y no `temporada` porque abajo hay rutas cuyo parámetro se
# llama así; el alias evita que una cosa tape a la otra.
from app import temporada as temporada_mod

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
    "imagen": ("Imagen de promoción reemplazada. Ya se ve en la página de "
               "inicio; si la sigues viendo vieja, recarga."),
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
    if "abre_el" in texto or "cierra_el" in texto:
        return (
            "Falta ejecutar migraciones/migracion_fechas_temporada.sql en "
            "Supabase: la temporada todavía no tiene columnas de fechas."
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
            # Banner de la home: la imagen que hay puesta y, si la subida
            # no está configurada, el motivo, para decirlo antes y no
            # después de que elija el archivo.
            "promo_image_url": imagen_promocion.get_promo_image_url(),
            "promo_no_disponible": imagen_promocion.motivo_no_disponible(),
            "promo_max_mb": imagen_promocion.MAX_BYTES // 1024 // 1024,
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


# ──────────────────────────────────────────────────────────────────────────
# IMAGEN DE PROMOCIÓN
# ──────────────────────────────────────────────────────────────────────────

@router.post("/descuentos/promocion", dependencies=[Depends(requiere_admin)])
async def subir_imagen_promocion(request: Request,
                                 imagen: UploadFile = File(None)):
    """
    Reemplaza el banner de la página de inicio.

    Es UNA sola imagen: la nueva pisa la anterior, no se acumulan. La
    validación de verdad (que sea una imagen, y de qué tipo) se hace
    mirando los bytes en app/imagen_promocion.py, no la extensión ni el
    Content-Type, que los escribe quien sube el archivo.

    Se lee con tope para que un archivo enorme no se cargue entero en
    memoria solo para acabar rechazándolo.
    """
    tope = imagen_promocion.MAX_BYTES + 1
    datos = await imagen.read(tope) if imagen is not None else b""

    tipo, error = imagen_promocion.validar(datos)
    if error:
        return _render_lista(request, error=error)

    ok, error = imagen_promocion.subir(datos, tipo)
    if not ok:
        return _render_lista(request, error=error)

    # La imagen ya está puesta: si la auditoría falla, se avisa por consola
    # pero no se le dice al admin que la subida salió mal, porque salió bien.
    try:
        db.auditar_accion(
            str(request.session.get("user") or ""),
            "imagen_promocion",
            {"archivo": getattr(imagen, "filename", "") or "",
             "tipo": tipo, "bytes": len(datos)},
        )
    except Exception as e:
        print(f"⚠️  No se pudo auditar el cambio de imagen de promoción: {e}")

    return RedirectResponse(url="/admin/descuentos?msg=imagen", status_code=303)


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


# ──────────────────────────────────────────────────────────────────────────
# TEMPORADAS
# ──────────────────────────────────────────────────────────────────────────
# La tienda funciona por temporadas. Aqui el admin sube el catalogo, los
# excluidos y el parrafo de terminos, los revisa, y decide cuando se
# publican y cuando se abre o se cierra la tienda.
#
# SUBIR NO ES PUBLICAR. Cada archivo se guarda aparte (`data_preparada`,
# `excluidos_preparada`, el parrafo en `temporada`) y los clientes siguen
# viendo lo de siempre hasta que el admin le da a publicar.
#
# Todo va por envio normal de formulario, no por fetch: cada accion cambia
# media pantalla, y recargarla una vez sale mas barato que pedir JSON y
# recargar igual despues.

MENSAJES_TEMPORADA = {
    "ref_creado": "Código de referido creado. Ya sirve para iniciar sesión.",
    "ref_guardado": "Código de referido guardado.",
    "ref_borrado": ("Código de referido eliminado. Ya no sirve para iniciar "
                    "sesión."),
    "ref_csv": "Directorio reemplazado con el archivo:",
    "catalogo": "Catálogo cargado. Revísalo abajo y publícalo cuando quieras.",
    "excluidos": "Excluidos cargados. Revísalos abajo y publícalos cuando quieras.",
    "parrafo": "Párrafo guardado. Se publicará con la temporada.",
    "descartado": "Se descartó lo que estaba preparado. Nada cambió para los clientes.",
    "publicada": "Temporada publicada: los clientes ya ven el catálogo nuevo.",
    "abierta": ("Temporada abierta. Los clientes ya pueden ver el catálogo "
                "y reservar; se cerrará sola el último día del rango."),
    "fechas": ("Fechas guardadas. La temporada se cierra sola el último día "
               "del rango nuevo."),
    "cerrada": ("Temporada cerrada. Quien entre verá el aviso; tú sigues "
                "navegando la tienda y entrando al panel."),
}

# Tope de lectura del archivo subido. Se lee con limite para que un archivo
# disparatado no se cargue entero en memoria solo para acabar rechazandolo.
TOPE_LECTURA = temporada_mod.MAX_BYTES + 1


def _render_temporada(request: Request, *, error=None, msg=None, avisos=None,
                      referido_error=None, referido_form=None,
                      referido_csv=None):
    templates = request.app.state.templates

    estado = db.obtener_estado_temporada()
    try:
        resumen = db.resumen_preparado() if estado.get("existe") else {}
    except Exception as e:
        resumen = {}
        error = error or _error_bd(e)

    try:
        catalogo_actual = len(request.app.state.df) if request.app.state.df is not None else 0
    except Exception:
        catalogo_actual = 0

    # Los códigos de referido viven en esta misma pantalla, en su propio
    # bloque al final. Si su consulta falla, el resto de la pantalla sigue
    # sirviendo: el aviso sale dentro de ese bloque y la lista queda vacía,
    # en vez de tumbar la gestión de la temporada.
    referidos, huerfanos, ciudades, referidos_total = [], [], [], 0
    try:
        referidos = db.listar_referidos()
        referidos_total = db.contar_referidos()
        ciudades = db.ciudades_conocidas()
        huerfanos = db.referidos_huerfanos()
    except Exception as e:
        referido_error = referido_error or _error_bd(e)

    return templates.TemplateResponse(
        request,
        "admin_temporada.html",
        {
            "estado": estado,
            # Lo que pasa HOY con ese estado: si el rango de fechas ya venció,
            # la tienda está cerrada aunque `estado.abierta` siga en true. La
            # pantalla se pinta por esto, no por el interruptor a secas.
            "vigencia": temporada_mod.vigencia(estado),
            "hoy": temporada_mod.hoy_bogota(),
            "resumen": resumen,
            "catalogo_en_memoria": catalogo_actual,
            "error": error,
            "msg": msg,
            "avisos": avisos or [],
            "max_mb": temporada_mod.MAX_BYTES // 1024 // 1024,
            "max_parrafo": temporada_mod.MAX_LARGO_PARRAFO,
            "parrafo_publicado": (db.obtener_promocion_activa() or {}).get("descripcion", ""),
            # Códigos de referido (ver la sección REFERIDOS más abajo).
            "referidos": referidos,
            "referidos_total": referidos_total,
            "referidos_tope": db.MAX_REFERIDOS,
            "referidos_huerfanos": huerfanos,
            "ciudades": ciudades,
            "referido_error": referido_error,
            "referido_form": referido_form or {},
            # Cuando hay un CSV subido y sin confirmar, esto trae la
            # comparación con lo que hay. Vacío el resto del tiempo.
            "referido_csv": referido_csv,
            "max_largo_codigo": MAX_LARGO_CODIGO,
            "max_largo_empleado": MAX_LARGO_EMPLEADO,
            "max_kb_directorio": MAX_BYTES_DIRECTORIO // 1024,
            "max_usuarios_por_codigo": db.MAX_USUARIOS_POR_CODIGO,
        },
    )


@router.get("/temporada", response_class=HTMLResponse,
            dependencies=[Depends(requiere_admin)])
async def ver_temporada(request: Request, msg: str = "", error: str = "",
                        ren: int = 0, reub: int = 0, sin: int = 0,
                        alt: int = 0, baj: int = 0, act: int = 0):
    """
    Pinta la pantalla de temporada (y, al final, los códigos de referido).

    Los números son los que dejó el POST de un referido en el redirect:
    cuántas cuentas se renombraron (`ren`), cambiaron de ciudad (`reub`) o
    quedaron sin código (`sin`), y en el reemplazo por CSV cuántos códigos
    entraron (`alt`), salieron (`baj`) o se actualizaron (`act`). Viajan
    como enteros, no como texto, para que el mensaje lo arme el servidor y
    no se pueda meter nada por la URL.
    """
    return _render_temporada(
        request,
        msg=_mensaje_temporada(msg, ren, reub, sin, alt, baj, act),
        error=error or None,
    )


async def _leer_subida(archivo):
    """Lee el archivo subido con tope. Devuelve `(datos, error)`."""
    if archivo is None:
        return None, "No llegó ningún archivo. Elige un CSV."
    datos = await archivo.read(TOPE_LECTURA)
    return datos, None


@router.post("/temporada/catalogo", dependencies=[Depends(requiere_admin)])
async def subir_catalogo(request: Request, archivo: UploadFile = File(None)):
    """
    Guarda el catálogo subido como PREPARADO. No toca lo que ven los
    clientes: eso solo pasa al publicar.
    """
    datos, error = await _leer_subida(archivo)
    if error:
        return _render_temporada(request, error=error)

    df, error = temporada_mod.leer_csv(datos)
    if error:
        return _render_temporada(request, error=error)

    listo, error, avisos = temporada_mod.validar_data(df)
    if error:
        return _render_temporada(request, error=error, avisos=avisos)

    try:
        db.preparar_catalogo(listo, str(request.session.get("user") or ""))
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e), avisos=avisos)

    if avisos:
        return _render_temporada(request, msg=MENSAJES_TEMPORADA["catalogo"],
                                 avisos=avisos)
    return RedirectResponse(url="/admin/temporada?msg=catalogo", status_code=303)


@router.post("/temporada/excluidos", dependencies=[Depends(requiere_admin)])
async def subir_excluidos(request: Request, archivo: UploadFile = File(None)):
    """Igual que el catálogo, para las referencias excluidas de la promoción."""
    datos, error = await _leer_subida(archivo)
    if error:
        return _render_temporada(request, error=error)

    df, error = temporada_mod.leer_csv(datos)
    if error:
        return _render_temporada(request, error=error)

    listo, error, avisos = temporada_mod.validar_excluidos(df)
    if error:
        return _render_temporada(request, error=error, avisos=avisos)

    try:
        db.preparar_excluidos(listo, str(request.session.get("user") or ""))
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e), avisos=avisos)

    if avisos:
        return _render_temporada(request, msg=MENSAJES_TEMPORADA["excluidos"],
                                 avisos=avisos)
    return RedirectResponse(url="/admin/temporada?msg=excluidos", status_code=303)


@router.post("/temporada/parrafo", dependencies=[Depends(requiere_admin)])
async def guardar_parrafo(request: Request, parrafo: str = Form("")):
    """Guarda el párrafo de términos preparado. Tampoco publica."""
    limpio, error = temporada_mod.validar_parrafo(parrafo)
    if error:
        return _render_temporada(request, error=error)

    try:
        db.preparar_parrafo(limpio, str(request.session.get("user") or ""))
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e))

    return RedirectResponse(url="/admin/temporada?msg=parrafo", status_code=303)


@router.post("/temporada/descartar", dependencies=[Depends(requiere_admin)])
async def descartar(request: Request, que: str = Form("")):
    """Tira una de las piezas preparadas. No toca nada publicado."""
    try:
        if not db.descartar_preparado(str(que).strip(),
                                      str(request.session.get("user") or "")):
            return _render_temporada(request, error="No sé qué quieres descartar.")
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e))

    return RedirectResponse(url="/admin/temporada?msg=descartado", status_code=303)


@router.post("/temporada/publicar", dependencies=[Depends(requiere_admin)])
async def publicar(request: Request):
    """
    Vuelca lo preparado sobre las tablas de verdad. Es el momento en que
    cambia lo que ven los clientes.

    El archivo MANDA sobre el inventario: el stock queda como diga el
    catálogo subido, aunque se hayan reservado unidades desde que se
    exportó. Por eso la pantalla pide confirmación antes de llegar aquí.
    """
    try:
        hecho = db.publicar_temporada(str(request.session.get("user") or ""))
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e))

    if not any(hecho.values()):
        return _render_temporada(request, error="No hay nada preparado que publicar.")

    # Que este proceso lo vea ya, sin esperar al TTL; los demás se enteran
    # solos por el número de versión.
    temporada_mod.invalidar_cache()
    temporada_mod.asegurar_catalogo(request.app)

    return RedirectResponse(url="/admin/temporada?msg=publicada", status_code=303)


@router.post("/temporada/abrir", dependencies=[Depends(requiere_admin)])
async def abrir(request: Request, desde: str = Form(""), hasta: str = Form("")):
    """
    Abre la tienda con un rango de fechas obligatorio.

    Las dos fechas son el punto de todo esto: con ellas la temporada se
    cierra sola el último día y nadie tiene que acordarse de entrar a
    cerrarla. Por eso abrir sin fechas no es una opción.
    """
    usuario = str(request.session.get("user") or "")

    # Si falta la migración de fechas, la pantalla ya enseñó el botón de
    # siempre (sin rango) y lo que no puede pasar es que la tienda se quede
    # sin poder abrirse por una migración pendiente.
    try:
        if not db.obtener_estado_temporada().get("fechas_ok", True):
            db.abrir_temporada_sin_fechas(usuario)
            temporada_mod.invalidar_cache()
            return RedirectResponse(url="/admin/temporada?msg=abierta",
                                    status_code=303)
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e))

    desde_f, hasta_f, error = temporada_mod.validar_rango(desde, hasta)
    if error:
        return _render_temporada(request, error=error)

    try:
        db.abrir_temporada(usuario, desde_f, hasta_f)
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e))

    temporada_mod.invalidar_cache()
    return RedirectResponse(url="/admin/temporada?msg=abierta", status_code=303)


@router.post("/temporada/fechas", dependencies=[Depends(requiere_admin)])
async def cambiar_fechas(request: Request, desde: str = Form(""),
                         hasta: str = Form("")):
    """
    Alarga, recorta o adelanta una temporada que ya está abierta.

    Sin esto, estirar una campaña un día más obligaría a cerrarla y volver a
    abrirla, que es la vuelta que queríamos quitar.
    """
    desde_f, hasta_f, error = temporada_mod.validar_rango(desde, hasta)
    if error:
        return _render_temporada(request, error=error)

    try:
        db.cambiar_fechas_temporada(str(request.session.get("user") or ""),
                                    desde_f, hasta_f)
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e))

    temporada_mod.invalidar_cache()
    return RedirectResponse(url="/admin/temporada?msg=fechas", status_code=303)


@router.post("/temporada/cerrar", dependencies=[Depends(requiere_admin)])
async def cerrar(request: Request):
    try:
        db.cerrar_temporada(str(request.session.get("user") or ""))
    except Exception as e:
        return _render_temporada(request, error=_error_bd(e))

    temporada_mod.invalidar_cache()
    return RedirectResponse(url="/admin/temporada?msg=cerrada", status_code=303)


# ──────────────────────────────────────────────────────────────────────────
# CÓDIGOS DE REFERIDO
# ──────────────────────────────────────────────────────────────────────────
# El directorio de empleados (`directorio_empleados`) es la tabla de códigos
# de referido: el código que el cliente teclea en /login y /registro se valida
# contra ella y su CIUDAD es la que decide qué inventario verá. Hasta ahora
# solo se podía tocar entrando a Supabase; esto es la misma tabla, editable
# desde el panel.
#
# POR QUÉ VIVE DENTRO DE /admin/temporada
# ---------------------------------------
# Es la pantalla donde ya se administra la data de la página (catálogo,
# excluidos, párrafo). No se abrió una pestaña aparte a propósito: es un
# bloque más de esa misma pantalla, con su misma navegación y sus mismos
# estilos.
#
# PERO AQUÍ NO HAY «PREPARAR Y PUBLICAR»
# --------------------------------------
# El resto de esa pantalla guarda aparte y solo cambia la tienda al publicar.
# Los referidos NO: lo que se guarda aquí vale desde ese momento, porque es
# el directorio contra el que se valida cada login. La pantalla lo dice en
# letra junto al bloque, para que no se confunda con los archivos de arriba.
#
# QUÉ SE ARRASTRA AL EDITAR
# -------------------------
# Lo decide db.actualizar_referido (ahí está el detalle): renombrar un código
# lo reescribe en las cuentas que lo tenían, y cambiarle la ciudad reescribe
# la ciudad de esas cuentas. Las reservas ya hechas no se tocan nunca: son el
# histórico de con qué código se vendió.
#
# SEGURIDAD
# ---------
# Las rutas cuelgan de este mismo router, así que pasan por `requiere_admin`
# igual que todo lo demás: el permiso se lee de Postgres en cada petición, no
# de la cookie. Las consultas van parametrizadas (nunca se compone el SQL con
# lo que llegó del formulario) y el código se valida contra una lista blanca
# de caracteres antes de tocar la BD. Las claves de Supabase siguen solo en
# el servidor: la pantalla no habla con la BD, habla con estas rutas.

MIN_LARGO_CODIGO = 2
MAX_LARGO_CODIGO = 30
MAX_LARGO_EMPLEADO = 80

# Lista blanca de lo que puede llevar un código: letras, números, punto,
# guion y guion bajo. El directorio ya tiene códigos con punto
# ('jr.cardenas') y no hace falta más. Sin espacios: el login hace TRIM en
# los extremos, pero uno en el medio sería un código imposible de teclear
# dos veces igual.
PATRON_CODIGO = re.compile(r"[A-Za-z0-9._-]+")


def _cuentas(n: int) -> str:
    """«1 cuenta» / «7 cuentas», para no escribir «1 cuenta(s)»."""
    return "1 cuenta" if int(n) == 1 else f"{int(n)} cuentas"


def _mensaje_temporada(clave: str, ren: int = 0, reub: int = 0, sin: int = 0,
                       alt: int = 0, baj: int = 0, act: int = 0):
    """
    El mensaje verde de la pantalla de temporada.

    Para los referidos se le pegan las consecuencias que tuvo el cambio
    (cuántas cuentas se renombraron, cuántas cambiaron de ciudad, cuántas
    quedaron sin código), que es justo lo que el admin no puede ver solo
    mirando la fila que acaba de guardar.
    """
    base = MENSAJES_TEMPORADA.get(clave)
    if not base:
        return None

    extras = []
    if clave == "ref_csv":
        # Se cuenta en sustantivos («2 códigos nuevos») y no en frases con
        # verbo, para que valga igual en singular y en plural.
        movimientos = []
        if alt:
            movimientos.append(_plural(alt, "código nuevo", "códigos nuevos"))
        if baj:
            movimientos.append(_plural(baj, "código dado de baja",
                                       "códigos dados de baja"))
        if act:
            movimientos.append(_plural(act, "código actualizado",
                                       "códigos actualizados"))
        extras.append(", ".join(movimientos) + "." if movimientos
                      else "el archivo decía exactamente lo que ya había.")
        if sin:
            extras.append(_plural(sin, "cuenta quedó", "cuentas quedaron") +
                          " sin código, por los que se dieron de baja.")
        if reub:
            extras.append(_plural(reub, "cuenta cambió", "cuentas cambiaron") +
                          " de ciudad.")
    elif clave == "ref_guardado":
        if ren:
            extras.append(f"El código nuevo quedó en {_cuentas(ren)}.")
        if reub == 1:
            extras.append("1 cuenta pasó a la ciudad nueva y verá ese "
                          "catálogo en su próximo inicio de sesión.")
        elif reub:
            extras.append(f"{reub} cuentas pasaron a la ciudad nueva y verán "
                          f"ese catálogo en su próximo inicio de sesión.")
        if not ren and not reub:
            extras.append("Ninguna cuenta se vio afectada.")
    elif clave == "ref_borrado" and sin:
        if sin == 1:
            extras.append("1 cuenta quedó sin código: tendrá que ingresar uno "
                          "válido la próxima vez que entre.")
        else:
            extras.append(f"{sin} cuentas quedaron sin código: tendrán que "
                          f"ingresar uno válido la próxima vez que entren.")

    return " ".join([base] + extras)


def validar_referido(codigo, nombre, ciudad, ciudades):
    """
    Limpia y comprueba lo que llegó del formulario.

    Devuelve `(codigo, nombre, ciudad, error)`. El error es un mensaje para
    el admin; si viene, no se toca la base de datos.

    La ciudad tiene que ser una de las que existen de verdad
    (`db.ciudades_conocidas`): es la que filtra el catálogo, así que una mal
    escrita no da ningún error visible, solo deja al cliente mirando una
    tienda vacía.
    """
    codigo = str(codigo or "").strip()
    nombre = str(nombre or "").strip()
    ciudad = str(ciudad or "").strip()

    if not codigo:
        return codigo, nombre, ciudad, "El referido necesita un código."
    if len(codigo) < MIN_LARGO_CODIGO:
        return codigo, nombre, ciudad, (
            f"El código es muy corto: necesita al menos {MIN_LARGO_CODIGO} "
            f"caracteres."
        )
    if len(codigo) > MAX_LARGO_CODIGO:
        return codigo, nombre, ciudad, (
            f"El código no puede pasar de {MAX_LARGO_CODIGO} caracteres."
        )
    if not PATRON_CODIGO.fullmatch(codigo):
        return codigo, nombre, ciudad, (
            "El código solo puede llevar letras, números, punto, guion y "
            "guion bajo. Sin espacios ni tildes: es lo que el cliente teclea "
            "al iniciar sesión."
        )

    if not nombre:
        return codigo, nombre, ciudad, "Falta el nombre de la persona dueña del código."
    if len(nombre) > MAX_LARGO_EMPLEADO:
        return codigo, nombre, ciudad, (
            f"El nombre no puede pasar de {MAX_LARGO_EMPLEADO} caracteres."
        )

    if not ciudad:
        return codigo, nombre, ciudad, (
            "Elige la ciudad del código: es la que decide qué inventario ve "
            "quien entra con él."
        )
    if ciudades and ciudad not in ciudades:
        return codigo, nombre, ciudad, (
            f"«{ciudad}» no es una de las ciudades que existen hoy "
            f"({', '.join(ciudades)}). Si es una ciudad nueva, publícala "
            f"primero en el catálogo."
        )

    return codigo, nombre, ciudad, None


@router.post("/referidos/guardar", dependencies=[Depends(requiere_admin)])
async def guardar_referido(request: Request, codigo_original: str = Form(""),
                           codigo: str = Form(""), nombre: str = Form(""),
                           ciudad: str = Form("")):
    """
    Crea un código o guarda los cambios de uno que ya existe.

    Una sola ruta para las dos cosas: `codigo_original` vacío significa
    «nuevo». Así el formulario de arriba y el de cada fila son el mismo
    formulario, con las mismas validaciones, y no hay dos caminos que se
    puedan ir separando con el tiempo.

    Va por envío normal de formulario (no fetch), como el resto de esta
    pantalla: cada cambio recarga y se vuelve a leer el directorio.
    """
    original = str(codigo_original or "").strip()
    tecleado = {"codigo": str(codigo or "").strip(),
                "nombre": str(nombre or "").strip(),
                "ciudad": str(ciudad or "").strip()}

    def _no(mensaje):
        # Al fallar un código NUEVO se devuelve lo tecleado, para no hacerle
        # escribirlo otra vez. Al fallar la edición de una fila no hace falta:
        # la fila se vuelve a pintar con lo que hay en la base.
        return _render_temporada(
            request,
            referido_error=mensaje,
            referido_form=None if original else tecleado,
        )

    try:
        ciudades = db.ciudades_conocidas()
    except Exception as e:
        return _no(_error_bd(e))

    codigo, nombre, ciudad, error = validar_referido(codigo, nombre, ciudad, ciudades)
    if error:
        return _no(error)

    usuario = str(request.session.get("user") or "")

    try:
        if original:
            resultado = db.actualizar_referido(original, codigo, nombre, ciudad, usuario)
        else:
            resultado = db.crear_referido(codigo, nombre, ciudad, usuario)
    except Exception as e:
        return _no(_error_bd(e))

    if not resultado.get("ok"):
        motivo = resultado.get("motivo")
        if motivo == "duplicado":
            return _no(
                f"Ya hay un código «{codigo}» en el directorio. Los códigos no "
                f"distinguen mayúsculas, así que no puede haber dos que solo "
                f"se diferencien en eso."
            )
        return _no("Ese código ya no está en el directorio. Recarga la pantalla.")

    if not original:
        return RedirectResponse(url="/admin/temporada?msg=ref_creado#referidos",
                                status_code=303)

    return RedirectResponse(
        url=(f"/admin/temporada?msg=ref_guardado"
             f"&ren={int(resultado.get('recodificados') or 0)}"
             f"&reub={int(resultado.get('reubicados') or 0)}#referidos"),
        status_code=303,
    )


@router.post("/referidos/borrar", dependencies=[Depends(requiere_admin)])
async def borrar_referido(request: Request, codigo: str = Form("")):
    """
    Saca un código del directorio.

    Las cuentas que lo tuvieran quedan sin código (tendrán que ingresar uno
    válido en su próximo inicio de sesión) y las reservas ya hechas se
    quedan como están. La pantalla lo pregunta diciendo a cuántas cuentas
    afecta antes de llegar aquí.
    """
    elegido = str(codigo or "").strip()
    if not elegido:
        return _render_temporada(request, referido_error="No sé qué código quieres borrar.")

    try:
        resultado = db.borrar_referido(elegido, str(request.session.get("user") or ""))
    except Exception as e:
        return _render_temporada(request, referido_error=_error_bd(e))

    if not resultado.get("ok"):
        return _render_temporada(
            request,
            referido_error="Ese código ya no está en el directorio. Recarga la pantalla.",
        )

    return RedirectResponse(
        url=(f"/admin/temporada?msg=ref_borrado"
             f"&sin={int(resultado.get('usuarios') or 0)}#referidos"),
        status_code=303,
    )


# ──────────────────────────────────────────────────────────────────────────
# REEMPLAZAR EL DIRECTORIO CON UN CSV
# ──────────────────────────────────────────────────────────────────────────
# Lo mismo que se hace con el catálogo, para los códigos: en vez de agregar
# y quitar de uno en uno, se sube un archivo y manda el archivo.
#
# SUBIR NO ES REEMPLAZAR
# ----------------------
# El archivo se lee, se valida y se ENSEÑA lo que cambiaría; no se escribe
# nada hasta que el admin confirma. Es el mismo paso de más que tiene
# publicar la temporada, y por el mismo motivo: aquí se pueden borrar de un
# golpe los 43 códigos con los que entra la gente.
#
# POR QUÉ EL ARCHIVO VIAJA DOS VECES
# ----------------------------------
# Entre revisar y confirmar hay que guardar en alguna parte el archivo ya
# validado. En vez de crear una tabla de preparación (como `data_preparada`)
# para unas pocas decenas de filas, el CSV normalizado vuelve al navegador
# en un campo oculto y regresa al confirmar. Al confirmar se vuelve a leer y
# a validar entero: lo que llega del formulario nunca se da por bueno.
#
# EL CSV
# ------
# Tres columnas: codigo, nombre, ciudad. Son las mismas que descarga el
# botón de «Descargar el directorio», así que la ida y la vuelta encajan:
# se descarga, se edita en Excel y se sube.

COLUMNAS_DIRECTORIO = ("codigo", "nombre", "ciudad")

# Cómo se llaman esas columnas cuando el archivo no viene del botón de
# descargar (un Excel hecho a mano, una exportación de Supabase con otro
# nombre). `leer_csv` ya dejó las cabeceras en minúsculas y sin espacios.
ALIAS_DIRECTORIO = {
    "código": "codigo",
    "codigo_referido": "codigo",
    "código_referido": "codigo",
    "codigo referido": "codigo",
    "referido": "codigo",
    "nombres": "nombre",
    "nombre completo": "nombre",
    "empleado": "nombre",
    "vendedor": "nombre",
    "sede": "ciudad",
}

# El directorio son unas decenas de filas: con 1 MB sobra de largo. El tope
# está para no cargar en memoria un archivo disparatado solo para acabar
# diciéndole que no.
MAX_BYTES_DIRECTORIO = 1024 * 1024


def _plural(n: int, singular: str, plural: str) -> str:
    """«1 código nuevo» / «3 códigos nuevos»."""
    return f"1 {singular}" if int(n) == 1 else f"{int(n)} {plural}"


def validar_directorio(df, ciudades):
    """
    Revisa el CSV del directorio. Devuelve `(filas, error, avisos)`.

    `filas` son dicts {codigo, nombre, ciudad} ya limpios. Cada fila pasa
    por `validar_referido`, el mismo que usa el formulario de una sola
    fila: una cosa no puede ser válida subiéndola en lote e inválida
    tecleándola.

    El `error` bloquea; los `avisos` no. Los números de fila que se citan
    son los del archivo tal como lo ve el admin en Excel (la cabecera es
    la 1).
    """
    avisos = []

    # Cabeceras: se traducen los nombres alternativos a los tres de siempre.
    columnas = {}
    for c in df.columns:
        columnas.setdefault(ALIAS_DIRECTORIO.get(c, c), c)

    faltan = [c for c in COLUMNAS_DIRECTORIO if c not in columnas]
    if faltan:
        return None, (
            "Al archivo le faltan columnas: " + ", ".join(faltan) +
            ". Tiene que traer codigo, nombre y ciudad, y la primera fila "
            "deben ser los nombres de las columnas. Lo más fácil es "
            "descargar el directorio actual y editar ese mismo archivo."
        ), avisos

    sobran = [c for c in df.columns if c not in columnas.values()]
    if sobran:
        avisos.append("Se ignoran columnas que el directorio no usa: "
                      + ", ".join(sobran[:6]) + ("..." if len(sobran) > 6 else ""))

    filas = []
    vistos = {}
    vacias = 0

    for i, registro in enumerate(df.to_dict("records")):
        numero = i + 2      # +2: la cabecera es la fila 1

        codigo = str(registro.get(columnas["codigo"], "") or "").strip()
        nombre = str(registro.get(columnas["nombre"], "") or "").strip()
        ciudad = str(registro.get(columnas["ciudad"], "") or "").strip()

        # Fila del todo vacía: Excel las deja al final y no significan nada.
        if not codigo and not nombre and not ciudad:
            vacias += 1
            continue

        codigo, nombre, ciudad, error = validar_referido(codigo, nombre, ciudad, ciudades)
        if error:
            return None, f"Fila {numero} del archivo: {error}", avisos

        clave = codigo.lower()
        if clave in vistos:
            return None, (
                f"El código «{codigo}» está dos veces en el archivo "
                f"(filas {vistos[clave]} y {numero}). Los códigos no "
                f"distinguen mayúsculas, así que no puede repetirse. Deja "
                f"una sola fila por persona."
            ), avisos
        vistos[clave] = numero

        filas.append({"codigo": codigo, "nombre": nombre, "ciudad": ciudad})

    if not filas:
        return None, (
            "El archivo no trae ningún código. El directorio no se vacía "
            "desde aquí: si hay que quitarlos todos, bórralos uno por uno."
        ), avisos

    if len(filas) > db.MAX_REFERIDOS:
        return None, (f"El archivo trae {len(filas)} códigos y el máximo son "
                      f"{db.MAX_REFERIDOS}."), avisos

    if vacias:
        avisos.append(f"Se saltaron {vacias} filas vacías del final del archivo.")

    return filas, None, avisos


def _a_csv(filas) -> str:
    """Las filas validadas, otra vez como CSV, para el campo oculto."""
    salida = io.StringIO()
    escritor = csv.writer(salida, lineterminator="\n")
    escritor.writerow(["codigo", "nombre", "ciudad"])
    for f in filas:
        escritor.writerow([f["codigo"], f["nombre"], f["ciudad"]])
    return salida.getvalue()


def _leer_directorio(datos: bytes, request: Request):
    """
    Bytes -> filas validadas. Devuelve `(filas, avisos, respuesta_de_error)`.

    Si algo falla, la tercera es la pantalla ya pintada con el aviso y las
    otras dos vienen vacías; quien llama solo tiene que devolverla.
    """
    def _no(mensaje):
        return None, [], _render_temporada(request, referido_error=mensaje)

    if len(datos) > MAX_BYTES_DIRECTORIO:
        return _no(f"El archivo supera {MAX_BYTES_DIRECTORIO // 1024} KB. "
                   f"El directorio son unas decenas de filas: revisa que sea "
                   f"el archivo correcto.")

    df, error = temporada_mod.leer_csv(datos)
    if error:
        return _no(error)

    try:
        ciudades = db.ciudades_conocidas()
    except Exception as e:
        return _no(_error_bd(e))

    filas, error, avisos = validar_directorio(df, ciudades)
    if error:
        return _no(error)

    return filas, avisos, None


@router.get("/referidos/csv", dependencies=[Depends(requiere_admin)])
async def descargar_directorio(request: Request):
    """
    El directorio actual en CSV.

    Es el punto de partida para reemplazarlo: se descarga, se edita en
    Excel y se vuelve a subir. Se manda con BOM (`utf-8-sig`) porque es lo
    que hace que Excel abra las tildes bien en vez de en jeroglífico.
    """
    try:
        contenido = db.exportar_directorio_csv()
    except Exception as e:
        return _render_temporada(request, referido_error=_error_bd(e))

    return Response(
        content=contenido.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="codigos_referido.csv"'},
    )


@router.post("/referidos/csv", dependencies=[Depends(requiere_admin)])
async def revisar_directorio_csv(request: Request, archivo: UploadFile = File(None)):
    """
    Lee el archivo y enseña qué cambiaría. NO escribe nada.

    El siguiente paso (confirmar) es otro clic y otra ruta, a propósito.
    """
    if archivo is None:
        return _render_temporada(
            request, referido_error="No llegó ningún archivo. Elige un CSV.")

    datos = await archivo.read(MAX_BYTES_DIRECTORIO + 1)

    filas, avisos, fallo = _leer_directorio(datos, request)
    if fallo is not None:
        return fallo

    try:
        comparacion = db.comparar_directorio(filas)
    except Exception as e:
        return _render_temporada(request, referido_error=_error_bd(e))

    return _render_temporada(request, referido_csv={
        "archivo": getattr(archivo, "filename", "") or "el archivo",
        "comparacion": comparacion,
        "avisos": avisos,
        "contenido": _a_csv(filas),
    })


@router.post("/referidos/csv/confirmar", dependencies=[Depends(requiere_admin)])
async def confirmar_directorio_csv(request: Request, contenido: str = Form("")):
    """
    Aplica el archivo que se acaba de revisar, en una sola transacción.

    El CSV vuelve del campo oculto y se valida otra vez entero, como si
    fuera la primera: entre la revisión y este clic pudo cambiar cualquier
    cosa, y lo que llega de un formulario no se da por bueno nunca.
    """
    filas, _, fallo = _leer_directorio(str(contenido or "").encode("utf-8"), request)
    if fallo is not None:
        return fallo

    try:
        hecho = db.reemplazar_directorio(filas, str(request.session.get("user") or ""))
    except Exception as e:
        return _render_temporada(request, referido_error=_error_bd(e))

    return RedirectResponse(
        url=(f"/admin/temporada?msg=ref_csv"
             f"&alt={hecho['altas']}&baj={hecho['bajas']}"
             f"&act={hecho['actualizados']}"
             f"&sin={hecho['cuentas_sin_codigo']}"
             f"&reub={hecho['cuentas_reubicadas']}#referidos"),
        status_code=303,
    )


# ──────────────────────────────────────────────────────────────────────────
# RESERVAS
# ──────────────────────────────────────────────────────────────────────────
# La tercera pestaña del panel: ver las ventas sin abrir la base de datos.
#
# AQUÍ NO SE TOCA NADA
# --------------------
# Esta pantalla SOLO LEE. No crea, no edita y no borra reservas: las sigue
# haciendo app/cart.py al confirmar el carrito, igual que siempre. Por eso
# todas sus rutas son GET y no hay ni un POST en esta sección.
#
# DE DÓNDE SALEN LOS DATOS
# ------------------------
# De `reservas_family` y `reservas_tiendas` en Postgres, que es la única
# copia que existe (no hay ningún CSV de reservas en el proyecto). Las dos
# se leen juntas; cada fila es un artículo y todas las de una reserva
# comparten `reserva_id`. El detalle está en app/db.py, sección «RESERVAS:
# CONSULTA PARA EL PANEL».
#
# POR QUÉ FILTRA Y PAGINA EL SERVIDOR Y NO LA PANTALLA
# ----------------------------------------------------
# Hoy son 64 reservas y cabrían de sobra en la página, pero esto crece con
# cada venta. Filtrar, ordenar, sumar y paginar en SQL cuesta lo mismo con
# 64 que con 60.000, y la pantalla nunca recibe más de las que enseña.
# Además, así los totales de arriba son los de TODO lo filtrado, no los de
# la página que se está viendo.

# Lo que puede pedir la URL. Se valida todo: el tamaño de página y el orden
# se comprueban contra las listas de db.py y lo demás va parametrizado.
FILTROS_RESERVAS = ("q", "desde", "hasta", "ciudad", "origen", "referido")


def _fecha_filtro(texto: str, dias: int = 0):
    """
    Pasa un «2026-11-20» del formulario a la marca de tiempo con la que se
    compara en la base.

    Las fechas de las reservas están guardadas con la hora de pared de
    Bogotá etiquetada como UTC, así que los límites del filtro se
    construyen igual. `dias=1` sirve para el «hasta»: el día elegido tiene
    que entrar entero, así que el corte es la medianoche del día siguiente.
    """
    texto = str(texto or "").strip()
    if not texto:
        return None
    try:
        dia = datetime.strptime(texto, "%Y-%m-%d")
    except ValueError:
        return None
    return dia.replace(tzinfo=timezone.utc) + timedelta(days=dias)


def _filtros_reservas_de_url(request: Request) -> dict:
    """
    Los filtros tal como vinieron en la URL, sin interpretar.

    Sirven para tres cosas: volver a pintar el formulario como lo dejó el
    admin, construir los enlaces de las páginas sin perder lo filtrado, y
    (ya convertidos) consultar la base.
    """
    return {clave: str(request.query_params.get(clave) or "").strip()
            for clave in FILTROS_RESERVAS}


def _consulta_reservas(crudos: dict) -> dict:
    """De los filtros de la URL a los que entiende app/db.py."""
    return {
        "q": crudos["q"],
        "desde": _fecha_filtro(crudos["desde"]),
        "hasta": _fecha_filtro(crudos["hasta"], dias=1),
        "ciudad": crudos["ciudad"],
        "origen": crudos["origen"],
        "referido": crudos["referido"],
    }


def _entero(valor, por_defecto: int, minimo: int = 1) -> int:
    """
    Un número de la URL, o el de siempre si lo que llegó no lo era.

    `pagina` y `por` se reciben como texto a propósito. Declarándolos como
    int, FastAPI contesta un 422 a `?por=abc`, y un enlace viejo o un
    retoque a mano en la barra de direcciones dejaría al admin mirando un
    error en vez de su pantalla.
    """
    try:
        numero = int(str(valor).strip())
    except (TypeError, ValueError):
        return por_defecto
    return max(minimo, numero)


def _enlace_base(crudos: dict, orden: str, por: int) -> str:
    """
    La parte de la URL que comparten los enlaces de paginación y el de
    descargar: todo lo filtrado menos el número de página.
    """
    partes = {k: v for k, v in crudos.items() if v}
    partes["orden"] = orden
    partes["por"] = str(por)
    return urlencode(partes)


@router.get("/reservas", response_class=HTMLResponse,
            dependencies=[Depends(requiere_admin)])
async def ver_reservas(request: Request, orden: str = db.ORDEN_RESERVAS_POR_DEFECTO,
                       pagina: str = "1", por: str = "25"):
    """
    La lista de reservas, con sus filtros, su orden y su paginación.

    Los totales de arriba se calculan sobre TODO lo que cumple el filtro,
    no sobre la página visible: si no, cambiar de página cambiaría las
    ventas del mes.
    """
    templates = request.app.state.templates

    crudos = _filtros_reservas_de_url(request)
    filtros = _consulta_reservas(crudos)
    if orden not in db.ORDENES_RESERVAS:
        orden = db.ORDEN_RESERVAS_POR_DEFECTO

    error = None
    resumen = {"reservas": 0, "unidades": 0, "valor": 0, "reservas_7dias": 0,
               "valor_7dias": 0, "primera": None, "ultima": None}
    pagina_datos = {"reservas": [], "total": 0, "pagina": 1, "paginas": 1,
                    "por_pagina": 25}
    ciudades = []

    try:
        resumen = db.resumen_reservas(filtros)
        pagina_datos = db.listar_reservas(
            filtros, orden=orden,
            pagina=_entero(pagina, 1),
            por_pagina=_entero(por, db.TAMANOS_PAGINA[0]),
        )
        ciudades = db.ciudades_de_reservas()
    except Exception as e:
        error = _error_bd(e)

    return templates.TemplateResponse(
        request,
        "admin_reservas.html",
        {
            "resumen": resumen,
            "pagina": pagina_datos,
            "ciudades": ciudades,
            "filtros": crudos,
            "hay_filtros": any(crudos.values()),
            "orden": orden,
            "tamanos": db.TAMANOS_PAGINA,
            "enlace_base": _enlace_base(crudos, orden, pagina_datos["por_pagina"]),
            "error": error,
        },
    )


@router.get("/reservas/csv", dependencies=[Depends(requiere_admin)])
async def descargar_reservas_csv(request: Request,
                                 orden: str = db.ORDEN_RESERVAS_POR_DEFECTO):
    """
    Lo que se está viendo, en CSV y con una fila por artículo.

    Respeta los filtros de la pantalla y se salta la paginación: se lleva
    todo lo que cumple el filtro, no la página.
    """
    filtros = _consulta_reservas(_filtros_reservas_de_url(request))
    if orden not in db.ORDENES_RESERVAS:
        orden = db.ORDEN_RESERVAS_POR_DEFECTO

    try:
        contenido = db.exportar_reservas_csv(filtros, orden=orden)
    except Exception as e:
        raise HTTPException(status_code=500, detail=_error_bd(e))

    hoy = db.hora_bogota_para_guardar().strftime("%Y-%m-%d")
    return Response(
        content=contenido.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="reservas_{hoy}.csv"'},
    )
