"""
Generación dinámica de documentos (comprobante de reserva) en memoria.

Flujo:
    datos de la reserva -> Jinja2 (plantilla fija) -> HTML -> xhtml2pdf -> PDF en BytesIO

El PDF NUNCA se guarda en disco ni en la base de datos: se construye en
tiempo real cuando el usuario pide la descarga y se envía directo al navegador.

Se usa xhtml2pdf (pisa) porque es Python puro: no requiere librerías nativas
(GTK/Pango/Cairo), por lo que instala con pip y funciona igual en Windows
(desarrollo local) y en Render (producción) sin configuración extra.
"""

import base64
import os
from datetime import datetime, timezone, timedelta
from io import BytesIO

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from xhtml2pdf import pisa

# Raíz del proyecto (carpeta que contiene `templates/` y `static/`).
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES_DIR = os.path.join(_BASE_DIR, "templates")

# Entorno Jinja2 propio para documentos. La plantilla controla solo el diseño;
# las variables (cliente, items, totales) cambian en cada descarga.
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)


def _format_currency(value):
    """Formatea un entero/representación de precio como '$89.950'."""
    if value is None or value == "":
        return "$0"
    s = str(value).replace("$", "").replace(".", "").replace(",", "").strip()
    if not s.lstrip("-").isdigit():
        return str(value)
    return "${:,}".format(int(s)).replace(",", ".")


_env.filters["currency"] = _format_currency


def _link_callback(uri, rel):
    """
    Resuelve las rutas de recursos (imágenes/CSS) que aparecen en la plantilla
    a rutas absolutas del sistema de archivos, para que xhtml2pdf pueda
    incrustarlas en el PDF. Soporta tanto `/static/...` como rutas relativas.
    """
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri

    path = uri
    if uri.startswith("/static/"):
        path = os.path.join(_BASE_DIR, uri.lstrip("/"))
    elif uri.startswith("static/"):
        path = os.path.join(_BASE_DIR, uri)
    else:
        path = os.path.join(_BASE_DIR, uri.lstrip("/"))

    path = os.path.normpath(path)
    return path if os.path.isfile(path) else uri


# Caché en proceso: misma URL de imagen -> mismo data-URI, para no
# descargarla dos veces si se repite entre artículos o entre descargas.
_IMG_CACHE = {}


def _imagen_a_datauri(url, timeout=8):
    """
    Descarga una imagen remota (http/https) y la devuelve como data-URI
    base64 listo para incrustar en el HTML del PDF. Si la URL está vacía,
    no es remota o falla la descarga, devuelve "" (el PDF se genera sin
    esa imagen, nunca rompe la descarga).
    """
    if not url:
        return ""
    url = str(url).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    if url in _IMG_CACHE:
        return _IMG_CACHE[url]

    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        datauri = "data:%s;base64,%s" % (
            ctype, base64.b64encode(resp.content).decode("ascii")
        )
    except Exception:
        datauri = ""

    _IMG_CACHE[url] = datauri
    return datauri


def generar_comprobante_reserva_pdf(reserva: dict) -> BytesIO:
    """
    Renderiza la plantilla del comprobante con los datos de la reserva y
    devuelve un BytesIO con el PDF listo para enviar al navegador.

    `reserva` es el dict que devuelve `db.obtener_reserva`.
    """
    # Convertir la URL de imagen de cada artículo a un data-URI incrustable.
    # Se hace una copia para no mutar el dict de la reserva original.
    items = []
    for it in reserva.get("items", []):
        it = dict(it)
        it["imagen_datauri"] = _imagen_a_datauri(it.get("imagen", ""))
        items.append(it)

    # Usar la fecha guardada con la reserva (hora de Bogotá) para que el
    # comprobante coincida exactamente con lo almacenado en la BD. Si por
    # alguna razón no viniera, calculamos la hora de Bogotá (UTC-5).
    fecha = reserva.get("fecha")
    if not fecha:
        fecha = datetime.now(timezone(timedelta(hours=-5))).strftime("%d/%m/%Y %I:%M %p")

    template = _env.get_template("comprobante_reserva.html")
    html = template.render(
        reserva=reserva,
        datos=reserva.get("datos_cliente", {}),
        items=items,
        total=reserva.get("total", 0),
        fecha=fecha,
    )

    buffer = BytesIO()
    resultado = pisa.CreatePDF(
        src=html,
        dest=buffer,
        encoding="utf-8",
        link_callback=_link_callback,
    )
    if resultado.err:
        raise RuntimeError("No se pudo generar el PDF del comprobante de reserva.")

    buffer.seek(0)
    return buffer
