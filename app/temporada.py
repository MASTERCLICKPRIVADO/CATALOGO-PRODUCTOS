"""
TEMPORADAS  —  leer y validar los archivos que sube el admin
============================================================

La tienda funciona por temporadas. El admin sube tres cosas desde el panel:

    1. el catalogo        (CSV, va a la tabla `data`)
    2. los excluidos      (CSV, va a la tabla `excluidos`)
    3. el parrafo         (texto, va a `promociones.descripcion`)

Este modulo SOLO se encarga de leerlas y de decir si estan bien. No escribe
en la base: de eso se encarga app/db.py, y publicarlas es otra decision del
admin, posterior y aparte.

POR QUE SE VALIDA TANTO
-----------------------
Al publicar, el archivo PISA el catalogo entero. Si llega con la columna
cambiada o media vacia, la tienda se queda sin productos. Aqui es barato
decir que no; una vez publicado, ya no.

LO QUE SE ACEPTA
----------------
CSV exportado desde Excel o desde Supabase, con cualquiera de los
separadores habituales (`;`, `,`, tabulador) y en UTF-8 o en la
codificacion de Windows en español. Se detecta solo: al admin no se le
pide que sepa nada de esto.
"""

import csv
import io

import pandas as pd


# ── Limites ───────────────────────────────────────────────────────────────
# El catalogo de referencia ronda las 19.600 filas y 2,9 MB. Los topes dejan
# margen de sobra y evitan que un archivo disparatado tumbe el servidor.
MAX_BYTES = 20 * 1024 * 1024        # 20 MB
MAX_FILAS = 200_000

# Codificaciones por orden de probabilidad. `utf-8-sig` se come el BOM que
# mete Excel; `cp1252` es lo que exporta Excel en español.
CODIFICACIONES = ("utf-8-sig", "cp1252", "latin-1")


# ── Columnas ──────────────────────────────────────────────────────────────
# Las 20 de la tabla `data`, en su orden. Son las que escribe Supabase al
# exportar, asi que el archivo que ya usa el admin encaja tal cual.
COLUMNAS_DATA = (
    "tienda", "inventario", "ciudad", "referencia", "talla", "nombre",
    "division", "precio_antes", "genero", "edad", "deporte", "tipo_producto",
    "dcto", "precio_ahora", "categoria", "subcategoria", "talla_cm",
    "talla_co", "talla_u.s_co", "aplica",
)

# Sin estas cuatro no hay catalogo que valga: identifican la fila y dicen
# cuanto hay. El resto puede venir vacio.
OBLIGATORIAS_DATA = ("referencia", "talla", "ciudad", "inventario")

# La clave natural. Verificado contra la tabla real: 0 duplicados en 19.587
# filas. Es lo que permite comparar el archivo nuevo con lo que ya hay.
CLAVE_DATA = ("tienda", "referencia", "talla", "ciudad")

COLUMNAS_EXCLUIDOS = ("article_id", "campaign_name", "id_promocion")
OBLIGATORIAS_EXCLUIDOS = ("article_id",)

MAX_LARGO_PARRAFO = 4000


# ── Lectura ───────────────────────────────────────────────────────────────

def _separador(muestra: str) -> str:
    """
    Adivina el separador de un CSV mirando la primera linea.

    Se prueba el sniffer de la libreria estandar y, si no se aclara, gana el
    caracter que mas veces aparezca en la cabecera. El punto y coma va
    primero porque es lo que exporta Excel en español, que es de donde
    salen estos archivos.
    """
    cabecera = muestra.split("\n", 1)[0]
    try:
        return csv.Sniffer().sniff(cabecera, delimiters=";,\t|").delimiter
    except csv.Error:
        pass
    return max((";", ",", "\t", "|"), key=cabecera.count)


def leer_csv(datos: bytes):
    """
    Convierte los bytes subidos en un DataFrame.

    Devuelve `(df, error)`: uno de los dos siempre es None. Todas las
    columnas se leen como TEXTO a proposito; convertir viene despues, en la
    validacion, donde se puede explicar que fila fallo.
    """
    if not datos:
        return None, "No llegó ningún archivo. Elige un CSV."

    if len(datos) > MAX_BYTES:
        return None, (f"El archivo supera los {MAX_BYTES // 1024 // 1024} MB. "
                      f"Revisa que sea el archivo correcto.")

    texto = None
    for codificacion in CODIFICACIONES:
        try:
            texto = datos.decode(codificacion)
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        return None, ("No se pudo leer el archivo: no parece texto. "
                      "¿Seguro que es un CSV y no un Excel (.xlsx)?")

    if not texto.strip():
        return None, "El archivo está vacío."

    try:
        df = pd.read_csv(
            io.StringIO(texto),
            sep=_separador(texto[:4000]),
            dtype=str,
            keep_default_na=False,
            na_filter=False,
        )
    except Exception as e:
        return None, f"No se pudo interpretar el CSV: {e}"

    if df.empty:
        return None, "El archivo no tiene ninguna fila, solo la cabecera."

    if len(df) > MAX_FILAS:
        return None, (f"El archivo trae {len(df):,} filas y el máximo son "
                      f"{MAX_FILAS:,}.".replace(",", "."))

    # Cabeceras: fuera espacios y todo en minusculas, para que no importe si
    # el archivo trae "Referencia", "REFERENCIA" o " referencia ".
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df, None


# ── Validacion del catalogo ───────────────────────────────────────────────

def validar_data(df):
    """
    Revisa el catalogo. Devuelve `(df_listo, error, avisos)`.

    `df_listo` trae exactamente las 20 columnas de la tabla `data`, en
    orden, con las que faltaban rellenas en blanco. `error` bloquea la
    subida; los `avisos` no, pero son cosas que conviene mirar.
    """
    avisos = []

    faltan = [c for c in OBLIGATORIAS_DATA if c not in df.columns]
    if faltan:
        return None, (
            "Al archivo le faltan columnas imprescindibles: "
            + ", ".join(faltan)
            + ". Revisa que sea la exportación del catálogo y que la primera "
              "fila sean los nombres de las columnas."
        ), avisos

    sobran = [c for c in df.columns if c not in COLUMNAS_DATA]
    if sobran:
        avisos.append("Se ignoran columnas que no existen en el catálogo: "
                      + ", ".join(sobran[:8])
                      + ("..." if len(sobran) > 8 else ""))

    ausentes = [c for c in COLUMNAS_DATA if c not in df.columns]
    if ausentes:
        avisos.append("Estas columnas no venían y quedarán vacías: "
                      + ", ".join(ausentes))

    listo = pd.DataFrame()
    for columna in COLUMNAS_DATA:
        if columna in df.columns:
            listo[columna] = df[columna].astype(str).str.strip()
        else:
            listo[columna] = ""

    # Inventario: tiene que ser un entero que no sea negativo.
    inventario = pd.to_numeric(listo["inventario"], errors="coerce")
    malas = inventario.isna()
    if malas.any():
        fila = int(malas.idxmax()) + 2      # +2: cabecera y base 1
        valor = listo.loc[malas.idxmax(), "inventario"]
        return None, (f"El inventario tiene que ser un número entero. En la "
                      f"fila {fila} dice «{valor}»."), avisos

    negativas = inventario < 0
    if negativas.any():
        fila = int(negativas.idxmax()) + 2
        return None, f"Hay inventario negativo en la fila {fila}.", avisos

    listo["inventario"] = inventario.astype(int)

    # Las que identifican la fila no pueden venir en blanco.
    for columna in ("referencia", "talla", "ciudad"):
        vacias = listo[columna] == ""
        if vacias.any():
            fila = int(vacias.idxmax()) + 2
            return None, f"La columna «{columna}» viene vacía en la fila {fila}.", avisos

    # La clave natural tiene que ser unica, o al publicar habria dos filas
    # compitiendo por el mismo inventario.
    duplicadas = listo.duplicated(subset=list(CLAVE_DATA), keep=False)
    if duplicadas.any():
        cuantas = int(duplicadas.sum())
        primera = listo[duplicadas].iloc[0]
        return None, (
            f"Hay {cuantas} filas repetidas para la misma combinación de "
            f"tienda, referencia, talla y ciudad. La primera es "
            f"«{primera['referencia']}» talla {primera['talla']} en "
            f"{primera['ciudad']}. Deja una sola fila por combinación."
        ), avisos

    if (listo["tienda"] == "").any():
        avisos.append("Hay filas sin tienda. Esas unidades no se podrán "
                      "asignar a un punto de salida al reservar.")

    if int(listo["inventario"].sum()) == 0:
        avisos.append("El archivo no trae ni una unidad de inventario: la "
                      "tienda quedaría sin stock para reservar.")

    return listo, None, avisos


# ── Validacion de los excluidos ───────────────────────────────────────────

def validar_excluidos(df):
    """
    Revisa el archivo de excluidos. Devuelve `(df_listo, error, avisos)`.

    `id_promocion` del archivo se IGNORA: al publicar, todas las filas se
    marcan con la promoción vigente. Si se respetara, un número equivocado
    en el archivo dejaría las exclusiones apuntando a ninguna parte y la
    promoción se aplicaría a artículos que no debía.
    """
    avisos = []

    faltan = [c for c in OBLIGATORIAS_EXCLUIDOS if c not in df.columns]
    if faltan:
        return None, (
            "Al archivo de excluidos le falta la columna «article_id», que "
            "es la referencia del artículo excluido."
        ), avisos

    listo = pd.DataFrame()
    listo["article_id"] = df["article_id"].astype(str).str.strip()
    listo["campaign_name"] = (
        df["campaign_name"].astype(str).str.strip()
        if "campaign_name" in df.columns else ""
    )

    vacias = listo["article_id"] == ""
    if vacias.any():
        fila = int(vacias.idxmax()) + 2
        return None, f"Hay una referencia vacía en la fila {fila}.", avisos

    repetidas = listo.duplicated(subset=["article_id"], keep="first")
    if repetidas.any():
        avisos.append(f"Se quitaron {int(repetidas.sum())} referencias "
                      f"repetidas; cada una se excluye una sola vez.")
        listo = listo[~repetidas].reset_index(drop=True)

    if "id_promocion" in df.columns:
        avisos.append("La columna «id_promocion» del archivo se ignora: "
                      "las exclusiones se asignan a la promoción vigente.")

    return listo, None, avisos


# ── Validacion del parrafo ────────────────────────────────────────────────

def validar_parrafo(texto: str):
    """Devuelve `(texto_limpio, error)`."""
    limpio = str(texto or "").strip()
    if not limpio:
        return "", "El párrafo de términos no puede quedar vacío."
    if len(limpio) > MAX_LARGO_PARRAFO:
        return "", (f"El párrafo no puede pasar de {MAX_LARGO_PARRAFO} "
                    f"caracteres; trae {len(limpio)}.")
    return limpio, None


# ──────────────────────────────────────────────────────────────────────────
# LA TEMPORADA EN MARCHA
# ──────────────────────────────────────────────────────────────────────────
# Lo de arriba lee archivos. Lo de aqui responde, en cada peticion, a dos
# preguntas: ¿esta abierta la tienda? y ¿el catalogo que tengo en memoria
# sigue siendo el bueno?
#
# POR QUE HACE FALTA ESTO
# -----------------------
# El catalogo son ~20.000 filas y 29 MB que viven en la memoria del proceso
# (`app.state.df`), porque consultarlo en cada peticion seria lentisimo.
# Render puede correr VARIAS copias del servidor a la vez, y cada una tiene
# su propia memoria. Si el admin publica una temporada, la copia que
# atendio esa peticion se entera; las demas no.
#
# La solucion es un numero: `temporada.version`, que sube con cada publicar,
# abrir o cerrar. Cada copia lo mira de vez en cuando y, si no coincide con
# el que tenia, recarga el catalogo. Nadie reinicia nada.
#
# EL COSTE
# --------
# Una consulta minuscula (una fila, dos columnas) como mucho cada
# TTL_SEGUNDOS, no por peticion. El catalogo se recarga solo cuando el
# numero cambia de verdad.

import time

TTL_SEGUNDOS = 10.0        # cada cuanto se vuelve a preguntar por la version
TTL_FALLO = 5.0            # si la BD falla, se reintenta antes

_cache = {"valor": None, "expira": 0.0}

# La version del catalogo que esta cargado en ESTE proceso. Empieza en None:
# la primera comprobacion siempre recarga y deja el numero puesto.
_version_cargada = {"valor": None}


def invalidar_cache():
    """
    Olvida lo cacheado para que la proxima peticion lo vuelva a mirar.

    La llama el panel despues de publicar, abrir o cerrar, para que el
    cambio se vea YA en el proceso que atendio al admin en vez de esperar
    a que expire el TTL.
    """
    _cache["valor"] = None
    _cache["expira"] = 0.0


def estado_vigente() -> dict:
    """
    {"abierta": bool, "version": int, "existe": bool}, con cache.

    Nunca lanza excepcion ni bloquea la tienda: si la base no responde, se
    asume ABIERTA. Dejar la tienda cerrada por un fallo de red seria peor
    que el fallo.
    """
    ahora = time.monotonic()
    if _cache["valor"] is not None and ahora < _cache["expira"]:
        return _cache["valor"]

    try:
        from app import db
        estado = db.obtener_estado_temporada()
        valor = {"abierta": bool(estado.get("abierta", True)),
                 "version": int(estado.get("version", 0)),
                 "existe": bool(estado.get("existe", False))}
        espera = TTL_SEGUNDOS
    except Exception as e:
        print(f"⚠️  No se pudo leer el estado de la temporada: {e}")
        valor = {"abierta": True, "version": _version_cargada["valor"] or 0,
                 "existe": False}
        espera = TTL_FALLO

    _cache["valor"] = valor
    _cache["expira"] = ahora + espera
    return valor


def asegurar_catalogo(app) -> bool:
    """
    Recarga `app.state.df` si otra copia del servidor publico una temporada.

    Devuelve True si recargo. Cuesta unos segundos, pero solo pasa la
    primera vez que este proceso ve una version nueva.
    """
    version = estado_vigente()["version"]
    if _version_cargada["valor"] == version:
        return False

    try:
        from app import home
        app.state.df = home.load_data()
    except Exception as e:
        # Mejor seguir con el catalogo viejo que quedarse sin ninguno; se
        # reintenta en la siguiente comprobacion.
        print(f"⚠️  No se pudo recargar el catálogo (versión {version}): {e}")
        return False

    anterior = _version_cargada["valor"]
    _version_cargada["valor"] = version
    print(f"✅ Catálogo recargado: versión {anterior} → {version}")
    return True


def marcar_catalogo_cargado():
    """
    Deja constancia de que el catalogo en memoria corresponde a la version
    actual. La llama el arranque, que ya carga el catalogo por su cuenta,
    para que la primera peticion no lo recargue otra vez sin motivo.
    """
    _version_cargada["valor"] = estado_vigente()["version"]
