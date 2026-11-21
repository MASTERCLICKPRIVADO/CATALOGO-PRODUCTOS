-- ---------------------------------------------------------------------------
-- FECHAS DE LA TEMPORADA
-- ---------------------------------------------------------------------------
-- Hasta ahora la temporada se abria y se cerraba a mano: alguien tenia que
-- entrar al panel el ultimo dia a pulsar «Cerrar temporada». Si se olvidaba,
-- la tienda seguia abierta.
--
-- Con estas dos fechas, al abrir se dice desde cuando y hasta cuando vale la
-- temporada, y la tienda se cierra sola cuando pasa la de fin. Tambien se
-- puede dejar programada para que empiece mas adelante.
--
-- SON FECHAS, NO INSTANTES, y se leen en hora de Colombia (UTC-5):
--   abre_el    la temporada vale DESDE las 00:00 de ese dia.
--   cierra_el  la temporada vale HASTA el final de ese dia (incluido).
--
-- Por que `date` y no `timestamptz`: lo que el admin escribe es «del 28 al
-- 30 de mayo». Guardar un instante obligaria a decidir una hora que nadie
-- escribio, y ademas los timestamptz de este proyecto se guardan
-- re-etiquetados como UTC (ver hora_bogota_para_guardar en app/db.py), lo
-- que para un dia suelto solo trae confusion.
--
-- QUEDAN EN NULL A PROPOSITO al correr esta migracion: una temporada que ya
-- estaba abierta sigue abierta y sin fecha de fin, igual que antes. La
-- primera vez que se cierre y se vuelva a abrir, el panel ya pedira las dos
-- fechas (son obligatorias en el formulario, no en la tabla).
--
-- Es idempotente: se puede correr varias veces sin romper nada.
-- ---------------------------------------------------------------------------

ALTER TABLE temporada ADD COLUMN IF NOT EXISTS abre_el   date;
ALTER TABLE temporada ADD COLUMN IF NOT EXISTS cierra_el date;

-- El rango tiene que tener sentido. No obliga a que existan: solo dice que,
-- si estan las dos, la de fin no puede ser anterior a la de inicio.
ALTER TABLE temporada DROP CONSTRAINT IF EXISTS temporada_rango_valido;

ALTER TABLE temporada ADD CONSTRAINT temporada_rango_valido
    CHECK (abre_el IS NULL OR cierra_el IS NULL OR cierra_el >= abre_el);


-- Para comprobar como quedo:
--   SELECT abierta, abre_el, cierra_el FROM temporada WHERE id IS TRUE;
