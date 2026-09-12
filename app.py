import os
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Flask, g, redirect, render_template, request, session, url_for, flash, send_from_directory
import requests
from sqlalchemy import (
    Boolean, Column, Date, Float, ForeignKey, Integer, MetaData, String,
    Table, Text, UniqueConstraint, create_engine, inspect, text,
)
from sqlalchemy.exc import IntegrityError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
TZ_LOCAL = ZoneInfo("America/Argentina/Buenos_Aires")


def hoy():
    return datetime.now(TZ_LOCAL).date()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.permanent_session_lifetime = timedelta(days=180)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def _env_limpio(nombre, default=None):
    valor = os.environ.get(nombre, default)
    return valor.strip() if valor else valor


WHATSAPP_TOKEN = _env_limpio("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = _env_limpio("WHATSAPP_PHONE_ID")
WHATSAPP_TEMPLATE_NAME = _env_limpio("WHATSAPP_TEMPLATE_NAME", "aviso_pago")
WHATSAPP_TEMPLATE_LANG = _env_limpio("WHATSAPP_TEMPLATE_LANG", "es_AR")

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    # Render entrega "postgres://"; lo normalizamos al dialecto psycopg (v3) de SQLAlchemy
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    engine = create_engine(f"sqlite:///{os.path.join(BASE_DIR, 'gimnasio.db')}")

metadata = MetaData()

clientes_t = Table(
    "clientes", metadata,
    Column("id", Integer, primary_key=True),
    Column("nombre", String, nullable=False),
    Column("dni", String, nullable=False, unique=True),
    Column("telefono", String),
    Column("email", String),
    Column("activo", Boolean, nullable=False),
    Column("objetivo_semanal", Integer),
)

pagos_t = Table(
    "pagos", metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", Integer, ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False),
    Column("fecha_pago", Date, nullable=False),
    Column("monto", Float, nullable=False),
    Column("dias_validez", Integer, nullable=False),
    Column("fecha_vencimiento", Date, nullable=False),
    Column("nota", String),
    Column("cupos_totales", Integer),
)

horarios_t = Table(
    "horarios", metadata,
    Column("id", Integer, primary_key=True),
    Column("dia_semana", String, nullable=False),
    Column("hora_inicio", String, nullable=False),
    Column("hora_fin", String, nullable=False),
    Column("etiqueta", String),
    Column("cupo_maximo", Integer, nullable=False),
    Column("activo", Boolean, nullable=False),
)

inscripciones_t = Table(
    "inscripciones", metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", Integer, ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False),
    Column("horario_id", Integer, ForeignKey("horarios.id", ondelete="CASCADE"), nullable=False),
    Column("activa", Boolean, nullable=False),
    Column("semana", Date),
    UniqueConstraint("cliente_id", "horario_id"),
)

asistencias_t = Table(
    "asistencias", metadata,
    Column("id", Integer, primary_key=True),
    Column("inscripcion_id", Integer, ForeignKey("inscripciones.id", ondelete="CASCADE"), nullable=False),
    Column("fecha", Date, nullable=False),
    UniqueConstraint("inscripcion_id", "fecha"),
)

planes_t = Table(
    "planes", metadata,
    Column("id", Integer, primary_key=True),
    Column("nombre", String, nullable=False),
    Column("precio", Float, nullable=False),
)

configuracion_t = Table(
    "configuracion", metadata,
    Column("id", Integer, primary_key=True),
    Column("alias_mp", String, nullable=False),
    Column("whatsapp_numero", String, nullable=False),
)

entrenamientos_t = Table(
    "entrenamientos", metadata,
    Column("id", Integer, primary_key=True),
    Column("fecha", Date, nullable=False, unique=True),
    Column("contenido", Text, nullable=False),
)

notificaciones_t = Table(
    "notificaciones", metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", Integer, ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False),
    Column("mensaje", String, nullable=False),
    Column("leida", Boolean, nullable=False, default=False),
    Column("fecha", Date, nullable=False),
)

EJERCICIOS_BASE = ["Sentadilla", "Press banca", "Peso muerto"]

registros_ejercicio_t = Table(
    "registros_ejercicio", metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", Integer, ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False),
    Column("ejercicio", String, nullable=False),
    Column("fecha", Date, nullable=False),
    Column("series", Integer, nullable=False),
    Column("repeticiones", Integer, nullable=False),
    Column("porcentaje", Float),
    Column("kilos", Float, nullable=False),
    Column("rm_estimado", Float, nullable=False),
)

MINUTOS_LIMITE_RESERVA = 15


def get_db():
    if "db" not in g:
        g.db = engine.connect()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    metadata.create_all(engine)
    inspector = inspect(engine)
    horarios_cols = {c["name"] for c in inspector.get_columns("horarios")}
    inscripciones_cols = {c["name"] for c in inspector.get_columns("inscripciones")}
    pagos_cols = {c["name"] for c in inspector.get_columns("pagos")}
    clientes_cols = {c["name"] for c in inspector.get_columns("clientes")}
    with engine.begin() as conn:
        # Migraciones livianas para bases ya desplegadas antes de estas columnas existir.
        if "activo" not in horarios_cols:
            conn.execute(text("ALTER TABLE horarios ADD COLUMN activo BOOLEAN NOT NULL DEFAULT TRUE"))
        if "semana" not in inscripciones_cols:
            conn.execute(text("ALTER TABLE inscripciones ADD COLUMN semana DATE"))
        if "cupos_totales" not in pagos_cols:
            conn.execute(text("ALTER TABLE pagos ADD COLUMN cupos_totales INTEGER"))
        if "objetivo_semanal" not in clientes_cols:
            conn.execute(text("ALTER TABLE clientes ADD COLUMN objetivo_semanal INTEGER"))
        existe = conn.execute(text("SELECT 1 FROM configuracion WHERE id = 1")).fetchone()
        if not existe:
            conn.execute(text("INSERT INTO configuracion (id, alias_mp, whatsapp_numero) VALUES (1, '', '')"))


def inicio_semana(fecha):
    # La "semana" de cupos arranca el sábado a las 00:00 (hora Argentina), no el lunes:
    # así lo que se reserva el fin de semana para el lunes cae en el mismo ciclo.
    dias_desde_sabado = (fecha.weekday() - 5) % 7
    return fecha - timedelta(days=dias_desde_sabado)


ORDEN_DESDE_SABADO = ["Sábado", "Domingo", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]


def fecha_de_clase(semana, dia_semana):
    # Fecha calendario real de esa clase dentro del ciclo (semana = sábado que lo inicia).
    return semana + timedelta(days=ORDEN_DESDE_SABADO.index(dia_semana))


def datetime_clase(fecha_clase, hora_inicio):
    # Combina la fecha calendario de la clase con su hora de inicio ("HH:MM") en un
    # datetime con timezone, para poder compararlo contra "ahora".
    hh, mm = (int(p) for p in hora_inicio.split(":"))
    return datetime.combine(fecha_clase, datetime.min.time(), tzinfo=TZ_LOCAL).replace(hour=hh, minute=mm)


def reserva_cerrada_por_tiempo(fecha_clase, hora_inicio):
    inicio_clase = datetime_clase(fecha_clase, hora_inicio)
    ahora = datetime.now(TZ_LOCAL)
    return inicio_clase - ahora < timedelta(minutes=MINUTOS_LIMITE_RESERVA)


# ---------- Modelos livianos sobre filas de la base ----------

class Cliente:
    def __init__(self, row, proximo_vencimiento=None):
        self.id = row["id"]
        self.nombre = row["nombre"]
        self.dni = row["dni"]
        self.telefono = row["telefono"]
        self.email = row["email"]
        self.activo = bool(row["activo"])
        self.objetivo_semanal = row["objetivo_semanal"] if "objetivo_semanal" in row.keys() else None
        self.proximo_vencimiento = proximo_vencimiento
        self.cupos_totales = None
        self.cupos_usados = None
        self.cupos_restantes = None

    @property
    def abono_vencido(self):
        return self.proximo_vencimiento is not None and self.proximo_vencimiento < hoy()

    @property
    def abono_impago(self):
        return self.proximo_vencimiento is None or self.abono_vencido

    @property
    def sin_cupos(self):
        return self.cupos_totales is not None and self.cupos_restantes <= 0

    @property
    def puede_reservar(self):
        return not self.abono_impago and not self.sin_cupos


def _to_date(value):
    # SQL crudo vía text() no aplica el result_processor de SQLAlchemy: sqlite3
    # devuelve las columnas Date como str, mientras que psycopg2 ya entrega date.
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _cliente_con_vencimiento(db, row):
    ultimo = db.execute(
        text(
            "SELECT fecha_pago, fecha_vencimiento, cupos_totales FROM pagos "
            "WHERE cliente_id = :cid ORDER BY fecha_vencimiento DESC LIMIT 1"
        ),
        {"cid": row["id"]},
    ).mappings().fetchone()
    vencimiento = _to_date(ultimo["fecha_vencimiento"]) if ultimo else None
    cliente = Cliente(row, vencimiento)
    if ultimo and ultimo["cupos_totales"] is not None:
        usados = db.execute(
            text(
                "SELECT COUNT(*) AS c FROM asistencias a JOIN inscripciones i ON i.id = a.inscripcion_id "
                "WHERE i.cliente_id = :cid AND a.fecha >= :fecha_desde"
            ),
            {"cid": row["id"], "fecha_desde": _to_date(ultimo["fecha_pago"])},
        ).mappings().fetchone()["c"]
        cliente.cupos_totales = ultimo["cupos_totales"]
        cliente.cupos_usados = usados
        cliente.cupos_restantes = max(ultimo["cupos_totales"] - usados, 0)
    return cliente


def listar_clientes(db, solo_activos=False):
    query = "SELECT * FROM clientes"
    params = {}
    if solo_activos:
        query += " WHERE activo = :activo"
        params["activo"] = True
    query += " ORDER BY nombre"
    rows = db.execute(text(query), params).mappings().fetchall()
    return [_cliente_con_vencimiento(db, r) for r in rows]


def obtener_cliente_por_dni(db, dni):
    row = db.execute(text("SELECT * FROM clientes WHERE dni = :dni"), {"dni": dni}).mappings().fetchone()
    if not row:
        return None
    return _cliente_con_vencimiento(db, row)


def obtener_cliente_por_id(db, cliente_id):
    row = db.execute(text("SELECT * FROM clientes WHERE id = :id"), {"id": cliente_id}).mappings().fetchone()
    if not row:
        return None
    return _cliente_con_vencimiento(db, row)


class Horario:
    def __init__(self, row, cupo_disponible, inscripciones=None, fecha_clase=None):
        self.id = row["id"]
        self.dia_semana = row["dia_semana"]
        self.hora_inicio = row["hora_inicio"]
        self.hora_fin = row["hora_fin"]
        self.etiqueta = row["etiqueta"]
        self.cupo_maximo = row["cupo_maximo"]
        self.cupo_disponible = cupo_disponible
        self.inscripciones = inscripciones or []
        self.activo = bool(row["activo"])
        self.fecha_clase = fecha_clase

    @property
    def cerrado_por_tiempo(self):
        if not self.fecha_clase:
            return False
        return reserva_cerrada_por_tiempo(self.fecha_clase, self.hora_inicio)


def _cupo_disponible(db, horario_id, cupo_maximo, semana):
    usados = db.execute(
        text("SELECT COUNT(*) AS c FROM inscripciones WHERE horario_id = :hid AND activa = :activa AND semana = :semana"),
        {"hid": horario_id, "activa": True, "semana": semana},
    ).mappings().fetchone()["c"]
    return cupo_maximo - usados


def listar_horarios(db, dia_semana=None, con_inscripciones=False, solo_activos=True, semana=None):
    semana = semana or inicio_semana(hoy())
    query = "SELECT * FROM horarios"
    conditions = []
    params = {}
    if dia_semana:
        conditions.append("dia_semana = :dia")
        params["dia"] = dia_semana
    if solo_activos:
        conditions.append("activo = :activo")
        params["activo"] = True
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY CASE dia_semana " + " ".join(
        f"WHEN '{d}' THEN {i}" for i, d in enumerate(DIAS)
    ) + " END, hora_inicio"
    rows = db.execute(text(query), params).mappings().fetchall()
    horarios = []
    for row in rows:
        cupo = _cupo_disponible(db, row["id"], row["cupo_maximo"], semana)
        inscripciones = []
        if con_inscripciones:
            inscripciones = listar_inscripciones_de_horario(db, row["id"], semana)
        horarios.append(Horario(row, cupo, inscripciones, fecha_de_clase(semana, row["dia_semana"])))
    return horarios


class Inscripcion:
    def __init__(self, row, cliente, horario=None):
        self.id = row["id"]
        self.cliente_id = row["cliente_id"]
        self.horario_id = row["horario_id"]
        self.activa = bool(row["activa"])
        self.cliente = cliente
        self.horario = horario


def listar_inscripciones_de_horario(db, horario_id, semana):
    rows = db.execute(
        text(
            "SELECT i.*, c.id AS c_id, c.nombre AS c_nombre, c.dni AS c_dni, c.telefono AS c_telefono, "
            "c.email AS c_email, c.activo AS c_activo "
            "FROM inscripciones i JOIN clientes c ON c.id = i.cliente_id "
            "WHERE i.horario_id = :hid AND i.semana = :semana"
        ),
        {"hid": horario_id, "semana": semana},
    ).mappings().fetchall()
    result = []
    for row in rows:
        cliente = Cliente({
            "id": row["c_id"], "nombre": row["c_nombre"], "dni": row["c_dni"],
            "telefono": row["c_telefono"], "email": row["c_email"], "activo": row["c_activo"],
        })
        result.append(Inscripcion(row, cliente))
    return result


def listar_inscripciones_de_cliente(db, cliente_id, semana):
    rows = db.execute(
        text(
            "SELECT i.*, h.*, i.id AS i_id FROM inscripciones i JOIN horarios h ON h.id = i.horario_id "
            "WHERE i.cliente_id = :cid AND i.activa = :activa AND i.semana = :semana "
            "ORDER BY CASE h.dia_semana " + " ".join(f"WHEN '{d}' THEN {n}" for n, d in enumerate(DIAS)) +
            " END, h.hora_inicio"
        ),
        {"cid": cliente_id, "activa": True, "semana": semana},
    ).mappings().fetchall()
    result = []
    for row in rows:
        horario_row = {
            "id": row["horario_id"], "dia_semana": row["dia_semana"], "hora_inicio": row["hora_inicio"],
            "hora_fin": row["hora_fin"], "etiqueta": row["etiqueta"], "cupo_maximo": row["cupo_maximo"],
            "activo": row["activo"],
        }
        horario = Horario(horario_row, cupo_disponible=None)
        insc_row = {"id": row["i_id"], "cliente_id": row["cliente_id"], "horario_id": row["horario_id"], "activa": row["activa"]}
        result.append(Inscripcion(insc_row, cliente=None, horario=horario))
    return result


class Pago:
    def __init__(self, row, cliente):
        self.id = row["id"]
        self.cliente = cliente
        self.fecha_pago = _to_date(row["fecha_pago"])
        self.monto = row["monto"]
        self.dias_validez = row["dias_validez"]
        self.fecha_vencimiento = _to_date(row["fecha_vencimiento"])
        self.nota = row["nota"]
        self.cupos_totales = row["cupos_totales"]


def listar_pagos(db, limit=20):
    rows = db.execute(
        text(
            "SELECT p.*, c.id AS c_id, c.nombre AS c_nombre, c.dni AS c_dni, c.telefono AS c_telefono, "
            "c.email AS c_email, c.activo AS c_activo, c.objetivo_semanal AS c_objetivo_semanal "
            "FROM pagos p JOIN clientes c ON c.id = p.cliente_id ORDER BY p.fecha_pago DESC, p.id DESC LIMIT :limit"
        ),
        {"limit": limit},
    ).mappings().fetchall()
    result = []
    for row in rows:
        cliente = Cliente({
            "id": row["c_id"], "nombre": row["c_nombre"], "dni": row["c_dni"],
            "telefono": row["c_telefono"], "email": row["c_email"], "activo": row["c_activo"],
            "objetivo_semanal": row["c_objetivo_semanal"],
        })
        result.append(Pago(row, cliente))
    return result


class Plan:
    def __init__(self, row):
        self.id = row["id"]
        self.nombre = row["nombre"]
        self.precio = row["precio"]


def listar_planes(db):
    rows = db.execute(text("SELECT * FROM planes ORDER BY precio")).mappings().fetchall()
    return [Plan(r) for r in rows]


class Config:
    def __init__(self, row):
        self.alias_mp = row["alias_mp"]
        self.whatsapp_numero = row["whatsapp_numero"]


def obtener_config(db):
    row = db.execute(text("SELECT * FROM configuracion WHERE id = 1")).mappings().fetchone()
    return Config(row)


def construir_whatsapp_link(numero, mensaje):
    if not numero:
        return None
    numero_limpio = "".join(ch for ch in numero if ch.isdigit())
    if not numero_limpio:
        return None
    return f"https://wa.me/{numero_limpio}?text={quote(mensaje)}"


class Entrenamiento:
    def __init__(self, row):
        self.id = row["id"]
        self.fecha = _to_date(row["fecha"])
        self.contenido = row["contenido"]


def obtener_entrenamiento(db, fecha):
    row = db.execute(text("SELECT * FROM entrenamientos WHERE fecha = :fecha"), {"fecha": fecha}).mappings().fetchone()
    return Entrenamiento(row) if row else None


def guardar_entrenamiento(db, fecha, contenido):
    existente = obtener_entrenamiento(db, fecha)
    if existente:
        db.execute(
            text("UPDATE entrenamientos SET contenido = :contenido WHERE fecha = :fecha"),
            {"contenido": contenido, "fecha": fecha},
        )
    else:
        db.execute(
            text("INSERT INTO entrenamientos (fecha, contenido) VALUES (:fecha, :contenido)"),
            {"fecha": fecha, "contenido": contenido},
        )
    db.commit()


# ---------- Notificaciones ----------

class Notificacion:
    def __init__(self, row):
        self.id = row["id"]
        self.mensaje = row["mensaje"]
        self.leida = bool(row["leida"])
        self.fecha = _to_date(row["fecha"])


def crear_notificacion(db, cliente_id, mensaje):
    db.execute(
        text("INSERT INTO notificaciones (cliente_id, mensaje, leida, fecha) VALUES (:cid, :mensaje, :leida, :fecha)"),
        {"cid": cliente_id, "mensaje": mensaje, "leida": False, "fecha": hoy()},
    )


def listar_notificaciones_no_leidas(db, cliente_id):
    rows = db.execute(
        text(
            "SELECT * FROM notificaciones WHERE cliente_id = :cid AND leida = :leida ORDER BY id DESC"
        ),
        {"cid": cliente_id, "leida": False},
    ).mappings().fetchall()
    return [Notificacion(r) for r in rows]


def marcar_notificaciones_leidas(db, cliente_id):
    db.execute(
        text("UPDATE notificaciones SET leida = :leida WHERE cliente_id = :cid"),
        {"leida": True, "cid": cliente_id},
    )
    db.commit()


# ---------- Progreso de fuerza (1RM) ----------

# ---------- Racha de asistencia ----------

def guardar_objetivo_semanal(db, cliente_id, objetivo):
    db.execute(
        text("UPDATE clientes SET objetivo_semanal = :obj WHERE id = :cid"),
        {"obj": objetivo, "cid": cliente_id},
    )
    db.commit()


def fechas_asistencia_cliente(db, cliente_id):
    rows = db.execute(
        text(
            "SELECT a.fecha FROM asistencias a "
            "JOIN inscripciones i ON i.id = a.inscripcion_id "
            "WHERE i.cliente_id = :cid"
        ),
        {"cid": cliente_id},
    ).mappings().fetchall()
    return [_to_date(r["fecha"]) for r in rows]


def calcular_racha(fechas, objetivo):
    if not objetivo or objetivo <= 0:
        return None

    def inicio_semana_lv(f):
        return f - timedelta(days=f.weekday())

    conteo_por_semana = {}
    for f in fechas:
        semana = inicio_semana_lv(f)
        conteo_por_semana[semana] = conteo_por_semana.get(semana, 0) + 1

    hoy_d = hoy()
    semana_actual = inicio_semana_lv(hoy_d)
    semana_revisar = semana_actual - timedelta(days=7)
    racha = 0
    while conteo_por_semana.get(semana_revisar, 0) >= objetivo:
        racha += 1
        semana_revisar -= timedelta(days=7)

    return {
        "racha": racha,
        "objetivo": objetivo,
        "semana_actual_count": conteo_por_semana.get(semana_actual, 0),
    }


def calcular_1rm(kilos, repeticiones):
    # Fórmula de Brzycki. Con 1 repetición el 1RM es directamente el peso levantado.
    if repeticiones <= 1:
        return kilos
    return kilos / (1.0278 - (0.0278 * repeticiones))


def listar_ejercicios_cliente(db, cliente_id):
    rows = db.execute(
        text("SELECT DISTINCT ejercicio FROM registros_ejercicio WHERE cliente_id = :cid"),
        {"cid": cliente_id},
    ).mappings().fetchall()
    propios = [r["ejercicio"] for r in rows]
    ordenados = list(EJERCICIOS_BASE)
    for e in propios:
        if e not in ordenados:
            ordenados.append(e)
    return ordenados


def guardar_registro_ejercicio(db, cliente_id, ejercicio, series, repeticiones, porcentaje, kilos):
    rm = calcular_1rm(kilos, repeticiones)
    maximo_previo = db.execute(
        text("SELECT MAX(rm_estimado) AS m FROM registros_ejercicio WHERE cliente_id = :cid AND ejercicio = :ej"),
        {"cid": cliente_id, "ej": ejercicio},
    ).mappings().fetchone()["m"]
    es_pr = maximo_previo is None or rm > maximo_previo
    db.execute(
        text(
            "INSERT INTO registros_ejercicio (cliente_id, ejercicio, fecha, series, repeticiones, porcentaje, kilos, rm_estimado) "
            "VALUES (:cid, :ej, :f, :s, :r, :p, :k, :rm)"
        ),
        {
            "cid": cliente_id, "ej": ejercicio, "f": hoy(), "s": series, "r": repeticiones,
            "p": porcentaje, "k": kilos, "rm": rm,
        },
    )
    db.commit()
    return rm, es_pr


def historial_1rm(db, cliente_id, por_ejercicio=6):
    ejercicios = listar_ejercicios_cliente(db, cliente_id)
    resultado = {}
    for ej in ejercicios:
        rows = db.execute(
            text(
                "SELECT fecha, rm_estimado FROM registros_ejercicio "
                "WHERE cliente_id = :cid AND ejercicio = :ej ORDER BY fecha DESC, id DESC LIMIT :n"
            ),
            {"cid": cliente_id, "ej": ej, "n": por_ejercicio},
        ).mappings().fetchall()
        if rows:
            puntos = [{"fecha": _to_date(r["fecha"]), "rm": r["rm_estimado"]} for r in reversed(rows)]
            resultado[ej] = puntos
    return resultado


def _fechas_unificadas(historial):
    todas = sorted({p["fecha"] for puntos in historial.values() for p in puntos})
    return [f.strftime("%d/%m") for f in todas], todas


def series_grafico_1rm(historial):
    labels, fechas = _fechas_unificadas(historial)
    series = []
    for ejercicio, puntos in historial.items():
        por_fecha = {p["fecha"]: round(p["rm"], 1) for p in puntos}
        series.append({"ejercicio": ejercicio, "datos": [por_fecha.get(f) for f in fechas]})
    return labels, series


def formatear_telefono_whatsapp(telefono):
    # Normaliza a formato E.164 para Argentina: 54 <código de área><número>, SIN el 9.
    # A diferencia de cómo se marcan llamadas, la API de WhatsApp para números
    # argentinos no lleva el 9 después del 54 (confirmado con el propio ejemplo
    # de código que genera Meta al probar el número de destino).
    # Asume que el teléfono está cargado sin 0 inicial y sin "15" (formato moderno).
    # Si el "15" está en el medio del número no se puede sacar de forma confiable
    # sin saber el largo del código de área.
    if not telefono:
        return None
    digitos = "".join(ch for ch in telefono if ch.isdigit())
    if not digitos:
        return None
    if digitos.startswith("549"):
        # Alguien lo cargó con el 9 de más (formato de llamada); lo sacamos.
        return "54" + digitos[3:]
    if digitos.startswith("54"):
        return digitos
    if digitos.startswith("0"):
        digitos = digitos[1:]
    return "54" + digitos


def enviar_whatsapp_pago(telefono, nombre, monto, fecha_vencimiento):
    if not (WHATSAPP_TOKEN and WHATSAPP_PHONE_ID):
        return False, "WhatsApp no está configurado (faltan WHATSAPP_TOKEN / WHATSAPP_PHONE_ID)."
    numero = formatear_telefono_whatsapp(telefono)
    if not numero:
        return False, "El cliente no tiene teléfono cargado."
    try:
        resp = requests.post(
            f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": numero,
                "type": "template",
                "template": {
                    "name": WHATSAPP_TEMPLATE_NAME,
                    "language": {"code": WHATSAPP_TEMPLATE_LANG},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "parameter_name": "nombre", "text": nombre},
                            {"type": "text", "parameter_name": "monto", "text": f"{monto:,.2f}"},
                            {"type": "text", "parameter_name": "vencimiento", "text": fecha_vencimiento.strftime("%d/%m/%Y")},
                        ],
                    }],
                },
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            return False, f"WhatsApp API respondió {resp.status_code}: {resp.text[:200]}"
        return True, None
    except requests.RequestException as exc:
        return False, str(exc)


# ---------- Auth admin ----------

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


# ---------- Identificación de clientes (sin contraseña, solo DNI) ----------

def identificar_cliente(cliente):
    session["cliente_dni"] = cliente.dni
    session.permanent = True


def cliente_de_sesion(db):
    dni = session.get("cliente_dni")
    if not dni:
        return None
    cliente = obtener_cliente_por_dni(db, dni)
    if not cliente or not cliente.activo:
        session.pop("cliente_dni", None)
        return None
    return cliente


# ---------- Rutas públicas ----------

@app.route("/sw.js")
def service_worker():
    respuesta = send_from_directory(app.static_folder, "sw.js")
    respuesta.headers["Service-Worker-Allowed"] = "/"
    respuesta.headers["Cache-Control"] = "no-cache"
    return respuesta


@app.route("/")
def index():
    db = get_db()
    horarios = listar_horarios(db, con_inscripciones=True)
    horarios_por_dia = {}
    for h in horarios:
        horarios_por_dia.setdefault(h.dia_semana, []).append(h)
    config = obtener_config(db)
    whatsapp_link = construir_whatsapp_link(
        config.whatsapp_numero, "Hola! Te escribo para enviarte el comprobante de mi pago del abono."
    )
    cliente_actual = cliente_de_sesion(db)
    notificaciones = listar_notificaciones_no_leidas(db, cliente_actual.id) if cliente_actual else []
    if notificaciones:
        marcar_notificaciones_leidas(db, cliente_actual.id)
    return render_template(
        "index.html", horarios_por_dia=horarios_por_dia, cliente_actual=cliente_actual,
        planes=listar_planes(db), config=config, whatsapp_link=whatsapp_link,
        entrenamiento_hoy=obtener_entrenamiento(db, hoy()), notificaciones=notificaciones,
    )


@app.route("/salir-cliente")
def salir_cliente():
    session.pop("cliente_dni", None)
    return redirect(url_for("index"))


@app.route("/reservar", methods=["POST"])
def reservar():
    db = get_db()
    dni = request.form.get("dni", "").strip() or session.get("cliente_dni", "")
    horario_id = request.form["horario_id"]
    semana_actual = inicio_semana(hoy())

    if not dni:
        flash("Necesitás ingresar tu DNI para reservar.", "error")
        return redirect(url_for("index"))

    cliente = obtener_cliente_por_dni(db, dni)
    if not cliente or not cliente.activo:
        flash("No encontramos un cliente activo con ese DNI. Consultá con el gimnasio.", "error")
        return redirect(url_for("index"))
    identificar_cliente(cliente)

    if cliente.abono_impago:
        flash("No se permite la reserva por abono impago o vencido.", "error")
        return redirect(url_for("index"))
    if cliente.sin_cupos:
        flash("No se permite la reserva: ya usaste todos los cupos de tu abono.", "error")
        return redirect(url_for("index"))

    horario_row = db.execute(text("SELECT * FROM horarios WHERE id = :hid"), {"hid": horario_id}).mappings().fetchone()
    if not horario_row or not horario_row["activo"]:
        flash("Ese horario ya no está disponible.", "error")
        return redirect(url_for("index"))

    fecha_clase = fecha_de_clase(semana_actual, horario_row["dia_semana"])
    if reserva_cerrada_por_tiempo(fecha_clase, horario_row["hora_inicio"]):
        flash(
            f"Ya no se puede reservar esta clase: falta menos de {MINUTOS_LIMITE_RESERVA} minutos para que empiece.",
            "error",
        )
        return redirect(url_for("index"))
    if fecha_clase > cliente.proximo_vencimiento:
        flash(
            f"No se permite la reserva: esa clase es el {fecha_clase.strftime('%d/%m/%Y')}, "
            f"posterior al vencimiento de tu abono ({cliente.proximo_vencimiento.strftime('%d/%m/%Y')}).",
            "error",
        )
        return redirect(url_for("index"))

    ya_inscripto = db.execute(
        text("SELECT 1 FROM inscripciones WHERE cliente_id = :cid AND horario_id = :hid AND activa = :activa AND semana = :semana"),
        {"cid": cliente.id, "hid": horario_id, "activa": True, "semana": semana_actual},
    ).fetchone()
    if ya_inscripto:
        flash("Ya tenés una reserva en ese horario esta semana.", "error")
        return redirect(url_for("index"))

    cupo = _cupo_disponible(db, horario_id, horario_row["cupo_maximo"], semana_actual)
    if cupo <= 0:
        flash("Ese horario ya no tiene cupo disponible.", "error")
        return redirect(url_for("index"))

    existente = db.execute(
        text("SELECT id FROM inscripciones WHERE cliente_id = :cid AND horario_id = :hid"),
        {"cid": cliente.id, "hid": horario_id},
    ).mappings().fetchone()
    if existente:
        db.execute(
            text("UPDATE inscripciones SET activa = :activa, semana = :semana WHERE id = :id"),
            {"activa": True, "semana": semana_actual, "id": existente["id"]},
        )
    else:
        db.execute(
            text("INSERT INTO inscripciones (cliente_id, horario_id, activa, semana) VALUES (:cid, :hid, :activa, :semana)"),
            {"cid": cliente.id, "hid": horario_id, "activa": True, "semana": semana_actual},
        )
    db.commit()
    flash("¡Turno reservado!", "success")
    return redirect(url_for("index"))


@app.route("/mi-cuenta")
def mi_cuenta():
    db = get_db()
    dni = request.args.get("dni", "").strip() or session.get("cliente_dni", "")
    cliente = None
    inscripciones = []
    notificaciones = []
    ejercicios = list(EJERCICIOS_BASE)
    grafico_labels, grafico_series = [], []
    racha = None
    buscado = bool(dni)
    if dni:
        cliente = obtener_cliente_por_dni(db, dni)
        if cliente:
            identificar_cliente(cliente)
            inscripciones = listar_inscripciones_de_cliente(db, cliente.id, inicio_semana(hoy()))
            notificaciones = listar_notificaciones_no_leidas(db, cliente.id)
            if notificaciones:
                marcar_notificaciones_leidas(db, cliente.id)
            ejercicios = listar_ejercicios_cliente(db, cliente.id)
            historial = historial_1rm(db, cliente.id)
            grafico_labels, grafico_series = series_grafico_1rm(historial)
            racha = calcular_racha(fechas_asistencia_cliente(db, cliente.id), cliente.objetivo_semanal)
    config = obtener_config(db)
    mensaje = (
        f"Hola! Soy {cliente.nombre} (DNI {cliente.dni}), te envío el comprobante de mi pago."
        if cliente else "Hola! Te escribo para enviarte el comprobante de mi pago del abono."
    )
    whatsapp_link = construir_whatsapp_link(config.whatsapp_numero, mensaje)
    return render_template(
        "mi_cuenta.html", dni=dni, cliente=cliente, inscripciones=inscripciones, buscado=buscado,
        racha=racha,
        planes=listar_planes(db), config=config, whatsapp_link=whatsapp_link, notificaciones=notificaciones,
        ejercicios=ejercicios, grafico_labels=grafico_labels, grafico_series=grafico_series,
    )


@app.route("/mi-cuenta/objetivo", methods=["POST"])
def mi_cuenta_objetivo():
    db = get_db()
    dni = request.form.get("dni", "").strip()
    cliente = obtener_cliente_por_dni(db, dni)
    if not cliente:
        flash("No pudimos identificar tu cuenta para guardar el objetivo.", "error")
        return redirect(url_for("mi_cuenta"))
    try:
        objetivo = int(request.form["objetivo_semanal"])
    except (KeyError, ValueError):
        flash("El objetivo semanal tiene que ser un número.", "error")
        return redirect(url_for("mi_cuenta", dni=dni))
    if objetivo <= 0 or objetivo > 5:
        flash("El objetivo semanal tiene que ser entre 1 y 5 (los días hábiles del gimnasio).", "error")
        return redirect(url_for("mi_cuenta", dni=dni))
    guardar_objetivo_semanal(db, cliente.id, objetivo)
    flash(f"Objetivo actualizado: {objetivo} veces por semana.", "success")
    return redirect(url_for("mi_cuenta", dni=dni))


@app.route("/mi-cuenta/registro", methods=["POST"])
def mi_cuenta_registro():
    db = get_db()
    dni = request.form.get("dni", "").strip()
    cliente = obtener_cliente_por_dni(db, dni)
    if not cliente:
        flash("No pudimos identificar tu cuenta para guardar el registro.", "error")
        return redirect(url_for("mi_cuenta"))

    ejercicio = (request.form.get("ejercicio_otro") or request.form.get("ejercicio") or "").strip()
    try:
        series = int(request.form["series"])
        repeticiones = int(request.form["repeticiones"])
        kilos = float(request.form["kilos"])
        porcentaje_raw = request.form.get("porcentaje", "").strip()
        porcentaje = float(porcentaje_raw) if porcentaje_raw else None
    except (KeyError, ValueError):
        flash("Revisá los datos del registro: series, repeticiones y kilos tienen que ser números.", "error")
        return redirect(url_for("mi_cuenta", dni=dni))

    if not ejercicio or series <= 0 or repeticiones <= 0 or kilos <= 0:
        flash("Completá ejercicio, series, repeticiones y kilos para guardar el registro.", "error")
        return redirect(url_for("mi_cuenta", dni=dni))

    rm, es_pr = guardar_registro_ejercicio(db, cliente.id, ejercicio, series, repeticiones, porcentaje, kilos)
    if es_pr:
        flash(f"🏆 ¡Nuevo PR en {ejercicio}! 1RM estimado: {rm:.1f} kg.", "pr")
    else:
        flash(f"Registro de {ejercicio} guardado.", "success")
    return redirect(url_for("mi_cuenta", dni=dni))


@app.route("/mi-cuenta/cancelar/<int:inscripcion_id>", methods=["POST"])
def mi_cuenta_cancelar(inscripcion_id):
    db = get_db()
    dni = request.form.get("dni", "").strip()
    row = db.execute(
        text("SELECT i.id, c.dni FROM inscripciones i JOIN clientes c ON c.id = i.cliente_id WHERE i.id = :id"),
        {"id": inscripcion_id},
    ).mappings().fetchone()
    if row and row["dni"] == dni:
        db.execute(text("UPDATE inscripciones SET activa = :activa WHERE id = :id"), {"activa": False, "id": inscripcion_id})
        db.commit()
        flash("Turno cancelado.", "success")
    else:
        flash("No pudimos cancelar ese turno.", "error")
    return redirect(url_for("mi_cuenta", dni=dni))


# ---------- Auth admin ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Contraseña incorrecta.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


MESES_CORTOS = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
MESES_LARGOS = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


def _ultimo_dia_mes(anio, mes):
    if mes == 12:
        return date(anio, 12, 31)
    return date(anio, mes + 1, 1) - timedelta(days=1)


def _primer_pago(db):
    row = db.execute(text("SELECT MIN(fecha_pago) AS f FROM pagos")).mappings().fetchone()
    return _to_date(row["f"]) if row and row["f"] else None


def resumen_ganancias(db, periodo="mes", anio_mes=None):
    if periodo not in ("semana", "mes", "anio"):
        periodo = "mes"
    hoy_d = hoy()
    buckets = []
    mes_actual = {"anio": hoy_d.year, "mes": hoy_d.month}

    if periodo == "semana":
        if anio_mes:
            yy, mm = anio_mes
        else:
            yy, mm = hoy_d.year, hoy_d.month
        ultimo_dia = _ultimo_dia_mes(yy, mm).day
        dia = 1
        n = 1
        while dia <= ultimo_dia:
            fin_semana = min(dia + 6, ultimo_dia)
            buckets.append({
                "inicio": date(yy, mm, dia), "fin": date(yy, mm, fin_semana),
                "label": f"Sem {n}",
            })
            dia += 7
            n += 1
        mes_actual = {"anio": yy, "mes": mm}
    elif periodo == "anio":
        for i in range(4, -1, -1):
            anio_b = hoy_d.year - i
            buckets.append({"inicio": date(anio_b, 1, 1), "fin": date(anio_b, 12, 31), "label": str(anio_b)})
    else:
        y, m = hoy_d.year, hoy_d.month
        for i in range(11, -1, -1):
            mm, yy = m - i, y
            while mm <= 0:
                mm += 12
                yy -= 1
            buckets.append({
                "inicio": date(yy, mm, 1), "fin": _ultimo_dia_mes(yy, mm),
                "label": f"{MESES_CORTOS[mm - 1]} {str(yy)[2:]}",
            })

    for b in buckets:
        b["total"] = 0.0

    desde = buckets[0]["inicio"] if buckets else hoy_d
    hasta = buckets[-1]["fin"] if buckets else hoy_d
    rows = db.execute(
        text("SELECT fecha_pago, monto FROM pagos WHERE fecha_pago >= :desde AND fecha_pago <= :hasta"),
        {"desde": desde, "hasta": hasta},
    ).mappings().fetchall()
    for r in rows:
        f = _to_date(r["fecha_pago"])
        for b in buckets:
            if b["inicio"] <= f <= b["fin"]:
                b["total"] += r["monto"]
                break

    total_periodo = sum(b["total"] for b in buckets)
    resultado = {
        "periodo": periodo, "buckets": buckets,
        "total_actual": total_periodo if periodo == "semana" else (buckets[-1]["total"] if buckets else 0.0),
    }

    if periodo == "semana":
        primer_pago = _primer_pago(db)
        mes_minimo = (primer_pago.year, primer_pago.month) if primer_pago else (hoy_d.year, hoy_d.month)
        mes_maximo = (hoy_d.year, hoy_d.month)
        anio_actual, mes_num = mes_actual["anio"], mes_actual["mes"]
        anio_prev, mes_prev = (anio_actual - 1, 12) if mes_num == 1 else (anio_actual, mes_num - 1)
        anio_next, mes_next = (anio_actual + 1, 1) if mes_num == 12 else (anio_actual, mes_num + 1)
        resultado["mes_actual"] = {"anio": anio_actual, "mes": mes_num, "label": f"{MESES_LARGOS[mes_num - 1]} {anio_actual}"}
        resultado["mes_prev"] = {"anio": anio_prev, "mes": mes_prev} if (anio_prev, mes_prev) >= mes_minimo else None
        resultado["mes_next"] = {"anio": anio_next, "mes": mes_next} if (anio_next, mes_next) <= mes_maximo else None

    return resultado


# ---------- Admin: dashboard ----------

@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    clientes = listar_clientes(db, solo_activos=True)
    vencidos = [c for c in clientes if c.abono_vencido]
    hoy_nombre = DIAS[hoy().weekday()]
    turnos_hoy = listar_horarios(db, dia_semana=hoy_nombre)
    periodo = request.args.get("periodo", "mes")
    anio_mes = None
    if periodo == "semana":
        try:
            anio_mes = (int(request.args["anio"]), int(request.args["mes"]))
        except (KeyError, ValueError):
            anio_mes = None
    ganancias = resumen_ganancias(db, periodo, anio_mes)
    return render_template(
        "admin/dashboard.html",
        total_clientes=len(clientes),
        vencidos=vencidos,
        hoy_nombre=hoy_nombre,
        turnos_hoy=turnos_hoy,
        ganancias=ganancias,
    )


# ---------- Admin: clientes ----------

@app.route("/admin/clientes")
@admin_required
def admin_clientes():
    db = get_db()
    return render_template("admin/clientes.html", clientes=listar_clientes(db))


@app.route("/admin/clientes/nuevo", methods=["GET", "POST"])
@admin_required
def admin_cliente_nuevo():
    if request.method == "POST":
        db = get_db()
        try:
            db.execute(
                text("INSERT INTO clientes (nombre, dni, telefono, email, activo) VALUES (:nombre, :dni, :telefono, :email, :activo)"),
                {
                    "nombre": request.form["nombre"], "dni": request.form["dni"],
                    "telefono": request.form.get("telefono"), "email": request.form.get("email"), "activo": True,
                },
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            flash("Ya existe un cliente con ese DNI.", "error")
            return render_template("admin/cliente_form.html", cliente=None)
        return redirect(url_for("admin_clientes"))
    return render_template("admin/cliente_form.html", cliente=None)


@app.route("/admin/clientes/<int:cliente_id>/editar", methods=["GET", "POST"])
@admin_required
def admin_cliente_editar(cliente_id):
    db = get_db()
    if request.method == "POST":
        db.execute(
            text(
                "UPDATE clientes SET nombre = :nombre, dni = :dni, telefono = :telefono, "
                "email = :email, activo = :activo WHERE id = :id"
            ),
            {
                "nombre": request.form["nombre"], "dni": request.form["dni"], "telefono": request.form.get("telefono"),
                "email": request.form.get("email"), "activo": bool(request.form.get("activo")), "id": cliente_id,
            },
        )
        db.commit()
        return redirect(url_for("admin_clientes"))
    cliente = obtener_cliente_por_id(db, cliente_id)
    return render_template("admin/cliente_form.html", cliente=cliente)


@app.route("/admin/clientes/<int:cliente_id>/eliminar", methods=["POST"])
@admin_required
def admin_cliente_eliminar(cliente_id):
    db = get_db()
    db.execute(text("DELETE FROM clientes WHERE id = :id"), {"id": cliente_id})
    db.commit()
    return redirect(url_for("admin_clientes"))


# ---------- Admin: pagos ----------

@app.route("/admin/pagos", methods=["GET", "POST"])
@admin_required
def admin_pagos():
    db = get_db()
    if request.method == "POST":
        fecha_pago = date.fromisoformat(request.form["fecha_pago"])
        dias_validez = int(request.form["dias_validez"])
        fecha_vencimiento = fecha_pago + timedelta(days=dias_validez)
        cupos_raw = request.form.get("cupos_totales", "").strip()
        cliente_id = request.form["cliente_id"]
        monto = float(request.form["monto"])
        db.execute(
            text(
                "INSERT INTO pagos (cliente_id, fecha_pago, monto, dias_validez, fecha_vencimiento, nota, cupos_totales) "
                "VALUES (:cid, :fecha_pago, :monto, :dias_validez, :fecha_vencimiento, :nota, :cupos_totales)"
            ),
            {
                "cid": cliente_id, "fecha_pago": fecha_pago, "monto": monto,
                "dias_validez": dias_validez, "fecha_vencimiento": fecha_vencimiento, "nota": request.form.get("nota"),
                "cupos_totales": int(cupos_raw) if cupos_raw else None,
            },
        )
        crear_notificacion(
            db, cliente_id,
            f"Registramos tu pago de ${monto:,.2f}. Tu abono queda activo hasta el "
            f"{fecha_vencimiento.strftime('%d/%m/%Y')}.",
        )
        db.commit()

        cliente = obtener_cliente_por_id(db, cliente_id)
        if cliente:
            enviado, error = enviar_whatsapp_pago(cliente.telefono, cliente.nombre, monto, fecha_vencimiento)
            if not enviado:
                flash(f"Pago registrado. Aviso in-app enviado, pero el WhatsApp no se pudo mandar: {error}", "warning")

        return redirect(url_for("admin_pagos"))
    return render_template(
        "admin/pagos.html",
        clientes=listar_clientes(db, solo_activos=True),
        pagos=listar_pagos(db),
        today=hoy().isoformat(),
    )


# ---------- Admin: horarios ----------

@app.route("/admin/horarios", methods=["GET", "POST"])
@admin_required
def admin_horarios():
    db = get_db()
    if request.method == "POST":
        db.execute(
            text(
                "INSERT INTO horarios (dia_semana, hora_inicio, hora_fin, etiqueta, cupo_maximo, activo) "
                "VALUES (:dia, :inicio, :fin, :etiqueta, :cupo, :activo)"
            ),
            {
                "dia": request.form["dia_semana"], "inicio": request.form["hora_inicio"], "fin": request.form["hora_fin"],
                "etiqueta": request.form.get("etiqueta"), "cupo": int(request.form["cupo_maximo"]), "activo": True,
            },
        )
        db.commit()
        return redirect(url_for("admin_horarios"))
    return render_template("admin/horarios.html", horarios=listar_horarios(db, solo_activos=False), dias=DIAS)


@app.route("/admin/horarios/<int:horario_id>/cancelar", methods=["POST"])
@admin_required
def admin_horario_cancelar(horario_id):
    db = get_db()
    row = db.execute(text("SELECT activo FROM horarios WHERE id = :id"), {"id": horario_id}).mappings().fetchone()
    if row:
        db.execute(text("UPDATE horarios SET activo = :activo WHERE id = :id"), {"activo": not row["activo"], "id": horario_id})
        db.commit()
    return redirect(url_for("admin_horarios"))


@app.route("/admin/horarios/<int:horario_id>/eliminar", methods=["POST"])
@admin_required
def admin_horario_eliminar(horario_id):
    db = get_db()
    db.execute(text("DELETE FROM horarios WHERE id = :id"), {"id": horario_id})
    db.commit()
    return redirect(url_for("admin_horarios"))


# ---------- Admin: asistencia ----------

@app.route("/admin/asistencia", methods=["GET", "POST"])
@admin_required
def admin_asistencia():
    db = get_db()
    fecha_sel = request.values.get("fecha") or hoy().isoformat()
    fecha_dt = date.fromisoformat(fecha_sel)
    dia_nombre = DIAS[fecha_dt.weekday()]

    if request.method == "POST":
        inscripcion_id = request.form["inscripcion_id"]
        try:
            db.execute(
                text("INSERT INTO asistencias (inscripcion_id, fecha) VALUES (:iid, :fecha)"),
                {"iid": inscripcion_id, "fecha": fecha_dt},
            )
            db.commit()
        except IntegrityError:
            db.rollback()
        return redirect(url_for("admin_asistencia", fecha=fecha_sel))

    horarios = listar_horarios(db, dia_semana=dia_nombre, con_inscripciones=True, semana=inicio_semana(fecha_dt))
    rows = db.execute(text("SELECT inscripcion_id FROM asistencias WHERE fecha = :fecha"), {"fecha": fecha_dt}).mappings().fetchall()
    asistencias_hoy = {r["inscripcion_id"] for r in rows}
    return render_template(
        "admin/asistencia.html",
        horarios=horarios,
        fecha_sel=fecha_sel,
        dia_nombre=dia_nombre,
        asistencias_hoy=asistencias_hoy,
    )


# ---------- Admin: entrenamiento del día ----------

@app.route("/admin/entrenamiento", methods=["GET", "POST"])
@admin_required
def admin_entrenamiento():
    db = get_db()
    fecha_sel = request.values.get("fecha") or hoy().isoformat()
    fecha_dt = date.fromisoformat(fecha_sel)

    if request.method == "POST":
        guardar_entrenamiento(db, fecha_dt, request.form.get("contenido", "").strip())
        return redirect(url_for("admin_entrenamiento", fecha=fecha_sel))

    return render_template(
        "admin/entrenamiento.html",
        fecha_sel=fecha_sel,
        entrenamiento=obtener_entrenamiento(db, fecha_dt),
        es_hoy=fecha_sel == hoy().isoformat(),
    )


# ---------- Admin: configuración (pago y contacto) ----------

@app.route("/admin/config", methods=["GET", "POST"])
@admin_required
def admin_config():
    db = get_db()
    if request.method == "POST":
        db.execute(
            text("UPDATE configuracion SET alias_mp = :alias, whatsapp_numero = :whatsapp WHERE id = 1"),
            {"alias": request.form.get("alias_mp", "").strip(), "whatsapp": request.form.get("whatsapp_numero", "").strip()},
        )
        db.commit()
        return redirect(url_for("admin_config"))
    return render_template("admin/config.html", config=obtener_config(db), planes=listar_planes(db))


@app.route("/admin/config/planes", methods=["POST"])
@admin_required
def admin_plan_nuevo():
    db = get_db()
    db.execute(
        text("INSERT INTO planes (nombre, precio) VALUES (:nombre, :precio)"),
        {"nombre": request.form["nombre"], "precio": float(request.form["precio"])},
    )
    db.commit()
    return redirect(url_for("admin_config"))


@app.route("/admin/config/planes/<int:plan_id>/eliminar", methods=["POST"])
@admin_required
def admin_plan_eliminar(plan_id):
    db = get_db()
    db.execute(text("DELETE FROM planes WHERE id = :id"), {"id": plan_id})
    db.commit()
    return redirect(url_for("admin_config"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
