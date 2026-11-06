"""
LA IMAGEN DE PROMOCIÓN  —  leerla y reemplazarla
================================================

El banner que sale en la home vive en Supabase Storage, en un bucket
público, SIEMPRE con el mismo nombre de archivo. Antes había que subirlo a
mano desde el panel de Supabase; ahora el admin lo reemplaza desde
/admin/descuentos.

Es UNA sola imagen: subir una nueva PISA la anterior. No hay galería ni
historial, a propósito — la home muestra un único banner y tener varios
archivos sueltos solo invita a equivocarse de cuál es el bueno.

CÓMO SE REFRESCA
----------------
El nombre del archivo no cambia, así que la URL tampoco. Para que el
navegador no siga mostrando la imagen vieja, `get_promo_image_url` le pega
un `?t=<timestamp>` distinto en cada carga.

QUÉ HACE FALTA EN EL ENTORNO
----------------------------
    SUPABASE_URL             ya estaba: de dónde se LEE la imagen.
    SUPABASE_STORAGE_BUCKET  ya estaba (por defecto "static").
    SUPABASE_PROMO_FILE      ya estaba (por defecto "promocion.jpg").
    SUPABASE_STORAGE_KEY     NUEVA: hace falta para ESCRIBIR.

`SUPABASE_STORAGE_KEY` es cualquier clave de Supabase con permiso de
escritura sobre ese bucket. Sirve la `service_role`, pero si prefieres algo
más acotado vale igual una clave con una policy que solo deje escribir ese
archivo: a este módulo le da lo mismo, la manda como Bearer y ya.

Sin esa variable la subida se niega con un mensaje que dice exactamente qué
falta. Leer la imagen sigue funcionando igual, así que la home nunca se ve
afectada por esto.
"""

import os
import time

import requests


# Tipos que aceptamos, con su firma de bytes. Se comprueba la FIRMA, no la
# extensión ni el Content-Type que manda el navegador: esos los escribe
# quien sube el archivo y no prueban nada.
FIRMAS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

# WEBP no se reconoce con un prefijo fijo: es "RIFF" + 4 bytes de tamaño +
# "WEBP", así que se comprueba aparte.
MAX_BYTES = 5 * 1024 * 1024          # 5 MB

# No hay tamaño MÍNIMO. Hubo uno y era un error: un PNG válido puede pesar
# 70 bytes, y el mínimo lo rechazaba diciendo que estaba dañado. Quien
# decide si esto es una imagen es la firma de bytes, no su tamaño.

TIEMPO_ESPERA = 30                   # segundos para hablar con Supabase


def get_promo_image_url() -> str:
    """
    URL pública del banner, con cache-buster.

    Estructura: <SUPABASE_URL>/storage/v1/object/public/<bucket>/<file>.

    El `?t=<timestamp>` hace que, al reemplazar la imagen, el navegador la
    traiga al instante sin hard-refresh. (El banner es ligero; no nos
    preocupa el extra de no cachearlo.)

    Si faltara SUPABASE_URL (entorno mal configurado), caemos a la imagen
    local en /static para no romper la home.
    """
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    bucket = (os.getenv("SUPABASE_STORAGE_BUCKET") or "static").strip("/")
    file_name = (os.getenv("SUPABASE_PROMO_FILE") or "promocion.jpg").lstrip("/")
    if not base:
        return f"/static/img/{file_name}"
    return f"{base}/storage/v1/object/public/{bucket}/{file_name}?t={int(time.time())}"


def detectar_tipo(datos: bytes):
    """
    Devuelve el Content-Type real mirando los primeros bytes, o None si eso
    no es una imagen de las que aceptamos.
    """
    for firma, tipo in FIRMAS:
        if datos.startswith(firma):
            return tipo
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "image/webp"
    return None


def validar(datos: bytes):
    """
    Revisa el archivo que llegó. Devuelve `(content_type, error)`: uno de
    los dos siempre es None.
    """
    if not datos:
        return None, "No llegó ningún archivo. Elige una imagen."

    # Sin decir cuánto pesa: quien llama lee el archivo con tope, así que
    # aquí no siempre se conoce el tamaño real.
    if len(datos) > MAX_BYTES:
        return None, (f"La imagen supera los {MAX_BYTES // 1024 // 1024} MB. "
                      f"Súbela más liviana.")

    tipo = detectar_tipo(datos)
    if not tipo:
        return None, ("Eso no es una imagen JPG, PNG ni WEBP. Revisa el "
                      "archivo: no basta con cambiarle la extensión.")

    return tipo, None


def _configuracion():
    """Lo que hace falta para escribir. Devuelve `(datos, error)`."""
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    bucket = (os.getenv("SUPABASE_STORAGE_BUCKET") or "static").strip("/")
    archivo = (os.getenv("SUPABASE_PROMO_FILE") or "promocion.jpg").lstrip("/")
    clave = (os.getenv("SUPABASE_STORAGE_KEY") or "").strip()

    if not base:
        return None, ("Falta SUPABASE_URL en el entorno: sin eso no se sabe "
                      "a qué proyecto de Supabase subir la imagen.")
    if not clave:
        return None, (
            "Falta SUPABASE_STORAGE_KEY en el entorno. Es la clave de "
            "Supabase con permiso para escribir en el bucket "
            f"«{bucket}». Defínela en el .env (local) y en las variables "
            "de entorno de Render, y vuelve a intentarlo."
        )

    return {"base": base, "bucket": bucket, "archivo": archivo, "clave": clave}, None


def motivo_no_disponible():
    """
    None si se puede subir; si no, el texto que explica qué falta.

    Sirve para avisar en el panel ANTES de que el admin elija un archivo y
    se lleve el chasco al darle al botón.
    """
    _, error = _configuracion()
    return error


def subir(datos: bytes, content_type: str):
    """
    Reemplaza el banner en Supabase Storage. Devuelve `(ok, error)`.

    Va contra la API de Storage con `x-upsert: true`, que es lo que hace
    que PISE el archivo anterior en vez de quejarse de que ya existe.

    Nunca lanza excepción: cualquier fallo vuelve como un texto que se le
    puede enseñar al admin.
    """
    config, error = _configuracion()
    if error:
        return False, error

    url = (f"{config['base']}/storage/v1/object/"
           f"{config['bucket']}/{config['archivo']}")

    try:
        respuesta = requests.post(
            url,
            data=datos,
            headers={
                "Authorization": f"Bearer {config['clave']}",
                "apikey": config["clave"],
                "Content-Type": content_type,
                "x-upsert": "true",      # pisa la imagen anterior
                "Cache-Control": "max-age=60",
            },
            timeout=TIEMPO_ESPERA,
        )
    except requests.Timeout:
        return False, ("Supabase no respondió a tiempo. La imagen puede "
                       "haberse subido o no: recarga la home para ver cuál "
                       "está puesta.")
    except requests.RequestException as e:
        return False, f"No se pudo hablar con Supabase: {e}"

    if respuesta.status_code in (200, 201):
        return True, None

    if respuesta.status_code in (401, 403):
        return False, ("Supabase rechazó la clave: SUPABASE_STORAGE_KEY no "
                       f"tiene permiso para escribir en «{config['bucket']}».")

    if respuesta.status_code == 404:
        return False, (f"Supabase no encuentra el bucket «{config['bucket']}». "
                       "Revisa SUPABASE_STORAGE_BUCKET.")

    if respuesta.status_code == 413:
        return False, "Supabase rechazó la imagen por tamaño. Súbela más liviana."

    detalle = (respuesta.text or "")[:200]
    return False, f"Supabase respondió {respuesta.status_code}: {detalle}"
