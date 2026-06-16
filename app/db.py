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
    """Devuelve dict {usuario, contrasenia, ciudad, codigo_referido} o None."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT usuario, contrasenia, ciudad, codigo_referido FROM usuarios WHERE usuario = %s",
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

def agregar_item_carrito(usuario, referencia, talla, ciudad, nombre, precio, imagen,
                          precio_antes="", dcto_original=""):
    """Agrega una unidad al carrito del usuario. Devuelve el id generado."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO carrito
                       (usuario, referencia, talla, ciudad, nombre, precio,
                        precio_antes, dcto_original, imagen, cantidad, fecha_agregado)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())
                   RETURNING id""",
                (
                    str(usuario), str(referencia), str(talla), str(ciudad),
                    str(nombre), str(precio),
                    "" if precio_antes is None else str(precio_antes),
                    "" if dcto_original is None else str(dcto_original),
                    str(imagen),
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
                          c.precio_antes, c.dcto_original, c.imagen, c.cantidad, c.fecha_agregado,
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
            items_map[cid] = d
            
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
                          precio_antes, dcto_original, imagen, cantidad, fecha_agregado
                     FROM carrito
                    WHERE id = %s AND usuario = %s""",
                (int(item_id), str(usuario)),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def eliminar_item(item_id, usuario):
    """Elimina un item del carrito. Devuelve el item eliminado o None."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM carrito
                    WHERE id = %s AND usuario = %s
                  RETURNING id, usuario, referencia, talla, ciudad, nombre, precio,
                            precio_antes, dcto_original, imagen, cantidad, fecha_agregado""",
                (int(item_id), str(usuario)),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    d = dict(row)
    d["id"] = str(d["id"])
    d["cantidad"] = str(d.get("cantidad", 1))
    return d


def vaciar_carrito(usuario):
    """Vacía el carrito del usuario. Devuelve los items eliminados."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM carrito
                    WHERE usuario = %s
                  RETURNING id, usuario, referencia, talla, ciudad, nombre, precio,
                            precio_antes, dcto_original, imagen, cantidad, fecha_agregado""",
                (str(usuario),),
            )
            rows = cur.fetchall()
        conn.commit()
    items = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["cantidad"] = str(d.get("cantidad", 1))
        items.append(d)
    return items


# ----------------------- RESERVAS -----------------------

def guardar_reserva(usuario, datos_cliente: dict, items: list,
                    codigo_referido: str = "") -> int:
    """
    Persiste una reserva en `reservas`: una fila por item, todas comparten
    el mismo `reserva_id` (tomado de la secuencia `reservas_reserva_id_seq`).

    El `codigo_referido` se guarda en cada fila (artículo) para poder
    atribuir cada reserva al empleado/promotor que refirió al cliente.
    """
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
                cur.execute(
                    """INSERT INTO reservas
                           (reserva_id, fecha, usuario, nombre, apellido, cedula,
                            correo, celular, direccion, ciudad_envio,
                            referencia, talla, ciudad_item, nombre_producto,
                            precio_unitario, cantidad, subtotal, codigo_referido)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
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
                    ),
                )
        conn.commit()
    return reserva_id


def obtener_reserva(reserva_id, usuario=None):
    """
    Devuelve una reserva completa para generar el comprobante PDF.

    Estructura: {
        "reserva_id", "datos_cliente" {nombre, apellido, cedula, correo,
        celular, direccion, ciudad_envio}, "items" [{...}], "total".
    }
    Si se pasa `usuario`, restringe la búsqueda a sus propias reservas
    (defensa contra que un usuario descargue el comprobante de otro).
    Devuelve None si no existe o no le pertenece.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if usuario is not None:
                cur.execute(
                    """SELECT reserva_id, fecha, usuario, nombre, apellido, cedula,
                              correo, celular, direccion, ciudad_envio,
                              referencia, talla, ciudad_item, nombre_producto,
                              precio_unitario, cantidad, subtotal, codigo_referido
                         FROM reservas
                        WHERE reserva_id = %s AND usuario = %s
                        ORDER BY id""",
                    (int(reserva_id), str(usuario)),
                )
            else:
                cur.execute(
                    """SELECT reserva_id, fecha, usuario, nombre, apellido, cedula,
                              correo, celular, direccion, ciudad_envio,
                              referencia, talla, ciudad_item, nombre_producto,
                              precio_unitario, cantidad, subtotal, codigo_referido
                         FROM reservas
                        WHERE reserva_id = %s
                        ORDER BY id""",
                    (int(reserva_id),),
                )
            rows = cur.fetchall()

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
