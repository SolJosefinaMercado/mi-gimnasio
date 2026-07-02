# Mi Gimnasio — control de clientes, pagos y turnos

App simple en Flask + SQLite para gestionar un gimnasio:
- **Vos (admin)**: cargás clientes, registrás pagos, creás horarios y marcás asistencia.
- **Tus clientes (sin login)**: reservan turnos y consultan su vencimiento de abono desde el header ("Mi cuenta / vencimiento"), identificándose con su DNI.

## Cómo correrlo localmente

```bash
python -m venv venv
source venv/bin/activate       # en Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Abrí `http://localhost:5000`.

**Acceso admin**: `http://localhost:5000/admin/login`
Contraseña por defecto: `admin123` (definida en `app.py` con la variable de entorno `ADMIN_PASSWORD`, cambiala antes de publicar).

## Primeros pasos dentro de la app

1. Entrá a `/admin/login` y logueate.
2. Cargá tus horarios de clase en **Horarios** (día, hora, cupo).
3. Cargá tus clientes en **Clientes** (nombre + DNI, que van a usar para reservar/consultar).
4. Registrá los pagos en **Pagos** (esto calcula automáticamente la fecha de vencimiento).
5. Compartí la URL pública con tus clientes para que reserven turnos y vean su vencimiento.
6. Usá **Asistencia** cada día para marcar quién vino a cada turno.

## Desplegar en Render (igual que tu proyecto de Mundialista)

1. Subí esta carpeta a un repo de GitHub.
2. En Render: New → Web Service → conectá el repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Agregá las variables de entorno `SECRET_KEY` y `ADMIN_PASSWORD` con valores propios (no dejes las de prueba).

**Importante sobre la base de datos**: SQLite en Render se reinicia con cada deploy si no configurás un disco persistente. Para producción real, andá a Render → tu servicio → "Disks" y montá un disco persistente en la carpeta del proyecto, o migrá a PostgreSQL (Render lo ofrece gratis) cuando el gimnasio crezca.

## Modelo de datos (resumen)

- `Cliente`: nombre, DNI, teléfono, email, activo
- `Pago`: cliente, fecha de pago, monto, fecha de vencimiento (pago + días de validez)
- `Horario`: día de la semana, hora inicio/fin, cupo máximo
- `Inscripcion`: relación cliente–horario (reserva)
- `Asistencia`: registro de presencia por inscripción y fecha

## Próximos pasos posibles

- Enviar recordatorios automáticos de vencimiento (email/WhatsApp).
- Exportar reportes de asistencia y pagos a Excel.
- Panel con métricas: clientes con más faltas, ingresos mensuales, etc. (ahí podrías reutilizar lo que aprendiste con KNN/Árbol de decisión para predecir abandono de clientes).
