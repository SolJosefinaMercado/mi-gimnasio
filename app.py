import os
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Flask, g, redirect, render_template, request, session, url_for, flash
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
    with engine.begin() as conn:
        # Migraciones livianas para bases ya desplegadas antes de estas columnas existir.
        if "activo" not in horarios_cols:
            conn.execute(text("ALTER TABLE horarios ADD COLUMN activo BOOLEAN NOT NULL DEFAULT TRUE"))
        if "semana" not in inscripciones_cols:
            conn.execute(text("ALTER TABLE inscripciones ADD COLUMN semana DATE"))
        if "cupos_totales" not in pagos_cols:
            conn.execute(text("ALTER TABLE pagos ADD COLUMN cupos_totales INTEGER"))
        existe = conn.execute(text("SELECT 1 FROM configuracion WHERE id = 1")).fetchone()
        if not existe:
            conn.execute(text("INSERT INTO configuracion (id, alias_mp, whatsapp_numero) VALUES (1, '', '')"))


def inicio_semana(fecha):
    # La "semana" de cupos arranca el sábado a las 00:00 (hora Argentina), no el lunes:
    # así lo que se reserva el fin de semana para el lunes cae en el mismo ciclo.
    dias_desde_sabado = (fecha.weekday() - 5) % 7
    return fecha - timedelta(days=dias_desde_sabado)


# ---------- Modelos livianos sobre filas de la base ----------

class Cliente:
    def __init__(self, row, proximo_vencimiento=None):
        self.id = row["id"]
        self.nombre = row["nombre"]
        self.dni = row["dni"]
        self.telefono = row["telefono"]
        self.email = row["email"]
        self.activo = bool(row["activo"])
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
    def __init__(self, row, cupo_disponible, inscripciones=None):
        self.id = row["id"]
        self.dia_semana = row["dia_semana"]
        self.hora_inicio = row["hora_inicio"]
        self.hora_fin = row["hora_fin"]
        self.etiqueta = row["etiqueta"]
        self.cupo_maximo = row["cupo_maximo"]
        self.cupo_disponible = cupo_disponible
        self.inscripciones = inscripciones or []
        self.activo = bool(row["activo"])


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
        horarios.append(Horario(row, cupo, inscripciones))
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
            "c.email AS c_email, c.activo AS c_activo "
            "FROM pagos p JOIN clientes c ON c.id = p.cliente_id ORDER BY p.fecha_pago DESC, p.id DESC LIMIT :limit"
        ),
        {"limit": limit},
    ).mappings().fetchall()
    result = []
    for row in rows:
        cliente = Cliente({
            "id": row["c_id"], "nombre": row["c_nombre"], "dni": row["c_dni"],
            "telefono": row["c_telefono"], "email": row["c_email"], "activo": row["c_activo"],
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
    return render_template(
        "index.html", horarios_por_dia=horarios_por_dia, cliente_actual=cliente_de_sesion(db),
        planes=listar_planes(db), config=config, whatsapp_link=whatsapp_link,
        entrenamiento_hoy=obtener_entrenamiento(db, hoy()),
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
    buscado = bool(dni)
    if dni:
        cliente = obtener_cliente_por_dni(db, dni)
        if cliente:
            identificar_cliente(cliente)
            inscripciones = listar_inscripciones_de_cliente(db, cliente.id, inicio_semana(hoy()))
    config = obtener_config(db)
    mensaje = (
        f"Hola! Soy {cliente.nombre} (DNI {cliente.dni}), te envío el comprobante de mi pago."
        if cliente else "Hola! Te escribo para enviarte el comprobante de mi pago del abono."
    )
    whatsapp_link = construir_whatsapp_link(config.whatsapp_numero, mensaje)
    return render_template(
        "mi_cuenta.html", dni=dni, cliente=cliente, inscripciones=inscripciones, buscado=buscado,
        planes=listar_planes(db), config=config, whatsapp_link=whatsapp_link,
    )


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


# ---------- Admin: dashboard ----------

@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    clientes = listar_clientes(db, solo_activos=True)
    vencidos = [c for c in clientes if c.abono_vencido]
    hoy_nombre = DIAS[hoy().weekday()]
    turnos_hoy = listar_horarios(db, dia_semana=hoy_nombre)
    return render_template(
        "admin/dashboard.html",
        total_clientes=len(clientes),
        vencidos=vencidos,
        hoy_nombre=hoy_nombre,
        turnos_hoy=turnos_hoy,
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
        db.execute(
            text(
                "INSERT INTO pagos (cliente_id, fecha_pago, monto, dias_validez, fecha_vencimiento, nota, cupos_totales) "
                "VALUES (:cid, :fecha_pago, :monto, :dias_validez, :fecha_vencimiento, :nota, :cupos_totales)"
            ),
            {
                "cid": request.form["cliente_id"], "fecha_pago": fecha_pago, "monto": float(request.form["monto"]),
                "dias_validez": dias_validez, "fecha_vencimiento": fecha_vencimiento, "nota": request.form.get("nota"),
                "cupos_totales": int(cupos_raw) if cupos_raw else None,
            },
        )
        db.commit()
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
