import os
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, g, redirect, render_template, request, session, url_for, flash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "gimnasio.db")
SCHEMA = os.path.join(BASE_DIR, "schema.sql")

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    if not os.path.exists(DATABASE):
        db = sqlite3.connect(DATABASE)
        with open(SCHEMA, encoding="utf-8") as f:
            db.executescript(f.read())
        db.commit()
        db.close()


# ---------- Modelos livianos sobre filas de sqlite3 ----------

class Cliente:
    def __init__(self, row, proximo_vencimiento=None):
        self.id = row["id"]
        self.nombre = row["nombre"]
        self.dni = row["dni"]
        self.telefono = row["telefono"]
        self.email = row["email"]
        self.activo = bool(row["activo"])
        self.proximo_vencimiento = proximo_vencimiento

    @property
    def abono_vencido(self):
        return self.proximo_vencimiento is not None and self.proximo_vencimiento < date.today()


def _cliente_con_vencimiento(db, row):
    ultimo = db.execute(
        "SELECT fecha_vencimiento FROM pagos WHERE cliente_id = ? ORDER BY fecha_vencimiento DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    vencimiento = date.fromisoformat(ultimo["fecha_vencimiento"]) if ultimo else None
    return Cliente(row, vencimiento)


def listar_clientes(db, solo_activos=False):
    query = "SELECT * FROM clientes"
    if solo_activos:
        query += " WHERE activo = 1"
    query += " ORDER BY nombre"
    rows = db.execute(query).fetchall()
    return [_cliente_con_vencimiento(db, r) for r in rows]


def obtener_cliente_por_dni(db, dni):
    row = db.execute("SELECT * FROM clientes WHERE dni = ?", (dni,)).fetchone()
    if not row:
        return None
    return _cliente_con_vencimiento(db, row)


def obtener_cliente_por_id(db, cliente_id):
    row = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
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


def _cupo_disponible(db, horario_id, cupo_maximo):
    usados = db.execute(
        "SELECT COUNT(*) AS c FROM inscripciones WHERE horario_id = ? AND activa = 1", (horario_id,)
    ).fetchone()["c"]
    return cupo_maximo - usados


def listar_horarios(db, dia_semana=None, con_inscripciones=False):
    query = "SELECT * FROM horarios"
    params = ()
    if dia_semana:
        query += " WHERE dia_semana = ?"
        params = (dia_semana,)
    query += " ORDER BY CASE dia_semana " + " ".join(
        f"WHEN '{d}' THEN {i}" for i, d in enumerate(DIAS)
    ) + " END, hora_inicio"
    rows = db.execute(query, params).fetchall()
    horarios = []
    for row in rows:
        cupo = _cupo_disponible(db, row["id"], row["cupo_maximo"])
        inscripciones = []
        if con_inscripciones:
            inscripciones = listar_inscripciones_de_horario(db, row["id"])
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


def listar_inscripciones_de_horario(db, horario_id):
    rows = db.execute(
        "SELECT i.*, c.id AS c_id, c.nombre AS c_nombre, c.dni AS c_dni, c.telefono AS c_telefono, "
        "c.email AS c_email, c.activo AS c_activo "
        "FROM inscripciones i JOIN clientes c ON c.id = i.cliente_id WHERE i.horario_id = ?",
        (horario_id,),
    ).fetchall()
    result = []
    for row in rows:
        cliente = Cliente({
            "id": row["c_id"], "nombre": row["c_nombre"], "dni": row["c_dni"],
            "telefono": row["c_telefono"], "email": row["c_email"], "activo": row["c_activo"],
        })
        result.append(Inscripcion(row, cliente))
    return result


def listar_inscripciones_de_cliente(db, cliente_id):
    rows = db.execute(
        "SELECT i.*, h.* , i.id AS i_id FROM inscripciones i JOIN horarios h ON h.id = i.horario_id "
        "WHERE i.cliente_id = ? AND i.activa = 1 "
        "ORDER BY CASE h.dia_semana " + " ".join(f"WHEN '{d}' THEN {n}" for n, d in enumerate(DIAS)) +
        " END, h.hora_inicio",
        (cliente_id,),
    ).fetchall()
    result = []
    for row in rows:
        horario_row = {
            "id": row["horario_id"], "dia_semana": row["dia_semana"], "hora_inicio": row["hora_inicio"],
            "hora_fin": row["hora_fin"], "etiqueta": row["etiqueta"], "cupo_maximo": row["cupo_maximo"],
        }
        horario = Horario(horario_row, cupo_disponible=None)
        insc_row = {"id": row["i_id"], "cliente_id": row["cliente_id"], "horario_id": row["horario_id"], "activa": row["activa"]}
        result.append(Inscripcion(insc_row, cliente=None, horario=horario))
    return result


class Pago:
    def __init__(self, row, cliente):
        self.id = row["id"]
        self.cliente = cliente
        self.fecha_pago = date.fromisoformat(row["fecha_pago"])
        self.monto = row["monto"]
        self.dias_validez = row["dias_validez"]
        self.fecha_vencimiento = date.fromisoformat(row["fecha_vencimiento"])
        self.nota = row["nota"]


def listar_pagos(db, limit=20):
    rows = db.execute(
        "SELECT p.*, c.id AS c_id, c.nombre AS c_nombre, c.dni AS c_dni, c.telefono AS c_telefono, "
        "c.email AS c_email, c.activo AS c_activo "
        "FROM pagos p JOIN clientes c ON c.id = p.cliente_id ORDER BY p.fecha_pago DESC, p.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        cliente = Cliente({
            "id": row["c_id"], "nombre": row["c_nombre"], "dni": row["c_dni"],
            "telefono": row["c_telefono"], "email": row["c_email"], "activo": row["c_activo"],
        })
        result.append(Pago(row, cliente))
    return result


# ---------- Auth admin ----------

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


# ---------- Rutas públicas ----------

@app.route("/")
def index():
    db = get_db()
    horarios = listar_horarios(db)
    horarios_por_dia = {}
    for h in horarios:
        horarios_por_dia.setdefault(h.dia_semana, []).append(h)
    return render_template("index.html", horarios_por_dia=horarios_por_dia)


@app.route("/reservar", methods=["POST"])
def reservar():
    db = get_db()
    dni = request.form["dni"].strip()
    horario_id = request.form["horario_id"]

    cliente = obtener_cliente_por_dni(db, dni)
    if not cliente or not cliente.activo:
        flash("No encontramos un cliente activo con ese DNI. Consultá con el gimnasio.", "error")
        return redirect(url_for("index"))

    horario_row = db.execute("SELECT * FROM horarios WHERE id = ?", (horario_id,)).fetchone()
    if not horario_row:
        flash("Ese horario ya no existe.", "error")
        return redirect(url_for("index"))

    ya_inscripto = db.execute(
        "SELECT 1 FROM inscripciones WHERE cliente_id = ? AND horario_id = ? AND activa = 1",
        (cliente.id, horario_id),
    ).fetchone()
    if ya_inscripto:
        flash("Ya tenés una reserva en ese horario.", "error")
        return redirect(url_for("index"))

    cupo = _cupo_disponible(db, horario_id, horario_row["cupo_maximo"])
    if cupo <= 0:
        flash("Ese horario ya no tiene cupo disponible.", "error")
        return redirect(url_for("index"))

    existente = db.execute(
        "SELECT id FROM inscripciones WHERE cliente_id = ? AND horario_id = ?", (cliente.id, horario_id)
    ).fetchone()
    if existente:
        db.execute("UPDATE inscripciones SET activa = 1 WHERE id = ?", (existente["id"],))
    else:
        db.execute(
            "INSERT INTO inscripciones (cliente_id, horario_id, activa) VALUES (?, ?, 1)",
            (cliente.id, horario_id),
        )
    db.commit()
    flash("¡Turno reservado!", "success")
    return redirect(url_for("index"))


@app.route("/mi-cuenta")
def mi_cuenta():
    db = get_db()
    dni = request.args.get("dni", "").strip()
    cliente = None
    inscripciones = []
    buscado = bool(dni)
    if dni:
        cliente = obtener_cliente_por_dni(db, dni)
        if cliente:
            inscripciones = listar_inscripciones_de_cliente(db, cliente.id)
    return render_template(
        "mi_cuenta.html", dni=dni, cliente=cliente, inscripciones=inscripciones, buscado=buscado
    )


@app.route("/mi-cuenta/cancelar/<int:inscripcion_id>", methods=["POST"])
def mi_cuenta_cancelar(inscripcion_id):
    db = get_db()
    dni = request.form.get("dni", "").strip()
    row = db.execute(
        "SELECT i.id, c.dni FROM inscripciones i JOIN clientes c ON c.id = i.cliente_id WHERE i.id = ?",
        (inscripcion_id,),
    ).fetchone()
    if row and row["dni"] == dni:
        db.execute("UPDATE inscripciones SET activa = 0 WHERE id = ?", (inscripcion_id,))
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
    hoy_nombre = DIAS[datetime.today().weekday()]
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
                "INSERT INTO clientes (nombre, dni, telefono, email, activo) VALUES (?, ?, ?, ?, 1)",
                (request.form["nombre"], request.form["dni"], request.form.get("telefono"), request.form.get("email")),
            )
            db.commit()
        except sqlite3.IntegrityError:
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
            "UPDATE clientes SET nombre = ?, dni = ?, telefono = ?, email = ?, activo = ? WHERE id = ?",
            (
                request.form["nombre"], request.form["dni"], request.form.get("telefono"),
                request.form.get("email"), 1 if request.form.get("activo") else 0, cliente_id,
            ),
        )
        db.commit()
        return redirect(url_for("admin_clientes"))
    cliente = obtener_cliente_por_id(db, cliente_id)
    return render_template("admin/cliente_form.html", cliente=cliente)


@app.route("/admin/clientes/<int:cliente_id>/eliminar", methods=["POST"])
@admin_required
def admin_cliente_eliminar(cliente_id):
    db = get_db()
    db.execute("DELETE FROM clientes WHERE id = ?", (cliente_id,))
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
        db.execute(
            "INSERT INTO pagos (cliente_id, fecha_pago, monto, dias_validez, fecha_vencimiento, nota) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                request.form["cliente_id"], fecha_pago.isoformat(), float(request.form["monto"]),
                dias_validez, fecha_vencimiento.isoformat(), request.form.get("nota"),
            ),
        )
        db.commit()
        return redirect(url_for("admin_pagos"))
    return render_template(
        "admin/pagos.html",
        clientes=listar_clientes(db, solo_activos=True),
        pagos=listar_pagos(db),
        today=date.today().isoformat(),
    )


# ---------- Admin: horarios ----------

@app.route("/admin/horarios", methods=["GET", "POST"])
@admin_required
def admin_horarios():
    db = get_db()
    if request.method == "POST":
        db.execute(
            "INSERT INTO horarios (dia_semana, hora_inicio, hora_fin, etiqueta, cupo_maximo) VALUES (?, ?, ?, ?, ?)",
            (
                request.form["dia_semana"], request.form["hora_inicio"], request.form["hora_fin"],
                request.form.get("etiqueta"), int(request.form["cupo_maximo"]),
            ),
        )
        db.commit()
        return redirect(url_for("admin_horarios"))
    return render_template("admin/horarios.html", horarios=listar_horarios(db), dias=DIAS)


@app.route("/admin/horarios/<int:horario_id>/eliminar", methods=["POST"])
@admin_required
def admin_horario_eliminar(horario_id):
    db = get_db()
    db.execute("DELETE FROM horarios WHERE id = ?", (horario_id,))
    db.commit()
    return redirect(url_for("admin_horarios"))


# ---------- Admin: asistencia ----------

@app.route("/admin/asistencia", methods=["GET", "POST"])
@admin_required
def admin_asistencia():
    db = get_db()
    fecha_sel = request.values.get("fecha") or date.today().isoformat()
    fecha_dt = date.fromisoformat(fecha_sel)
    dia_nombre = DIAS[fecha_dt.weekday()]

    if request.method == "POST":
        inscripcion_id = request.form["inscripcion_id"]
        try:
            db.execute(
                "INSERT INTO asistencias (inscripcion_id, fecha) VALUES (?, ?)", (inscripcion_id, fecha_sel)
            )
            db.commit()
        except sqlite3.IntegrityError:
            pass
        return redirect(url_for("admin_asistencia", fecha=fecha_sel))

    horarios = listar_horarios(db, dia_semana=dia_nombre, con_inscripciones=True)
    rows = db.execute("SELECT inscripcion_id FROM asistencias WHERE fecha = ?", (fecha_sel,)).fetchall()
    asistencias_hoy = {r["inscripcion_id"] for r in rows}
    return render_template(
        "admin/asistencia.html",
        horarios=horarios,
        fecha_sel=fecha_sel,
        dia_nombre=dia_nombre,
        asistencias_hoy=asistencias_hoy,
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
