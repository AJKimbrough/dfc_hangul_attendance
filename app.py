from flask import Flask, render_template, request, redirect, url_for, flash, send_file, make_response, session
from flask import Response
import csv
from urllib.parse import urljoin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from datetime import datetime, date
from apscheduler.schedulers.background import BackgroundScheduler

import re
from sqlalchemy.engine.url import make_url
import io, os, qrcode, smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

def normalize_database_url(url: str) -> str:
    if not url:
        return 'sqlite:///attendance.db'
    # Render/Heroku sometimes give postgres://; SQLAlchemy prefers postgresql+psycopg2://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    # Optionally enforce SSL if not present (safe for most hosted DBs)
    if "sslmode=" not in url and url.startswith("postgresql+psycopg2://"):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url

raw_db_url = os.getenv('DATABASE_URL', 'sqlite:///attendance.db')
db_url = normalize_database_url(raw_db_url)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY','dev')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL','sqlite:///attendance.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

from werkzeug.exceptions import HTTPException

@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e  # return real 404/403, etc.
    import traceback
    traceback.print_exc()
    return ("Something went wrong. Check the server logs for details.", 500)

@app.route('/favicon.ico')
def favicon():
    path = os.path.join(app.root_path, 'static', 'favicon.ico')
    if os.path.exists(path):
        return send_file(path, mimetype='image/x-icon')
    return ("", 204)


@app.context_processor
def inject_current_year():
    from datetime import datetime
    return {"current_year": datetime.utcnow().year}

# 2) dev error handler to print error terminal
@app.errorhandler(Exception)
def on_error(e):
    import traceback
    traceback.print_exc()
    return ("Something went wrong. Check the server logs for details.", 500)


@app.after_request
def skip_ngrok_warning(response):
    response.headers['ngrok-skip-browser-warning'] = 'true'
    return response

DANGER_THRESHOLD = 0.5  # < 50% attendance -> inactive

def png_response(buf: io.BytesIO):
    resp = make_response(send_file(buf, mimetype='image/png'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# Initialize DB
db = SQLAlchemy(app)

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    email = db.Column(db.String(255), nullable=True, unique=True)
    active = db.Column(db.Boolean, default=True)
    last_notified_at = db.Column(db.DateTime, nullable=True)

class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    class_date = db.Column(db.Date, nullable=False, unique=True)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=False)
    present = db.Column(db.Boolean, default=True, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('student_id', 'session_id', name='uniq_attendance'),)

    student = db.relationship('Student', backref='attendances')
    session = db.relationship('Session', backref='attendances')

def public_url_for(endpoint: str, **values) -> str:
    """
    Build a URL for QR codes using PUBLIC_BASE_URL if provided.
    Examples:
      PUBLIC_BASE_URL = https://abc123.ngrok.io
      public_url_for('checkin', session_id=5) -> https://abc123.ngrok.io/checkin?session_id=5
    """
    base = os.getenv('PUBLIC_BASE_URL')
    rel = url_for(endpoint, _external=False, **values)
    if base:
        return urljoin(base.rstrip('/') + '/', rel.lstrip('/'))
    return url_for(endpoint, _external=True, **values)

# Utility: ensure today's session exists
def get_or_create_today_session():
    today = date.today()
    sess = Session.query.filter_by(class_date=today).first()
    if not sess:
        sess = Session(class_date=today)
        db.session.add(sess)
        db.session.commit()
    return sess

# Email logic
def send_email(to_email: str, subject: str, body: str):
    host = os.getenv('SMTP_HOST')
    port = int(os.getenv('SMTP_PORT','587'))
    user = os.getenv('SMTP_USERNAME')
    pwd = os.getenv('SMTP_PASSWORD')
    from_email = os.getenv('FROM_EMAIL', user)
    if not (host and port and user and pwd and from_email and to_email):
        print("[WARN] Email not configured; skipping send.")
        return False
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = to_email
    msg.set_content(body)
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
    return True

# Attendance logic
def attendance_ratio(student_id: int):
    total_sessions = db.session.query(func.count(Session.id)).scalar() or 0
    if total_sessions == 0:
        return 1.0  
    present_count = (
        db.session.query(func.count(Attendance.id))
        .filter(Attendance.student_id==student_id, Attendance.present==True)
        .scalar() or 0
    )
    return present_count / total_sessions

# Notify if close to inactive
def check_and_notify_student(student: Student):
    ratio = attendance_ratio(student.id)
    is_inactive = ratio < DANGER_THRESHOLD
    if is_inactive and student.active:
        # transition from active -> inactive
        student.active = False
        db.session.commit()
        if student.email:
            send_email(
                student.email,
                "Attendance Alert: You are below 50%",
                f"Hi {student.name},\n\nOur records show your attendance is {ratio:.0%}, which is below the required 50%. Please reach out to your instructor to get back on track.\n\nThanks."
            )
            student.last_notified_at = datetime.utcnow()
            db.session.commit()
    elif (not is_inactive) and (not student.active):
        # transition from inactive -> active
        student.active = True
        db.session.commit()

# Routes
@app.route('/')
def index():
    today_session = get_or_create_today_session()
    qr_target = public_url_for('checkin', session_id=today_session.id)
    return render_template('index.html', today_session=today_session, qr_target=qr_target)

@app.route('/qr/today')
def qr_today():
    """Render a QR that points to the check-in page for today's session."""
    sess = get_or_create_today_session()
    url = public_url_for('checkin', session_id=sess.id)
    print(f"[QR] Generating QR for URL: {url}")  
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return png_response(buf)

@app.route('/qr.png')
def qr_for_url():
    url = request.args.get('url')
    if not url:
        return "Missing url", 400
    print(f"[QR] /qr.png for URL: {url}")
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return png_response(buf)

@app.route('/checkin')
def checkin():
    session_id = request.args.get('session_id', type=int)
    if not session_id:
        sess = get_or_create_today_session()
        session_id = sess.id
    sess = Session.query.get_or_404(session_id)
    return render_template('checkin.html', class_session=sess)

@app.route('/submit_checkin', methods=['POST'])
def submit_checkin():
    session_id = request.form.get('session_id', type=int)
    name = (request.form.get('name') or '').strip()
    email = (request.form.get('email') or '').strip()
    if not name:
        flash('Please enter your name.', 'error')
        return redirect(url_for('checkin', session_id=session_id))

    student = Student.query.filter(func.lower(Student.name)==name.lower()).first()
    if not student:
        student = Student(name=name, email=(email or None))
        db.session.add(student)
        db.session.commit()
    else:
        if email and (not student.email):
            student.email = email
            db.session.commit()

    # Mark attendance
    existing = Attendance.query.filter_by(student_id=student.id, session_id=session_id).first()
    if not existing:
        att = Attendance(student_id=student.id, session_id=session_id, present=True)
        db.session.add(att)
        db.session.commit()

    # Recompute status, notify
    check_and_notify_student(student)

    flash('Checked in! Have a great class.', 'success')
    return redirect(url_for('checkin', session_id=session_id))

# Admin / dashboard
@app.route('/dashboard')
def dashboard():
    students = Student.query.order_by(Student.name).all()
    total_sessions = db.session.query(func.count(Session.id)).scalar() or 0
    rows = []
    for s in students:
        ratio = attendance_ratio(s.id) if total_sessions else 1.0
        rows.append({
            'id': s.id,                     
            'name': s.name,
            'email': s.email or '—',
            'present': int(round(ratio*100)),
            'status': 'Active' if s.active else 'Inactive'
        })
    return render_template('dashboard.html', rows=rows, total_sessions=total_sessions)

@app.route('/dashboard/by-date')
def dashboard_by_date():
    """Sectioned attendance: list who checked in for each session date."""
    sessions = Session.query.order_by(Session.class_date.desc()).all()
    days = []
    for s in sessions:
        rows = (
            db.session.query(Student.name, Student.email)
            .join(Attendance, Attendance.student_id == Student.id)
            .filter(Attendance.session_id == s.id, Attendance.present == True)
            .order_by(Student.name)
            .all()
        )
        days.append({
            "session": s,
            "count": len(rows),
            "students": [{"name": n, "email": e or "—"} for (n, e) in rows],
        })
    return render_template('dashboard_by_date.html', days=days)

@app.route('/admin/export.csv')
def export_csv():
    session_id = request.args.get('session_id', type=int)
    output = io.StringIO()
    writer = csv.writer(output)

    if session_id:
        sess = Session.query.get_or_404(session_id)
        writer.writerow(['session_date','name','email'])
        for name, email in (
            db.session.query(Student.name, Student.email)
            .join(Attendance, Attendance.student_id == Student.id)
            .filter(Attendance.session_id == session_id, Attendance.present == True)
            .order_by(Student.name)
        ):
            writer.writerow([sess.class_date.isoformat(), name, email or ''])
    else:
        students = Student.query.order_by(Student.name).all()
        total_sessions = db.session.query(func.count(Session.id)).scalar() or 0
        writer.writerow(['name','email','attendance_percent','status','present_days','total_sessions'])
        for s in students:
            present_count = (
                db.session.query(func.count(Attendance.id))
                .filter(Attendance.student_id==s.id, Attendance.present==True)
                .scalar() or 0
            )
            pct = int(round((present_count/(total_sessions or 1))*100))
            status = 'Active' if pct>=50 else 'Inactive'
            writer.writerow([s.name, s.email or '', pct, status, present_count, total_sessions])

    resp = Response(output.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = 'attachment; filename="attendance_export.csv"'
    return resp

# Admin: create a session
@app.route('/admin/create_session', methods=['POST'])
def create_session():
    code = request.form.get('code')
    if code != os.getenv('ADMIN_CODE','letmein'):
        return "Forbidden", 403
    date_str = request.form.get('class_date')
    when = datetime.strptime(date_str, '%Y-%m-%d').date()
    if not Session.query.filter_by(class_date=when).first():
        db.session.add(Session(class_date=when))
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/admin/clear_students', methods=['POST'])
def clear_students():
    code = request.form.get('code', '')
    if code != os.getenv('ADMIN_CODE', 'letmein'):
        return "Forbidden", 403

    Attendance.query.delete()   
    Student.query.delete()      
    db.session.commit()
    flash("All students and their attendance have been deleted.", "success")
    return redirect(url_for('dashboard'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if code == os.getenv('ADMIN_CODE', 'letmein'):
            session['is_admin'] = True
            flash('Admin mode enabled.', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid admin code.', 'error')
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    flash('Admin mode disabled.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/health')
def health():
    return "ok", 200

# ---- Admin Student Management ----
def require_admin():
    return bool(session.get('is_admin'))

@app.route('/admin/student/<int:student_id>/delete', methods=['POST'])
def delete_student(student_id):
    if not require_admin():
        return "Forbidden", 403
    s = Student.query.get_or_404(student_id)
    Attendance.query.filter_by(student_id=s.id).delete(synchronize_session=False)
    db.session.delete(s)
    db.session.commit()
    flash(f"Deleted {s.name}.", "success")
    return redirect(url_for('dashboard'))

@app.route('/admin/student/<int:student_id>/edit', methods=['GET', 'POST'])
def edit_student(student_id):
    if not require_admin():
        return "Forbidden", 403
    s = Student.query.get_or_404(student_id)
    if request.method == 'POST':
        new_name = (request.form.get('name') or '').strip()
        new_email = (request.form.get('email') or '').strip() or None
        if not new_name:
            flash('Name is required.', 'error')
            return redirect(url_for('edit_student', student_id=student_id))
        s.name = new_name
        s.email = new_email
        try:
            db.session.commit()
            flash('Student updated.', 'success')
            return redirect(url_for('dashboard'))
        except IntegrityError:
            db.session.rollback()
            flash('Name or email already exists. Choose a different one.', 'error')
            return redirect(url_for('edit_student', student_id=student_id))
    return render_template('edit_student.html', student=s)


@app.route('/_debug_base')
def debug_base():
    return {
        "PUBLIC_BASE_URL": os.getenv("PUBLIC_BASE_URL"),
        "qr_example": public_url_for("checkin", session_id=get_or_create_today_session().id)
    }, 200

# Background:check statuses daily, 5 AM
scheduler = BackgroundScheduler(daemon=True)
@scheduler.scheduled_job('cron', hour=5)
def daily_recompute():
    with app.app_context():
        for s in Student.query.all():
            prev = s.active
            check_and_notify_student(s)
            if prev and not s.active:
                print(f"[INFO] {s.name} dropped below 50% and was notified.")

scheduler.start()

# CLI: initialize DB
@app.cli.command('init-db')
def init_db_cmd():
    db.create_all()
    print('Initialized the database.')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)


