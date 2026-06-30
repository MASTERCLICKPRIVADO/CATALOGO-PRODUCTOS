"""
LÓGICAS DE DESCUENTO  —  todo en un solo lugar
==============================================

Este archivo concentra TODA la lógica de descuentos del carrito, separada del
resto del proyecto. El carrito (app/cart.py) y la página del carrito
(templates/carrito.html) solo le PREGUNTAN a este archivo cuánto descuento
aplicar; no saben cómo se calcula. Así puedes cambiar, agregar o quitar
descuentos sin tocar el resto de la app.

──────────────────────────────────────────────────────────────────────────────
CÓMO PRENDER / APAGAR EL DESCUENTO POR CANTIDAD
──────────────────────────────────────────────────────────────────────────────
La regla actual es: mientras más artículos lleves, más descuento adicional.

    2 artículos  → 20% adicional
    3 artículos  → 30% adicional
    4 o más      → 40% adicional

Eso se define en la lista `REGLAS_DESCUENTO` de abajo. Cada línea es un tramo
con la forma (cantidad_mínima, porcentaje):

  • Para QUITAR un tramo  → comenta (pon un #) o borra esa línea.
  • Para AGREGAR un tramo → añade una línea nueva, p. ej. (5, 50).
  • Para CAMBIAR un %     → edita el número de esa línea.
  • Para APAGAR TODO el descuento por cantidad → comenta TODAS las líneas de
    adentro de la lista (los corchetes [ ] se quedan), o déjala vacía así:
    `REGLAS_DESCUENTO = []`.

Si apagas todo, la página y el carrito siguen funcionando con normalidad:
cada artículo se muestra y se cobra a su precio normal, conservando la
promoción propia que ya traiga del catálogo (precio antes / precio ahora).
No se aplica ningún descuento por cantidad ni se muestran avisos de promoción.
──────────────────────────────────────────────────────────────────────────────
"""

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  REGLAS DE DESCUENTO POR CANTIDAD                                         ║
# ║  (cantidad_mínima_de_artículos, porcentaje_de_descuento)                 ║
# ║  Comenta una línea para apagar ese tramo. Comenta todas para apagar todo.║
# ╚══════════════════════════════════════════════════════════════════════════╝
REGLAS_DESCUENTO = [
    #(2, 20),   # Lleva 2 artículos  → 20% adicional
    #(3, 30),   # Lleva 3 artículos  → 30% adicional
    #(4, 40),   # Lleva 4 o más      → 40% adicional
]


def _reglas():
    """Devuelve las reglas ordenadas de menor a mayor cantidad.

    Usa globals().get(...) a propósito: si comentas o borras por completo
    `REGLAS_DESCUENTO`, esto no se rompe — simplemente no hay reglas y todo
    el descuento queda apagado.
    """
    reglas = globals().get("REGLAS_DESCUENTO", []) or []
    return sorted(reglas, key=lambda r: r[0])


def descuento_por_cantidad(num_items: int) -> int:
    """% de descuento adicional que corresponde por llevar `num_items` artículos.

    Devuelve 0 si no hay ninguna regla activa (todo comentado) o si la
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
