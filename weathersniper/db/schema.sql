-- Ciudades monitoreadas y su configuracion
CREATE TABLE cities (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    icao            TEXT NOT NULL UNIQUE,
    latitude        FLOAT NOT NULL,
    longitude       FLOAT NOT NULL,
    timezone        TEXT NOT NULL,
    unit            TEXT NOT NULL,
    peak_hour_local INT NOT NULL,
    active          BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Mercados de Polymarket mapeados a ciudades
CREATE TABLE markets (
    id              TEXT PRIMARY KEY,
    city_id         TEXT REFERENCES cities(id),
    date            DATE NOT NULL,
    question        TEXT NOT NULL,
    resolution_source TEXT,
    icao_resolution TEXT,
    status          TEXT DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_temp   FLOAT,
    UNIQUE(city_id, date)
);

-- Outcomes (brackets) de cada mercado
CREATE TABLE outcomes (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT REFERENCES markets(id),
    token_id        TEXT NOT NULL,
    bracket_low     FLOAT NOT NULL,
    bracket_high    FLOAT NOT NULL,
    side            TEXT NOT NULL,
    last_price      FLOAT,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(market_id, token_id)
);

-- Senales generadas por el signal engine
CREATE TABLE signals (
    id              BIGSERIAL PRIMARY KEY,
    city_id         TEXT REFERENCES cities(id),
    market_id       TEXT REFERENCES markets(id),
    strategy        TEXT NOT NULL,
    side            TEXT NOT NULL,
    bracket_low     FLOAT NOT NULL,
    bracket_high    FLOAT NOT NULL,
    entry_price     FLOAT NOT NULL,
    metar_temp      FLOAT,
    forecast_temp   FLOAT,
    confidence      FLOAT NOT NULL,
    suggested_size  FLOAT NOT NULL,
    status          TEXT DEFAULT 'pending',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    approved_at     TIMESTAMPTZ,
    notes           TEXT
);

-- Trades ejecutados
CREATE TABLE trades (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    order_id        TEXT,
    side            TEXT NOT NULL,
    size_usdc       FLOAT NOT NULL,
    entry_price     FLOAT NOT NULL,
    shares          FLOAT NOT NULL,
    status          TEXT DEFAULT 'open',
    exit_price      FLOAT,
    pnl_usdc        FLOAT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ
);

-- Snapshot de METAR por ciudad (historico)
CREATE TABLE metar_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    city_id         TEXT REFERENCES cities(id),
    icao            TEXT NOT NULL,
    temp_c          FLOAT NOT NULL,
    temp_f          FLOAT NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    raw             JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(city_id, observed_at)
);

-- Indices utiles
CREATE INDEX idx_markets_city_date ON markets(city_id, date);
CREATE INDEX idx_signals_status ON signals(status);
CREATE INDEX idx_signals_created ON signals(created_at DESC);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_metar_city_observed ON metar_snapshots(city_id, observed_at DESC);
