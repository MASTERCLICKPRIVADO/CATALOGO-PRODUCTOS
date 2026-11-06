-- ═══════════════════════════════════════════════════════════════════════════
-- MIGRACIÓN: descuentos gestionables desde el panel de admin
-- ═══════════════════════════════════════════════════════════════════════════
-- Ejecutar UNA vez en Supabase → SQL Editor. Es idempotente: se puede correr
-- de nuevo sin borrar lo que ya haya configurado el admin.
--
-- Mueve las reglas de descuento por cantidad de `app/logicas_descuento.py`
-- (donde se editaban a mano) a estas tablas. La lista del código queda como
-- RESPALDO: si la BD falla, la app sigue aplicando esos tramos.
-- ═══════════════════════════════════════════════════════════════════════════


-- 1) Tramos de descuento por cantidad: "lleva N artículos → X% adicional".
--
--    Los CHECK son la red de seguridad real: aunque el panel tuviera un bug
--    o alguien entrara por otro lado, la BD rechaza un 400% por un dedazo.
CREATE TABLE IF NOT EXISTS reglas_descuento (
    id              serial PRIMARY KEY,
    min_items       int NOT NULL UNIQUE CHECK (min_items BETWEEN 1 AND 99),
    porcentaje      int NOT NULL CHECK (porcentaje BETWEEN 0 AND 90),
    activo          boolean NOT NULL DEFAULT true,
    actualizado_por text,
    actualizado_en  timestamptz
);


-- 2) Interruptor general del descuento por cantidad.
--
--    Tabla de UNA sola fila (el CHECK sobre la PK booleana lo garantiza):
--    permite apagar toda la promoción sin perder los tramos configurados.
CREATE TABLE IF NOT EXISTS config_descuentos (
    id              boolean PRIMARY KEY DEFAULT true CHECK (id),
    activo          boolean NOT NULL DEFAULT true,
    actualizado_por text,
    actualizado_en  timestamptz
);

INSERT INTO config_descuentos (id, activo, actualizado_por, actualizado_en)
VALUES (true, true, 'migracion', now())
ON CONFLICT (id) DO NOTHING;


-- 3) Auditoría: quién cambió los descuentos, cuándo y qué había antes.
--
--    Esto toca el precio que ve el cliente, así que cada cambio queda
--    registrado con el estado anterior y el nuevo. Sirve para revisar una
--    reclamación meses después y para devolver la configuración a como estaba.
CREATE TABLE IF NOT EXISTS descuentos_auditoria (
    id       bigserial PRIMARY KEY,
    fecha    timestamptz NOT NULL,
    usuario  text NOT NULL,
    accion   text NOT NULL,
    detalle  jsonb
);

CREATE INDEX IF NOT EXISTS descuentos_auditoria_fecha_idx
    ON descuentos_auditoria (fecha DESC);


-- 4) Semilla: los tramos que hoy están escritos en app/logicas_descuento.py.
--
--    Solo se insertan si la tabla está vacía, para que volver a correr este
--    script no pise la configuración que el admin haya dejado.
INSERT INTO reglas_descuento (min_items, porcentaje, activo, actualizado_por, actualizado_en)
SELECT v.min_items, v.porcentaje, true, 'migracion', now()
  FROM (VALUES (2, 20), (3, 30), (4, 40)) AS v(min_items, porcentaje)
 WHERE NOT EXISTS (SELECT 1 FROM reglas_descuento);


-- 5) Dar el rol de admin a una persona.
--
--    El rol vive en la BD a propósito: así se concede o se revoca con este
--    UPDATE, sin tocar el código ni volver a desplegar, y el correo no queda
--    escrito en el repositorio.
--
--    Descomenta y cambia el correo:
--
-- UPDATE usuarios SET permisos = 'admin' WHERE usuario = 'correo@dominio.com';
--
--    Para quitarlo:
--
-- UPDATE usuarios SET permisos = '' WHERE usuario = 'correo@dominio.com';


-- Comprobación rápida de cómo quedó todo:
SELECT 'reglas' AS tabla, min_items::text, porcentaje::text, activo::text
  FROM reglas_descuento
 ORDER BY min_items;
SELECT 'config' AS tabla, activo::text FROM config_descuentos;
