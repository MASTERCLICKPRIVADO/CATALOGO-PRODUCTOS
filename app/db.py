"""
Capa de persistencia contra Supabase Postgres.

Tablas: usuarios, data (catálogo + inventario), carrito, reservas.

El pooler de Supabase (puerto 6543) opera en modo Transaction, por lo que
desactivamos los prepared statements server-side (`prepare_threshold=None`)
para evitar problemas de estado entre conexiones reutilizadas.
"""

import os
from datetime import datetime, timezone, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Json
from dotenv import load_dotenv

load_dotenv()

# Colombia no usa horario de verano: siempre UTC-5. Usamos un offset fijo
# (en vez de depender de la zona horaria de la sesión, que el pooler de
# Supabase ignora y deja en UTC).
BOGOTA_TZ = timezone(timedelta(hours=-5))


def hora_bogota_para_guardar():
    """
    Devuelve la hora actual de Bogotá lista para guardar en una columna
    `timestamptz` de modo que el dashboard de Supabase (que muestra los
    timestamptz en UTC) la despliegue con la hora colombiana correcta.

    Se calcula la hora real de Bogotá y luego se re-etiqueta como UTC, así
    el instante almacenado coincide con la hora de pared colombiana,
    independientemente de la zona horaria de la conexión.
    """
    ahora_bogota = datetime.now(BOGOTA_TZ)
    return ahora_bogota.replace(tzinfo=timezone.utc)


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "Falta DATABASE_URL en el entorno. Define la connection string del "
        "pooler de Supabase (puerto 6543) en tu .env o en Render."
    )


def _get_conn():
    """Abre una conexión nueva contra Supabase. Cerrar con `with`."""
    return psycopg.connect(
        DATABASE_URL,
        prepare_threshold=None,
        row_factory=dict_row,
        autocommit=False,
        options="-c timezone=America/Bogota",
    )


def init_storage():
    """
    Las tablas viven en Supabase (gestionadas por SQL migrations en el
    dashboard). Este hook solo valida que la conexión funciona al arrancar.
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as e:
        print(f"⚠️  No se pudo verificar la conexión a Supabase: {e}")


# ----------------------- USUARIOS -----------------------

def obtener_usuario(usuario):
    """Devuelve dict {usuario, contrasenia, ciudad, codigo_referido, permisos} o None."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT usuario, contrasenia, ciudad, codigo_referido, permisos FROM usuarios WHERE usuario = %s",
                (str(usuario),),
            )
            return cur.fetchone()


def obtener_referido(codigo):
    """
    Busca un código en `directorio_empleados`. Devuelve
    dict {codigo, nombre, ciudad} o None si no existe.
    El match es case-insensitive y tolerante a espacios alrededor.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT codigo, nombre, ciudad
                     FROM directorio_empleados
                    WHERE LOWER(TRIM(codigo)) = LOWER(TRIM(%s))
                    LIMIT 1""",
                (str(codigo),),
            )
            return cur.fetchone()


def obtener_directorio_tiendas() -> dict:
    """
    Devuelve {tienda: celular} desde `directorio_tiendas`.

    Se usa para enrutar la consulta por WhatsApp de un usuario invitado a una
    tienda concreta (ver app/home.py `detalle_producto`). La clave es el nombre
    de la tienda tal como aparece en `data.tienda` (normalizada con strip).
    """
    out = {}
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tienda, celular FROM directorio_tiendas")
            for r in cur.fetchall():
                t = str(r.get("tienda") or "").strip()
                if t:
                    out[t] = str(r.get("celular") or "").strip()
    return out


def actualizar_referido_y_ciudad(usuario, codigo_referido, ciudad):
    """
    Sobreescribe `codigo_referido` y `ciudad` del usuario en la BD.
    Se llama en cada login: el código ingresado en ese momento define
    la ciudad cuyo inventario verá el usuario en esta sesión.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE usuarios
                      SET codigo_referido = %s,
                          ciudad = %s
                    WHERE usuario = %s""",
                (
                    str(codigo_referido).strip(),
                    str(ciudad).strip(),
                    str(usuario),
                ),
            )
        conn.commit()


def actualizar_contrasenia(usuario, nueva_contrasenia):
    """
    Sobreescribe la contraseña almacenada del usuario. Se usa para migrar
    contraseñas legacy en texto plano a bcrypt tras un login exitoso.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE usuarios SET contrasenia = %s WHERE usuario = %s",
                (str(nueva_contrasenia), str(usuario)),
            )
        conn.commit()


def crear_usuario(correo, contrasenia, ciudad, codigo_referido):
    """
    Inserta un nuevo usuario en `usuarios`. El correo se almacena en la
    columna `usuario` (que es también el identificador de login).

    Devuelve True si lo creó; False si el correo ya estaba registrado.
    Cualquier otro error se propaga.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Idempotencia: si el correo ya existe, no hacemos nada
            cur.execute(
                "SELECT 1 FROM usuarios WHERE LOWER(usuario) = LOWER(%s) LIMIT 1",
                (str(correo),),
            )
            if cur.fetchone():
                return False

            cur.execute(
                """INSERT INTO usuarios (usuario, contrasenia, ciudad, codigo_referido)
                   VALUES (%s, %s, %s, %s)""",
                (
                    str(correo).strip(),
                    str(contrasenia),
                    str(ciudad).strip(),
                    str(codigo_referido).strip(),
                ),
            )
        conn.commit()
    return True


# ----------------------- INVENTARIO -----------------------

def descontar_inventario(referencia, talla, ciudad, cantidad: int = 1):
    """
    Descuenta `cantidad` unidades del inventario en `data`, distribuyendo
    entre las filas (tiendas) que coincidan con (referencia, talla, ciudad).
    Devuelve True solo si pudo descontar la cantidad completa.
    """
    restante = int(cantidad)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, inventario
                     FROM data
                    WHERE referencia = %s AND talla = %s AND ciudad = %s
                      AND inventario > 0
                    ORDER BY id
                    FOR UPDATE""",
                (str(referencia), str(talla), str(ciudad)),
            )
            rows = cur.fetchall()
            for r in rows:
                if restante <= 0:
                    break
                quitar = min(int(r["inventario"]), restante)
                cur.execute(
                    "UPDATE data SET inventario = inventario - %s WHERE id = %s",
                    (quitar, r["id"]),
                )
                restante -= quitar
        conn.commit()
    return restante == 0


def reponer_inventario(referencia, talla, ciudad, cantidad: int = 1):
    """Suma `cantidad` unidades sobre la primera fila que coincida."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM data
                    WHERE referencia = %s AND talla = %s AND ciudad = %s
                    ORDER BY id LIMIT 1 FOR UPDATE""",
                (str(referencia), str(talla), str(ciudad)),
            )
            row = cur.fetchone()
            if not row:
                return False
            cur.execute(
                "UPDATE data SET inventario = inventario + %s WHERE id = %s",
                (int(cantidad), row["id"]),
            )
        conn.commit()
    return True


# ----------------------- CARRITO -----------------------

def _con_imagen(d: dict) -> dict:
    """
    Añade al item la URL de su foto, derivada de la referencia contra R2.

    El carrito ya NO guarda la imagen: era una copia redundante (la referencia
    basta) y además se quedaba congelada con la URL del host viejo. Es el mismo
    criterio que ya usaban las reservas, cuyas tablas tampoco tienen `imagen`.

    El import va dentro de la función a propósito: home.py importa db.py, así
    que a nivel de módulo sería una importación circular.
    """
    from app.home import get_imagen_url
    d["imagen"] = get_imagen_url(d.get("referencia"))
    return d


def agregar_item_carrito(usuario, referencia, talla, ciudad, nombre, precio,
                          precio_antes="", dcto_original=""):
    """Agrega una unidad al carrito del usuario. Devuelve el id generado."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO carrito
                       (usuario, referencia, talla, ciudad, nombre, precio,
                        precio_antes, dcto_original, cantidad, fecha_agregado)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())
                   RETURNING id""",
                (
                    str(usuario), str(referencia), str(talla), str(ciudad),
                    str(nombre), str(precio),
                    "" if precio_antes is None else str(precio_antes),
                    "" if dcto_original is None else str(dcto_original),
                ),
            )
            new_id = cur.fetchone()["id"]
        conn.commit()
    return new_id


def obtener_carrito(usuario):
    """Devuelve los items del usuario como dicts, incluyendo info de tallas desde data."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, c.usuario, c.referencia, c.talla, c.ciudad, c.nombre, c.precio,
                          c.precio_antes, c.dcto_original, c.cantidad, c.fecha_agregado,
                          d.talla_cm, d.talla_co, "talla_u.s_co" as talla_usco,
                          d.aplica
                     FROM carrito c
                     LEFT JOIN data d ON c.referencia = d.referencia
                                     AND c.talla = d.talla
                                     AND c.ciudad = d.ciudad
                    WHERE c.usuario = %s
                    ORDER BY c.fecha_agregado DESC, c.id DESC""",
                (str(usuario),),
            )
            rows = cur.fetchall()
    
    # Dado que un producto puede estar en varias tiendas en la misma ciudad, 
    # el JOIN puede devolver duplicados. Agrupamos por id de carrito.
    items_map = {}
    for r in rows:
        cid = str(r["id"])
        if cid not in items_map:
            d = dict(r)
            d["id"] = cid
            d["cantidad"] = str(d.get("cantidad", 1))
            if d.get("fecha_agregado"):
                d["fecha_agregado"] = d["fecha_agregado"].strftime("%Y-%m-%d %H:%M:%S")
            else:
                d["fecha_agregado"] = ""
            items_map[cid] = _con_imagen(d)

    return list(items_map.values())


def contar_items(usuario) -> int:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(cantidad), 0) AS total FROM carrito WHERE usuario = %s",
                (str(usuario),),
            )
            row = cur.fetchone()
    return int(row["total"] or 0)


def contar_items_en_carrito(usuario, referencia, talla, ciudad) -> int:
    """Cantidad total que el usuario ya tiene en su carrito para (ref, talla, ciudad)."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(cantidad), 0) AS total
                     FROM carrito
                    WHERE usuario = %s AND referencia = %s
                      AND talla = %s AND ciudad = %s""",
                (str(usuario), str(referencia), str(talla), str(ciudad)),
            )
            row = cur.fetchone()
    return int(row["total"] or 0)


def obtener_item(item_id, usuario):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, usuario, referencia, talla, ciudad, nombre, precio,
                          precio_antes, dcto_original, cantidad, fecha_agregado
                     FROM carrito
                    WHERE id = %s AND usuario = %s""",
                (int(item_id), str(usuario)),
            )
            row = cur.fetchone()
    return _con_imagen(dict(row)) if row else None


def eliminar_item(item_id, usuario):
    """Elimina un item del carrito. Devuelve el item eliminado o None."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM carrito
                    WHERE id = %s AND usuario = %s
                  RETURNING id, usuario, referencia, talla, ciudad, nombre, precio,
                            precio_antes, dcto_original, cantidad, fecha_agregado""",
                (int(item_id), str(usuario)),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    d = dict(row)
    d["id"] = str(d["id"])
    d["cantidad"] = str(d.get("cantidad", 1))
    return _con_imagen(d)


def vaciar_carrito(usuario):
    """Vacía el carrito del usuario. Devuelve los items eliminados."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM carrito
                    WHERE usuario = %s
                  RETURNING id, usuario, referencia, talla, ciudad, nombre, precio,
                            precio_antes, dcto_original, cantidad, fecha_agregado""",
                (str(usuario),),
            )
            rows = cur.fetchall()
        conn.commit()
    items = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["cantidad"] = str(d.get("cantidad", 1))
        items.append(_con_imagen(d))
    return items


# ----------------------- RESERVAS -----------------------

# Usuarios sin permiso master (clientes/family) -> reservas_family.
# Usuarios con permiso master (tiendas) -> reservas_tiendas.
# Mismas columnas en ambas; comparten `reservas_reserva_id_seq` para que
# el número de reserva sea único sin importar en cuál tabla quede.
_RESERVAS_TABLAS = ("reservas_family", "reservas_tiendas")


def _tienda_de_salida(cur, referencia, talla, ciudad) -> str:
    """
    Tienda DESDE LA QUE saldrá el producto de un item (referencia, talla,
    ciudad): la primera fila de `data` con stock, ordenada por `id`.

    Es exactamente la tienda de la que `descontar_inventario` empieza a restar
    inventario (misma consulta), así `tienda_salida` coincide con el inventario
    que realmente se descuenta. El nombre coincide con `directorio_tiendas.tienda`.

    Devuelve "" si no hay ninguna fila con stock para ese item.
    """
    cur.execute(
        """SELECT tienda
             FROM data
            WHERE referencia = %s AND talla = %s AND ciudad = %s
              AND inventario > 0
            ORDER BY id
            LIMIT 1""",
        (str(referencia), str(talla), str(ciudad)),
    )
    row = cur.fetchone()
    return str(row["tienda"]).strip() if row and row.get("tienda") else ""


def guardar_reserva(usuario, datos_cliente: dict, items: list,
                    codigo_referido: str = "", es_master: bool = False) -> int:
    """
    Persiste una reserva: una fila por item, todas comparten el mismo
    `reserva_id` (tomado de la secuencia `reservas_reserva_id_seq`).

    Va a `reservas_tiendas` si `es_master` es True, o a `reservas_family`
    en caso contrario.

    El `codigo_referido` se guarda en cada fila (artículo) para poder
    atribuir cada reserva al empleado/promotor que refirió al cliente.

    En `reservas_tiendas` se guarda además `tienda_salida`: la tienda de la
    que sale el stock de cada item (ver `_tienda_de_salida`). Esa columna NO
    existe en `reservas_family`, por lo que el INSERT la incluye solo cuando
    la reserva cae en `reservas_tiendas`.
    """
    tabla = "reservas_tiendas" if es_master else "reservas_family"
    incluir_tienda_salida = (tabla == "reservas_tiendas")

    if incluir_tienda_salida:
        insert = SQL(
            """INSERT INTO {tabla}
                   (reserva_id, fecha, usuario, nombre, apellido, cedula,
                    correo, celular, direccion, ciudad_envio,
                    referencia, talla, ciudad_item, nombre_producto,
                    precio_unitario, cantidad, subtotal, codigo_referido,
                    tienda_salida)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
        ).format(tabla=Identifier(tabla))
    else:
        insert = SQL(
            """INSERT INTO {tabla}
                   (reserva_id, fecha, usuario, nombre, apellido, cedula,
                    correo, celular, direccion, ciudad_envio,
                    referencia, talla, ciudad_item, nombre_producto,
                    precio_unitario, cantidad, subtotal, codigo_referido)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s)"""
        ).format(tabla=Identifier(tabla))

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nextval('reservas_reserva_id_seq') AS rid")
            reserva_id = int(cur.fetchone()["rid"])

            # Una sola marca de tiempo (hora de Bogotá) para TODAS las filas
            # de esta reserva, para que el comprobante y la BD coincidan.
            fecha_reserva = hora_bogota_para_guardar()

            for it in items:
                cantidad = int(it.get("cantidad", 1) or 1)
                precio_unit = int(it.get("precio_unitario", 0) or 0)
                valores = [
                    reserva_id,
                    fecha_reserva,
                    str(usuario),
                    str(datos_cliente.get("nombre", "")),
                    str(datos_cliente.get("apellido", "")),
                    str(datos_cliente.get("cedula", "")),
                    str(datos_cliente.get("correo", "")),
                    str(datos_cliente.get("celular", "")),
                    str(datos_cliente.get("direccion", "")),
                    str(datos_cliente.get("ciudad_envio", "")),
                    str(it.get("referencia", "")),
                    str(it.get("talla", "")),
                    str(it.get("ciudad_item", "")),
                    str(it.get("nombre_producto", "")),
                    precio_unit,
                    cantidad,
                    precio_unit * cantidad,
                    str(codigo_referido or "").strip(),
                ]
                if incluir_tienda_salida:
                    # La tienda de salida se resuelve ANTES de descontar el
                    # inventario (eso ocurre después en cart.py), así que la
                    # consulta ve el stock intacto y devuelve la misma primera
                    # tienda de la que luego se restará.
                    valores.append(
                        _tienda_de_salida(
                            cur,
                            it.get("referencia", ""),
                            it.get("talla", ""),
                            it.get("ciudad_item", ""),
                        )
                    )
                cur.execute(insert, tuple(valores))
        conn.commit()
    return reserva_id


def obtener_reserva(reserva_id, usuario=None):
    """
    Devuelve una reserva completa para generar el comprobante PDF.

    Estructura: {
        "reserva_id", "datos_cliente" {nombre, apellido, cedula, correo,
        celular, direccion, ciudad_envio}, "items" [{...}], "total".
    }
    Busca en `reservas_family` y `reservas_tiendas` (en ese orden): como
    `reserva_id` es único entre ambas, a lo sumo una la contiene.
    Si se pasa `usuario`, restringe la búsqueda a sus propias reservas
    (defensa contra que un usuario descargue el comprobante de otro).
    Devuelve None si no existe o no le pertenece.
    """
    columnas = """reserva_id, fecha, usuario, nombre, apellido, cedula,
                  correo, celular, direccion, ciudad_envio,
                  referencia, talla, ciudad_item, nombre_producto,
                  precio_unitario, cantidad, subtotal, codigo_referido"""

    rows = []
    for tabla in _RESERVAS_TABLAS:
        if usuario is not None:
            query = SQL(
                f"SELECT {columnas} FROM {{tabla}} WHERE reserva_id = %s AND usuario = %s ORDER BY id"
            ).format(tabla=Identifier(tabla))
            params = (int(reserva_id), str(usuario))
        else:
            query = SQL(
                f"SELECT {columnas} FROM {{tabla}} WHERE reserva_id = %s ORDER BY id"
            ).format(tabla=Identifier(tabla))
            params = (int(reserva_id),)

        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        if rows:
            break

    if not rows:
        return None

    primera = rows[0]

    # Formatear la fecha guardada (hora de Bogotá). El valor se almacenó
    # re-etiquetado como UTC, así que lo convertimos a UTC antes de formatear
    # para recuperar la hora de pared colombiana sin importar la zona horaria
    # con la que la conexión leyó el dato.
    fecha_dt = primera.get("fecha")
    if fecha_dt is not None:
        if getattr(fecha_dt, "tzinfo", None) is not None:
            fecha_dt = fecha_dt.astimezone(timezone.utc)
        fecha_str = fecha_dt.strftime("%d/%m/%Y %I:%M %p")
    else:
        fecha_str = ""

    datos_cliente = {
        "nombre": primera.get("nombre", ""),
        "apellido": primera.get("apellido", ""),
        "cedula": primera.get("cedula", ""),
        "correo": primera.get("correo", ""),
        "celular": primera.get("celular", ""),
        "direccion": primera.get("direccion", ""),
        "ciudad_envio": primera.get("ciudad_envio", ""),
    }

    items = []
    total = 0
    for r in rows:
        cantidad = int(r.get("cantidad", 1) or 1)
        precio_unit = int(r.get("precio_unitario", 0) or 0)
        subtotal = int(r.get("subtotal", precio_unit * cantidad) or 0)
        total += subtotal
        items.append({
            "referencia": r.get("referencia", ""),
            "talla": r.get("talla", ""),
            "ciudad_item": r.get("ciudad_item", ""),
            "nombre_producto": r.get("nombre_producto", ""),
            "precio_unitario": precio_unit,
            "cantidad": cantidad,
            "subtotal": subtotal,
        })

    return {
        "reserva_id": int(primera.get("reserva_id", reserva_id)),
        "usuario": primera.get("usuario", ""),
        "codigo_referido": primera.get("codigo_referido", ""),
        "fecha": fecha_str,
        "datos_cliente": datos_cliente,
        "items": items,
        "total": total,
    }


# ----------------------- PROMOCIONES Y EXCLUSIONES -----------------------

def obtener_promocion_activa():
    """Devuelve la última promoción registrada en la tabla `promociones`."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id_promocion, descripcion FROM promociones ORDER BY id_promocion DESC LIMIT 1"
            )
            return cur.fetchone()


def obtener_referencias_excluidas(id_promocion=None):
    """
    Devuelve las referencias excluidas de la tabla `excluidos`.
    Si se pasa un id_promocion, filtra solo las de esa promoción.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if id_promocion:
                cur.execute(
                    "SELECT article_id, campaign_name FROM excluidos WHERE id_promocion = %s",
                    (id_promocion,)
                )
            else:
                cur.execute(
                    "SELECT article_id, campaign_name FROM excluidos"
                )
            return cur.fetchall()


def obtener_set_ids_excluidos(id_promocion):
    """Devuelve un set de IDs de artículos excluidos para la promoción dada."""
    excluidos = obtener_referencias_excluidas(id_promocion)
    return {str(ex['article_id']).strip() for ex in excluidos}


# ----------------------- DESCUENTOS POR CANTIDAD -----------------------
#
# El admin guarda varias PLANTILLAS de descuento ("Navidad", "Black Friday")
# en `plantillas_descuento`, cada una con sus tramos en `reglas_descuento` y
# con su propio modo acumulativo. Exactamente una puede estar activa (lo
# garantiza un índice único parcial) y el carrito usa esa.
#
# `config_descuentos` es el interruptor general: apaga la promoción sin
# desactivar ni perder la plantilla.
#
# Ver migraciones/migracion_descuentos.sql, migraciones/migracion_plantillas.sql
# y app/logicas_descuento.py.


def _interruptor_general(cur) -> bool:
    """Lee el interruptor general con un cursor ya abierto."""
    cur.execute("SELECT activo FROM config_descuentos WHERE id IS TRUE")
    fila = cur.fetchone()
    return True if fila is None else bool(fila["activo"])


def _auditar(cur, usuario: str, accion: str, detalle: dict):
    """Deja constancia de un cambio de descuentos (mismo cursor/transacción)."""
    cur.execute(
        """INSERT INTO descuentos_auditoria (fecha, usuario, accion, detalle)
           VALUES (%s, %s, %s, %s)""",
        (hora_bogota_para_guardar(), str(usuario), str(accion), Json(detalle)),
    )


def obtener_descuentos_vigentes() -> dict:
    """
    Lo que necesita el CARRITO, en una sola conexión:

        {"activo": bool, "acumulativo": bool, "plantilla": str|None,
         "reglas": [(min_items, porcentaje), ...]}

    Sale de la plantilla ACTIVA y solo con sus tramos activos, ya ordenados.
    Si no hay ninguna plantilla activa, `reglas` viene vacío: no hay promoción
    configurada y el carrito no aplica descuento por cantidad.

    Lanza excepción si la BD no responde: quien llama (app/logicas_descuento)
    decide qué hacer, y lo que hace es caer a los tramos de respaldo del código.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            activo = _interruptor_general(cur)

            cur.execute(
                "SELECT id, nombre, acumulativo FROM plantillas_descuento WHERE activa LIMIT 1"
            )
            plantilla = cur.fetchone()

            if not plantilla:
                return {"activo": activo, "acumulativo": True,
                        "plantilla": None, "reglas": []}

            cur.execute(
                """SELECT min_items, porcentaje
                     FROM reglas_descuento
                    WHERE plantilla_id = %s AND activo
                    ORDER BY min_items""",
                (plantilla["id"],),
            )
            reglas = [(int(r["min_items"]), int(r["porcentaje"])) for r in cur.fetchall()]

    return {
        "activo": activo,
        "acumulativo": bool(plantilla["acumulativo"]),
        "plantilla": plantilla["nombre"],
        "reglas": reglas,
    }


def listar_plantillas() -> dict:
    """
    Todas las plantillas con sus tramos, para la lista del panel:

        {"activo": bool, "plantillas": [{id, nombre, acumulativo, activa,
                                         reglas: [{min_items, porcentaje, activo}]}]}

    Incluye los tramos pausados, porque el admin tiene que verlos para poder
    volver a prenderlos. La activa va primero.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            activo = _interruptor_general(cur)

            cur.execute(
                """SELECT id, nombre, acumulativo, activa, actualizado_por, actualizado_en
                     FROM plantillas_descuento
                    ORDER BY activa DESC, LOWER(TRIM(nombre))"""
            )
            plantillas = [dict(p) for p in cur.fetchall()]

            cur.execute(
                """SELECT plantilla_id, min_items, porcentaje, activo
                     FROM reglas_descuento
                    ORDER BY plantilla_id, min_items"""
            )
            por_plantilla = {}
            for fila in cur.fetchall():
                por_plantilla.setdefault(fila["plantilla_id"], []).append({
                    "min_items": int(fila["min_items"]),
                    "porcentaje": int(fila["porcentaje"]),
                    "activo": bool(fila["activo"]),
                })

    for plantilla in plantillas:
        plantilla["acumulativo"] = bool(plantilla["acumulativo"])
        plantilla["activa"] = bool(plantilla["activa"])
        plantilla["reglas"] = por_plantilla.get(plantilla["id"], [])

    return {"activo": activo, "plantillas": plantillas}


def obtener_plantilla(plantilla_id: int):
    """Una plantilla con sus tramos (incluidos los pausados), o None."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, nombre, acumulativo, activa
                     FROM plantillas_descuento
                    WHERE id = %s""",
                (int(plantilla_id),),
            )
            plantilla = cur.fetchone()
            if not plantilla:
                return None

            cur.execute(
                """SELECT min_items, porcentaje, activo
                     FROM reglas_descuento
                    WHERE plantilla_id = %s
                    ORDER BY min_items""",
                (int(plantilla_id),),
            )
            reglas = [
                {
                    "min_items": int(r["min_items"]),
                    "porcentaje": int(r["porcentaje"]),
                    "activo": bool(r["activo"]),
                }
                for r in cur.fetchall()
            ]

    return {
        "id": int(plantilla["id"]),
        "nombre": plantilla["nombre"],
        "acumulativo": bool(plantilla["acumulativo"]),
        "activa": bool(plantilla["activa"]),
        "reglas": reglas,
    }


def _escribir_reglas(cur, plantilla_id: int, reglas: list, usuario: str):
    """
    Reemplaza los tramos de una plantilla (borrar + insertar).

    El panel siempre manda la tabla completa, así que un reemplazo es más
    simple y más seguro que actualizar fila por fila: no hay forma de quedar
    con una configuración a medias si dos guardados coinciden en el tiempo.
    """
    cur.execute("DELETE FROM reglas_descuento WHERE plantilla_id = %s", (int(plantilla_id),))
    for regla in reglas:
        cur.execute(
            """INSERT INTO reglas_descuento
                   (plantilla_id, min_items, porcentaje, activo, actualizado_por, actualizado_en)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                int(plantilla_id),
                int(regla["min_items"]),
                int(regla["porcentaje"]),
                bool(regla.get("activo", True)),
                str(usuario),
                hora_bogota_para_guardar(),
            ),
        )


def crear_plantilla(nombre: str, acumulativo: bool, reglas: list, usuario: str) -> int:
    """Crea una plantilla (siempre inactiva) y devuelve su id.

    Nace inactiva a propósito: crear una plantilla nueva nunca debe cambiarle
    el precio al cliente. Se activa después, como un paso aparte y explícito.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO plantillas_descuento
                       (nombre, acumulativo, activa, actualizado_por, actualizado_en)
                   VALUES (%s, %s, false, %s, %s)
                   RETURNING id""",
                (str(nombre).strip(), bool(acumulativo), str(usuario),
                 hora_bogota_para_guardar()),
            )
            plantilla_id = int(cur.fetchone()["id"])

            _escribir_reglas(cur, plantilla_id, reglas, usuario)
            _auditar(cur, usuario, "crear_plantilla", {
                "plantilla_id": plantilla_id,
                "nombre": nombre,
                "acumulativo": bool(acumulativo),
                "reglas": reglas,
            })

    return plantilla_id


def guardar_plantilla(plantilla_id: int, nombre: str, acumulativo: bool,
                      reglas: list, usuario: str) -> dict:
    """
    Guarda nombre, modo acumulativo y tramos de una plantilla existente.

    Devuelve el estado ANTERIOR (para la auditoría y para poder revertir).
    No cambia cuál plantilla está activa.
    """
    anterior = obtener_plantilla(plantilla_id)

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE plantillas_descuento
                      SET nombre = %s,
                          acumulativo = %s,
                          actualizado_por = %s,
                          actualizado_en = %s
                    WHERE id = %s""",
                (str(nombre).strip(), bool(acumulativo), str(usuario),
                 hora_bogota_para_guardar(), int(plantilla_id)),
            )

            _escribir_reglas(cur, plantilla_id, reglas, usuario)
            _auditar(cur, usuario, "guardar_plantilla", {
                "plantilla_id": int(plantilla_id),
                "antes": anterior,
                "despues": {
                    "nombre": nombre,
                    "acumulativo": bool(acumulativo),
                    "reglas": reglas,
                },
            })

    return anterior


def duplicar_plantilla(plantilla_id: int, usuario: str):
    """
    Copia una plantilla con un nombre libre ("Navidad (copia)", "(copia 2)"...).

    Devuelve el id de la copia, o None si la original no existe. La copia nace
    inactiva, como cualquier plantilla nueva.
    """
    original = obtener_plantilla(plantilla_id)
    if not original:
        return None

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT LOWER(TRIM(nombre)) AS nombre FROM plantillas_descuento"
            )
            usados = {f["nombre"] for f in cur.fetchall()}

    base = original["nombre"]
    nombre = f"{base} (copia)"
    contador = 2
    while nombre.strip().lower() in usados:
        nombre = f"{base} (copia {contador})"
        contador += 1

    return crear_plantilla(nombre, original["acumulativo"], original["reglas"], usuario)


def activar_plantilla(plantilla_id: int, usuario: str) -> bool:
    """
    Deja activa SOLO la plantilla indicada. Devuelve False si no existe.

    Son dos UPDATE en una transacción (apagar todas, prender una) en vez de
    uno solo: el índice único parcial que garantiza "una sola activa" se
    evalúa fila por fila, y un intercambio en una sola sentencia puede
    chocar consigo mismo.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nombre FROM plantillas_descuento WHERE id = %s",
                (int(plantilla_id),),
            )
            plantilla = cur.fetchone()
            if not plantilla:
                return False

            cur.execute("SELECT id, nombre FROM plantillas_descuento WHERE activa")
            antes = cur.fetchone()

            cur.execute("UPDATE plantillas_descuento SET activa = false WHERE activa")
            cur.execute(
                """UPDATE plantillas_descuento
                      SET activa = true, actualizado_por = %s, actualizado_en = %s
                    WHERE id = %s""",
                (str(usuario), hora_bogota_para_guardar(), int(plantilla_id)),
            )

            _auditar(cur, usuario, "activar_plantilla", {
                "antes": dict(antes) if antes else None,
                "despues": {"id": int(plantilla_id), "nombre": plantilla["nombre"]},
            })

    return True


def borrar_plantilla(plantilla_id: int, usuario: str) -> bool:
    """
    Borra una plantilla y sus tramos (cascada). Devuelve False si no existe.

    Quien llama debe impedir borrar la plantilla activa (ver app/admin.py):
    aquí no se valida para que la función siga siendo una operación simple.
    """
    plantilla = obtener_plantilla(plantilla_id)
    if not plantilla:
        return False

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM plantillas_descuento WHERE id = %s", (int(plantilla_id),)
            )
            _auditar(cur, usuario, "borrar_plantilla", {"plantilla": plantilla})

    return True


def guardar_interruptor_general(activo: bool, usuario: str):
    """Prende o apaga TODA la promoción por cantidad, sin tocar las plantillas."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            antes = _interruptor_general(cur)
            cur.execute(
                """INSERT INTO config_descuentos (id, activo, actualizado_por, actualizado_en)
                   VALUES (true, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE
                      SET activo = EXCLUDED.activo,
                          actualizado_por = EXCLUDED.actualizado_por,
                          actualizado_en = EXCLUDED.actualizado_en""",
                (bool(activo), str(usuario), hora_bogota_para_guardar()),
            )
            _auditar(cur, usuario, "interruptor_general",
                     {"antes": antes, "despues": bool(activo)})


def obtener_auditoria_descuentos(limite: int = 10):
    """Últimos cambios de descuentos, del más reciente al más antiguo."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT fecha, usuario, accion, detalle
                     FROM descuentos_auditoria
                    ORDER BY fecha DESC, id DESC
                    LIMIT %s""",
                (int(limite),),
            )
            return cur.fetchall()


# ----------------------- POWER BI (REPORTING) -----------------------

# Whitelist de tablas exportables a Power BI con su columna de orden. El
# nombre de tabla nunca viene del request (siempre de esta constante), así
# que es seguro componerlo como identificador SQL con `Identifier`.
POWERBI_TABLES = {
    "carrito": "id",
    "data": "id",
    "directorio_empleados": "codigo",
    "excluidos": "article_id",
    "promociones": "id_promocion",
    "reservas_family": "id",
    "reservas_tiendas": "id",
    "usuarios": "usuario",
}


def fetch_table_rows(table: str, offset: int = 0, limit: int = None):
    """
    Devuelve todas las filas de `table` (debe estar en POWERBI_TABLES),
    ordenadas por su columna natural. Si `limit` es None trae todo;
    si no, aplica LIMIT/OFFSET para paginación.
    """
    order_col = POWERBI_TABLES[table]
    query = SQL("SELECT * FROM {table} ORDER BY {order_col}").format(
        table=Identifier(table), order_col=Identifier(order_col)
    )
    params = ()
    if limit is not None:
        query = SQL("{base} LIMIT %s OFFSET %s").format(base=query)
        params = (int(limit), int(offset))
    elif offset:
        query = SQL("{base} OFFSET %s").format(base=query)
        params = (int(offset),)

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
