-- ═══════════════════════════════════════════════════════════════════════════
-- MIGRACIÓN 2: plantillas de descuento
-- ═══════════════════════════════════════════════════════════════════════════
-- Ejecutar DESPUÉS de migracion_descuentos.sql, UNA vez, en Supabase →
-- SQL Editor. Es idempotente: se puede correr de nuevo sin perder nada.
--
-- Qué cambia:
--   Antes había UNA sola configuración de tramos. Ahora el admin guarda
--   varias PLANTILLAS con nombre ("Navidad", "Black Friday", "Normal"),
--   cada una con sus tramos y con su propio modo acumulativo, y activa la
--   que quiera. El carrito usa siempre la plantilla activa.
--
--   Los tramos que ya existían NO se pierden: se quedan agrupados en una
--   plantilla llamada "Actual", que queda activa. Es decir, al correr esto
--   el precio que ve el cliente no cambia.
-- ═══════════════════════════════════════════════════════════════════════════


-- 1) Plantillas.
--
--    `acumulativo` define cómo se combina el % del tramo con el descuento
--    que ya trae la prenda:
--      true  -> se ENCADENAN: el % del tramo se aplica sobre el precio ya
--               rebajado (precio_ahora). Es el comportamiento histórico.
--      false -> gana el MAYOR de los dos descuentos, nunca ambos.
CREATE TABLE IF NOT EXISTS plantillas_descuento (
    id              serial PRIMARY KEY,
    nombre          text NOT NULL,
    acumulativo     boolean NOT NULL DEFAULT true,
    activa          boolean NOT NULL DEFAULT false,
    actualizado_por text,
    actualizado_en  timestamptz
);

-- Nombres sin repetir, ignorando mayúsculas y espacios de los extremos.
CREATE UNIQUE INDEX IF NOT EXISTS plantillas_descuento_nombre_key
    ON plantillas_descuento (LOWER(TRIM(nombre)));

-- Como máximo UNA plantilla activa. Esto no es una convención del código:
-- lo garantiza la base de datos, así que es imposible quedar con dos
-- promociones vigentes al mismo tiempo.
CREATE UNIQUE INDEX IF NOT EXISTS plantillas_descuento_una_activa
    ON plantillas_descuento ((activa)) WHERE activa;


-- 2) Los tramos pasan a pertenecer a una plantilla.
ALTER TABLE reglas_descuento
    ADD COLUMN IF NOT EXISTS plantilla_id int;


-- 3) Mover lo que ya existía a una plantilla llamada "Actual".
--
--    Solo se crea si hay tramos sueltos (sin plantilla) que rescatar, o si
--    todavía no existe ninguna plantilla. Si ya corriste esto antes, no
--    hace nada.
INSERT INTO plantillas_descuento (nombre, acumulativo, activa, actualizado_por, actualizado_en)
SELECT 'Actual', true, true, 'migracion', now()
 WHERE NOT EXISTS (SELECT 1 FROM plantillas_descuento);

UPDATE reglas_descuento
   SET plantilla_id = (
        SELECT id FROM plantillas_descuento
         ORDER BY activa DESC, id ASC
         LIMIT 1
   )
 WHERE plantilla_id IS NULL;

-- Si quedó alguna fila sin plantilla (no debería), se borra: un tramo
-- huérfano no se puede mostrar ni aplicar.
DELETE FROM reglas_descuento WHERE plantilla_id IS NULL;


-- 4) Ahora sí: plantilla_id obligatorio, con borrado en cascada.
ALTER TABLE reglas_descuento
    ALTER COLUMN plantilla_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'reglas_descuento_plantilla_fk'
    ) THEN
        ALTER TABLE reglas_descuento
            ADD CONSTRAINT reglas_descuento_plantilla_fk
            FOREIGN KEY (plantilla_id)
            REFERENCES plantillas_descuento (id)
            ON DELETE CASCADE;
    END IF;
END $$;


-- 5) La cantidad mínima ya no es única en toda la tabla, sino dentro de cada
--    plantilla: dos plantillas distintas pueden tener su propio tramo de 2
--    artículos. Los CHECK de rango (1..99 y 0..90) se quedan igual.
ALTER TABLE reglas_descuento
    DROP CONSTRAINT IF EXISTS reglas_descuento_min_items_key;

CREATE UNIQUE INDEX IF NOT EXISTS reglas_descuento_plantilla_min_key
    ON reglas_descuento (plantilla_id, min_items);


-- Comprobación de cómo quedó:
SELECT p.id,
       p.nombre,
       p.acumulativo,
       p.activa,
       COUNT(r.id) AS tramos
  FROM plantillas_descuento p
  LEFT JOIN reglas_descuento r ON r.plantilla_id = p.id
 GROUP BY p.id, p.nombre, p.acumulativo, p.activa
 ORDER BY p.id;
