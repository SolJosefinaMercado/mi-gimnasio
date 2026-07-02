DROP TABLE IF EXISTS asistencias;
DROP TABLE IF EXISTS inscripciones;
DROP TABLE IF EXISTS pagos;
DROP TABLE IF EXISTS horarios;
DROP TABLE IF EXISTS clientes;

CREATE TABLE clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    dni TEXT NOT NULL UNIQUE,
    telefono TEXT,
    email TEXT,
    activo INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE pagos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    fecha_pago TEXT NOT NULL,
    monto REAL NOT NULL,
    dias_validez INTEGER NOT NULL DEFAULT 30,
    fecha_vencimiento TEXT NOT NULL,
    nota TEXT
);

CREATE TABLE horarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dia_semana TEXT NOT NULL,
    hora_inicio TEXT NOT NULL,
    hora_fin TEXT NOT NULL,
    etiqueta TEXT,
    cupo_maximo INTEGER NOT NULL DEFAULT 10
);

CREATE TABLE inscripciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    horario_id INTEGER NOT NULL REFERENCES horarios(id) ON DELETE CASCADE,
    activa INTEGER NOT NULL DEFAULT 1,
    UNIQUE(cliente_id, horario_id)
);

CREATE TABLE asistencias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inscripcion_id INTEGER NOT NULL REFERENCES inscripciones(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    UNIQUE(inscripcion_id, fecha)
);
