"""
LÓGICAS DE DESCUENTO  —  todo en un solo lugar
==============================================

Este archivo concentra TODA la lógica de descuentos del carrito, separada del
resto del proyecto. El carrito (app/cart.py) y la página del carrito
(templates/carrito.html) solo le PREGUNTAN a este archivo cuánto descuento
aplicar; no saben cómo se calcula.

──────────────────────────────────────────────────────────────────────────────
DÓNDE SE CAMBIAN LOS DESCUENTOS AHORA
──────────────────────────────────────────────────────────────────────────────
En la página, desde el panel del admin:  /admin/descuentos

El admin guarda varias PLANTILLAS ("Normal", "Navidad", "Black Friday") en la
tabla `plantillas_descuento`, cada una con sus tramos en `reglas_descuento` y
con su propio modo acumulativo, y activa la que quiera. Que haya o no una
plantilla activa es lo único que enciende o apaga la promoción: no hay otro
interruptor. Ya no hace falta editar este archivo ni volver a
desplegar para cambiar un porcentaje: el admin lo hace desde el navegador y el
cambio se ve en el carrito en cuestión de segundos.

Cada plantilla define además CÓMO se combina su % con el descuento que ya trae
la prenda (ver `precio_con_descuento`):

  • acumulativo    → se encadenan: el % del tramo se aplica sobre el precio ya
                     rebajado, así que los dos ahorros se suman.
  • no acumulativo → gana el mayor de los dos descuentos, nunca ambos.

──────────────────────────────────────────────────────────────────────────────
LA LISTA DE ABAJO ES EL RESPALDO
──────────────────────────────────────────────────────────────────────────────
`REGLAS_DESCUENTO` ya NO es la fuente de verdad: es la red de seguridad. Solo
se usa si la base de datos no responde, para que el carrito siga aplicando
algo razonable en vez de quedarse sin promoción en plena venta.

Dos cosas importantes:

  • Si la BD responde con CERO tramos (porque el admin desactivó la
    promoción o borró los tramos), eso es una respuesta válida: NO se usa
    el respaldo y el carrito trabaja sin descuento. Apagar es apagar.
  • El respaldo solo entra en juego cuando la consulta FALLA (BD caída, red,
    tabla que aún no existe porque falta correr la migración).

Si de todas formas quieres tocar el respaldo a mano, cada línea es un tramo
con la forma (cantidad_mínima, porcentaje): comenta una línea para quitar ese
tramo, o deja la lista vacía (`REGLAS_DESCUENTO = []`) para que, si la BD
falla, no se aplique ningún descuento.
──────────────────────────────────────────────────────────────────────────────
"""

import time


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  RESPALDO — tramos que se usan SOLO si la base de datos no responde      ║
# ║  (cantidad_mínima_de_artículos, porcentaje_de_descuento)                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝
REGLAS_DESCUENTO = [
    (2, 20),   # Lleva 2 artículos  → 20% adicional
    (3, 30),   # Lleva 3 artículos  → 30% adicional
    (4, 40),   # Lleva 4 o más      → 40% adicional
]


# ──────────────────────────────────────────────────────────────────────────
# CACHÉ
# ──────────────────────────────────────────────────────────────────────────
# El carrito consulta los descuentos varias veces por cada página que sirve
# (precio de cada item, banner, barra lateral). Sin caché serían varias
# conexiones a Supabase por pageview, y además el pooler es compartido.
#
# El TTL es corto a propósito: cuando el admin guarda, su propio proceso
# limpia la caché al instante (`invalidar_cache`), pero si Render está
# corriendo más de un worker los demás no se enteran de ese guardado. El TTL
# es lo que garantiza que TODOS los workers se pongan al día solos en pocos
# segundos, sin reiniciar nada.
_TTL_OK = 30.0        # segundos que vale una lectura buena de la BD
_TTL_FALLO = 5.0      # tras un fallo reintentamos pronto (no nos quedamos pegados al respaldo)

_cache = {"valor": None, "expira": 0.0}


def invalidar_cache():
    """Olvida lo cacheado: la próxima consulta vuelve a leer la BD.

    La llama el panel de admin justo después de guardar, para que el cambio
    se vea de inmediato sin esperar el TTL.
    """
    _cache["valor"] = None
    _cache["expira"] = 0.0


def _respaldo():
    """Tramos del respaldo de este archivo, tolerante a que lo hayan borrado."""
    reglas = globals().get("REGLAS_DESCUENTO", []) or []
    return [(int(minimo), int(pct)) for minimo, pct in reglas]


def _estado():
    """
    Estado vigente de los descuentos: {"activo": bool, "reglas": [(min, pct)]}.

    Lee de la BD (con caché) y cae al respaldo del archivo si la consulta
    falla. Nunca lanza excepción: el carrito no se puede caer por esto.
    """
    ahora = time.monotonic()
    if _cache["valor"] is not None and ahora < _cache["expira"]:
        return _cache["valor"]

    try:
        from app import db
        estado = db.obtener_descuentos_vigentes()
        valor = {
            "activo": bool(estado.get("activo", True)),
            "acumulativo": bool(estado.get("acumulativo", True)),
            "plantilla": estado.get("plantilla"),
            "reglas": [(int(m), int(p)) for m, p in estado.get("reglas", [])],
            "origen": "bd",
        }
        ttl = _TTL_OK
    except Exception as e:
        print(f"⚠️  No se pudieron leer los descuentos de la BD, uso el respaldo: {e}")
        # El respaldo es acumulativo porque es lo que hacia la app antes de
        # que el modo fuera configurable.
        valor = {
            "activo": True,
            "acumulativo": True,
            "plantilla": None,
            "reglas": _respaldo(),
            "origen": "respaldo",
        }
        ttl = _TTL_FALLO

    _cache["valor"] = valor
    _cache["expira"] = ahora + ttl
    return valor


def _reglas():
    """Tramos vigentes ordenados de menor a mayor cantidad.

    Devuelve [] si no hay ninguna plantilla activa o si la activa no tiene
    tramos → descuento por cantidad apagado.
    """
    estado = _estado()
    if not estado["activo"]:
        return []
    return sorted(estado["reglas"], key=lambda r: r[0])


def origen_de_las_reglas() -> str:
    """"bd" o "respaldo" — de dónde salen los tramos que se están aplicando.

    Solo informativo: el panel de admin lo muestra para que se note cuando la
    BD no está respondiendo y el carrito está trabajando con el respaldo.
    """
    return _estado().get("origen", "bd")


# ──────────────────────────────────────────────────────────────────────────
# API PÚBLICA — lo que consume app/cart.py (no cambiar las firmas)
# ──────────────────────────────────────────────────────────────────────────

def es_acumulativo() -> bool:
    """
    True si el % por cantidad se ENCADENA con el descuento propio de la prenda.

    Lo define la plantilla activa:
      - True  -> el % del tramo se aplica sobre el precio ya rebajado, así que
                 los dos ahorros se suman. Es el comportamiento histórico.
      - False -> gana el MAYOR de los dos descuentos, nunca ambos.
    """
    return bool(_estado().get("acumulativo", True))


def plantilla_activa():
    """Nombre de la plantilla vigente, o None si no hay ninguna activa."""
    return _estado().get("plantilla")


def precio_con_descuento(precio_antes, precio_ahora, porcentaje_tramo) -> int:
    """
    Precio final de UN artículo. Es el único lugar donde se decide cómo se
    combinan el descuento propio de la prenda y el % por cantidad.

        precio_antes  -> precio original, sin ningún descuento
        precio_ahora  -> precio con el descuento propio de la prenda ya aplicado
        porcentaje_tramo -> % por cantidad que le toca a este artículo
                            (0 si el artículo está excluido de la promoción)

    Acumulativo (encadenado):
        $100.000 con 30% propio (=$70.000) y tramo del 20%
        -> 70.000 x 0,80 = $56.000   (44% de ahorro total)

    No acumulativo (gana el mayor):
        el mismo caso -> gana el 30% propio -> $70.000
        Con un tramo del 50%, ganaría el tramo -> $50.000

    Nunca sube el precio: si el tramo es peor que el descuento propio, el
    cliente se queda con el precio que ya tenía.
    """
    precio_antes = int(precio_antes or 0)
    precio_ahora = int(precio_ahora or 0)
    porcentaje_tramo = int(porcentaje_tramo or 0)

    if porcentaje_tramo <= 0:
        return precio_ahora
    if precio_antes <= 0:
        precio_antes = precio_ahora

    if es_acumulativo():
        # Encadenado: el tramo se aplica sobre el precio ya rebajado.
        return int(round(precio_ahora * (100 - porcentaje_tramo) / 100))

    # Excluyente: el tramo se aplica sobre el precio ORIGINAL y se compara
    # con el descuento propio; se cobra el más barato de los dos.
    precio_con_tramo = int(round(precio_antes * (100 - porcentaje_tramo) / 100))
    return min(precio_ahora, precio_con_tramo)


def descuento_por_cantidad(num_items: int) -> int:
    """% de descuento adicional que corresponde por llevar `num_items` artículos.

    Devuelve 0 si no hay ninguna regla activa (descuento apagado) o si la
    cantidad no alcanza ni el primer tramo. 0 significa "sin descuento".
    """
    porcentaje = 0
    for cantidad_minima, pct in _reglas():
        if num_items >= cantidad_minima:
            porcentaje = pct
    return porcentaje


def reglas_descuento():
    """Lista de reglas activas como dicts {'min', 'pct'} — solo para MOSTRARLAS
    en la página del carrito (barra lateral). Si no hay reglas, devuelve []
    y la UI no muestra ninguna promoción por cantidad.
    """
    return [{"min": minimo, "pct": pct} for minimo, pct in _reglas()]


def siguiente_tramo(num_items: int):
    """Próximo tramo que MEJORA el descuento actual, para animar al cliente a
    llevar uno más ("+1 artículo → 30%").

    Devuelve un dict {'faltan': cuántos faltan, 'pct': nuevo %} o None si ya
    no hay un tramo mejor (o si el descuento está apagado).
    """
    actual = descuento_por_cantidad(num_items)
    for cantidad_minima, pct in _reglas():
        if cantidad_minima > num_items and pct > actual:
            return {"faltan": cantidad_minima - num_items, "pct": pct}
    return None
