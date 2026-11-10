-- ---------------------------------------------------------------------------
-- TEMPORADAS
-- ---------------------------------------------------------------------------
-- La tienda funciona por temporadas: se abre, se usa unos dias y se cierra.
-- El admin prepara la temporada siguiente (catalogo, excluidos y el parrafo
-- de terminos) sin que eso afecte a lo que ven los clientes, y decide el
-- momento exacto de publicarla.
--
-- QUE HAY AQUI
--   temporada            una sola fila con el estado: abierta o cerrada, la
--                        version (para que los servidores sepan que algo
--                        cambio) y el parrafo que esta preparado.
--   data_preparada       el catalogo que se subio pero TODAVIA no se publica.
--   excluidos_preparada  lo mismo para las referencias excluidas.
--
-- Publicar = volcar lo preparado sobre las tablas de verdad (`data`,
-- `excluidos`, `promociones`) dentro de UNA transaccion, y subir `version`.
-- Mientras no se publique, los clientes siguen viendo lo de siempre.
--
-- Es idempotente: se puede correr varias veces sin romper nada.
-- ---------------------------------------------------------------------------


-- ── Estado de la temporada ────────────────────────────────────────────────
-- Una sola fila, siempre. El truco de `id boolean PRIMARY KEY CHECK (id)` es
-- el mismo que ya usaba config_descuentos: la tabla no puede tener dos filas.
CREATE TABLE IF NOT EXISTS temporada (
    id                 boolean PRIMARY KEY DEFAULT true CHECK (id),

    -- Cerrada: quien entre ve el aviso de temporada cerrada y no puede
    -- reservar. El admin sigue entrando al panel.
    abierta            boolean NOT NULL DEFAULT true,

    -- Sube cada vez que cambia algo que los servidores tienen en memoria
    -- (publicar, abrir, cerrar). Cada copia del servidor compara este
    -- numero con el que cargo y, si no coincide, recarga el catalogo.
    version            bigint  NOT NULL DEFAULT 1,

    -- El parrafo de terminos que esta PREPARADO (aun sin publicar). Si es
    -- NULL, al publicar se conserva el que ya estaba en `promociones`.
    parrafo_preparado  text,

    -- Para poder contar en pantalla que hay preparado y desde cuando.
    data_filas         integer,
    data_en            timestamptz,
    data_por           text,
    excluidos_filas    integer,
    excluidos_en       timestamptz,
    excluidos_por      text,
    parrafo_en         timestamptz,
    parrafo_por        text,

    actualizado_por    text,
    actualizado_en     timestamptz
);

INSERT INTO temporada (id, abierta, version, actualizado_en)
VALUES (true, true, 1, now())
ON CONFLICT (id) DO NOTHING;


-- ── Areas de preparacion ──────────────────────────────────────────────────
-- Misma forma que las tablas de verdad. `LIKE` copia las columnas tal cual;
-- a data_preparada se le quita `id` porque lo genera `data` al publicar.
CREATE TABLE IF NOT EXISTS data_preparada (LIKE data);

ALTER TABLE data_preparada DROP COLUMN IF EXISTS id;

CREATE TABLE IF NOT EXISTS excluidos_preparada (LIKE excluidos);


-- ── La promocion ──────────────────────────────────────────────────────────
-- Siempre hay UNA fila en `promociones` y los excluidos apuntan a ella. No
-- se guarda historico de campañas a proposito: el parrafo se reescribe.
-- Esta linea solo la crea si la tabla estuviera vacia.
INSERT INTO promociones (id_promocion, descripcion)
SELECT 1, ''
WHERE NOT EXISTS (SELECT 1 FROM promociones);


-- ── Comprobacion ──────────────────────────────────────────────────────────
-- SELECT abierta, version FROM temporada;
-- SELECT count(*) FROM data_preparada;
-- SELECT count(*) FROM excluidos_preparada;
