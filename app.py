import sqlite3
import random
import os
import time
import uuid
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from email_validator import validate_email, EmailNotValidError
import phonenumbers

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_vr_project'

CURRENT_VERSION = str(uuid.uuid4())

# --- НАСТРОЙКА BREVO ---
BREVO_API_KEY = os.environ.get('BREVO_API_KEY')
SENDER_EMAIL = os.environ.get('MAIL_USER')
# -----------------------

MAINTENANCE_MODE = False
SUPER_ADMIN_EMAIL = 'admin@nexus-market.ru'

def init_db():
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, phone TEXT, password TEXT, role TEXT, is_banned INTEGER DEFAULT 0, notification TEXT DEFAULT NULL)''')
    try: c.execute("ALTER TABLE users ADD COLUMN notification TEXT DEFAULT NULL")
    except sqlite3.OperationalError: pass

    c.execute('''CREATE TABLE IF NOT EXISTS products 
                 (id INTEGER PRIMARY KEY, title TEXT, description TEXT, price INTEGER, icon TEXT, author_name TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id INTEGER PRIMARY KEY, sender_name TEXT, sender_email TEXT, content TEXT, created_at TEXT, status TEXT DEFAULT 'new')''')
    try: c.execute("ALTER TABLE messages ADD COLUMN status TEXT DEFAULT 'new'")
    except sqlite3.OperationalError: pass

    # НОВАЯ ТАБЛИЦА ДЛЯ ЗАЩИТЫ ОТ ВЗЛОМА (чтобы не сбрасывалась)
    c.execute('''CREATE TABLE IF NOT EXISTS login_attempts 
                 (ip TEXT PRIMARY KEY, count INTEGER DEFAULT 0, lock_until REAL DEFAULT 0)''')
    
    c.execute("SELECT COUNT(*) FROM users WHERE email = ?", (SUPER_ADMIN_EMAIL,))
    if c.fetchone()[0] == 0:
        default_pw = generate_password_hash('123456')
        c.execute("INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)", 
                  ('Главный Босс', SUPER_ADMIN_EMAIL, '+7 999 123-45-67', default_pw, 'super_admin'))
    
    c.execute("SELECT COUNT(*) FROM products")
    if c.fetchone()[0] == 0:
        products = [
            ('Аккаунт Discord с Nitro', 'Полный доступ.', 1500, '🎮', 'NEXUS Official'),
            ('VR-очки NEXUS Pro', 'Погружение в VR.', 45000, '🥽', 'NEXUS Official')
        ]
        c.executemany("INSERT INTO products (title, description, price, icon, author_name) VALUES (?, ?, ?, ?, ?)", products)
    conn.commit()
    conn.close()

init_db()
verification_codes = {}

@app.route('/api/version')
def get_version():
    return jsonify({"version": CURRENT_VERSION})

@app.before_request
def check_notifications():
    if session.get('user_email') and request.endpoint not in ['static', 'get_version']:
        conn = sqlite3.connect('store.db')
        c = conn.cursor()
        c.execute("SELECT notification FROM users WHERE email = ?", (session['user_email'],))
        result = c.fetchone()
        if result and result[0]:
            flash(result[0], 'success')
            c.execute("UPDATE users SET notification = NULL WHERE email = ?", (session['user_email'],))
            conn.commit()
        conn.close()

@app.before_request
def check_maintenance():
    global MAINTENANCE_MODE
    if MAINTENANCE_MODE:
        if session.get('user_role') not in ['admin', 'super_admin']:
            if request.endpoint not in ['maintenance', 'static', 'get_version']:
                return render_template('maintenance.html'), 503

@app.route('/maintenance')
def maintenance():
    return render_template('maintenance.html')

@app.route('/')
def index():
    if MAINTENANCE_MODE and session.get('user_role') not in ['admin', 'super_admin']:
        return render_template('maintenance.html'), 503
    
    search_query = request.args.get('q', '').strip()
    # Баг 5: Экранируем символы поиска
    safe_query = search_query.replace('%', '').replace('_', '')
    
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    if safe_query:
        c.execute("SELECT * FROM products WHERE title LIKE ?", (f'%{safe_query}%',))
    else:
        c.execute("SELECT * FROM products")
    products = c.fetchall()
    conn.close()
    return render_template('index.html', products=products, search_query=search_query)

@app.route('/health')
def health():
    return "OK", 200

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if 'consent' not in request.form:
            flash('Вы должны согласиться на обработку данных!', 'error')
            return redirect(url_for('register'))

        name = request.form['name'].strip()
        email = request.form['email'].strip()
        phone = request.form['phone'].strip()
        
        try:
            validate_email(email, check_deliverability=False)
        except EmailNotValidError:
            flash('Введите корректный email адрес (например, name@mail.ru)', 'error')
            return redirect(url_for('register'))

        try:
            parsed_phone = phonenumbers.parse(phone, "RU")
            if not phonenumbers.is_valid_number(parsed_phone):
                flash('Введите корректный номер телефона (например, +7 999 123-45-67)', 'error')
                return redirect(url_for('register'))
        except phonenumbers.NumberParseException:
            flash('Неверный формат телефона!', 'error')
            return redirect(url_for('register'))

        # Баг 3: Быстрая проверка на дубликаты одним запросом
        conn = sqlite3.connect('store.db')
        c = conn.cursor()
        c.execute("SELECT name, email, phone FROM users WHERE email = ? OR name = ? OR phone = ?", (email, name, phone))
        duplicates = c.fetchall()
        conn.close()

        for d in duplicates:
            if d[1].lower() == email.lower():
                flash('Пользователь с такой почтой уже зарегистрирован!', 'error')
                return redirect(url_for('register'))
            if d[0].lower() == name.lower():
                flash('Пользователь с таким ником уже зарегистрирован! Придумайте другой.', 'error')
                return redirect(url_for('register'))
            if d[2] == phone:
                flash('Пользователь с таким номером телефона уже зарегистрирован!', 'error')
                return redirect(url_for('register'))

        code = str(random.randint(1000, 9999))
        verification_codes[email] = code
        
        try:
            url = "https://api.brevo.com/v3/smtp/email"
            headers = {
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json"
            }
            payload = {
                "sender": {"name": "NEXUS Market", "email": SENDER_EMAIL},
                "to": [{"email": email}],
                "subject": "Код подтверждения NEXUS Market",
                "htmlContent": f"<h2>Ваш код: {code}</h2>"
            }
            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code == 201:
                flash('Код отправлен на почту!', 'success')
            else:
                print(f"❌ ОШИБКА BREVO: {response.text}")
                flash(f'Ошибка отправки. ВАШ ТЕСТОВЫЙ КОД: {code}', 'error')
        except Exception as e:
            print(f"❌ ОШИБКА СЕТИ: {e}")
            flash(f'Ошибка отправки. ВАШ ТЕСТОВЫЙ КОД: {code}', 'error')
        
        session['reg_name'] = name
        session['reg_email'] = email
        session['reg_phone'] = phone
        return redirect(url_for('verify'))
    return render_template('auth.html', mode='register')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if 'reg_email' not in session: return redirect(url_for('register'))
    if request.method == 'POST':
        if verification_codes.get(session['reg_email']) == request.form['code']:
            session['verified_email'] = session['reg_email']
            return redirect(url_for('set_password'))
        flash('Неверный код!', 'error')
    return render_template('auth.html', mode='verify')

@app.route('/set_password', methods=['GET', 'POST'])
def set_password():
    if 'verified_email' not in session: return redirect(url_for('register'))
    if request.method == 'POST':
        password = request.form['password']
        if len(password) < 6:
            flash('Пароль должен быть не короче 6 символов!', 'error')
            return redirect(url_for('set_password'))

        hashed_pw = generate_password_hash(password)
        try:
            conn = sqlite3.connect('store.db')
            c = conn.cursor()
            c.execute("INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)", 
                      (session.get('reg_name'), session.get('verified_email'), session.get('reg_phone'), hashed_pw, 'user'))
            conn.commit()
            conn.close()
            session.clear()
            flash('Аккаунт создан! Войдите.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Эта почта уже занята!', 'error')
            return redirect(url_for('register'))
    return render_template('auth.html', mode='password')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Баг 2: Правильное определение IP за прокси-сервером Layero
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if user_ip:
        user_ip = user_ip.split(',')[0].strip()

    current_time = time.time()
    
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    
    # Баг 1: Проверка блокировки из базы данных
    c.execute("SELECT count, lock_until FROM login_attempts WHERE ip = ?", (user_ip,))
    attempt = c.fetchone()
    
    if attempt and current_time < attempt[1]:
        remaining = int(attempt[1] - current_time)
        conn.close()
        flash(f'Заблокировано. Ждите {remaining} сек.', 'error')
        return render_template('auth.html', mode='login', lockout_time=remaining)

    if request.method == 'POST':
        email = request.form['email'].strip()
        password = request.form['password']
        
        c.execute("SELECT name, password, role, is_banned FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        
        if user:
            if user[3] == 1:
                flash('Ваш аккаунт заблокирован администрацией!', 'error')
                conn.close()
                return render_template('auth.html', mode='login')
                
            if check_password_hash(user[1], password):
                # Успешный вход: сбрасываем счетчик
                c.execute("DELETE FROM login_attempts WHERE ip = ?", (user_ip,))
                conn.commit()
                session['user_name'] = user[0]
                session['user_role'] = user[2]
                session['user_email'] = email
                conn.close()
                return redirect(url_for('index'))
        
        # Неудачная попытка
        new_count = (attempt[0] if attempt else 0) + 1
        
        if new_count >= 10:
            lock_time = current_time + 1800
            c.execute("INSERT OR REPLACE INTO login_attempts (ip, count, lock_until) VALUES (?, ?, ?)", (user_ip, new_count, lock_time))
            conn.commit()
            conn.close()
            return render_template('auth.html', mode='login', lockout_time=1800)
        else:
            c.execute("INSERT OR REPLACE INTO login_attempts (ip, count, lock_until) VALUES (?, ?, ?)", (user_ip, new_count, 0))
            conn.commit()
            flash(f'Неверные данные. Осталось попыток: {10 - new_count}', 'error')
            
    conn.close()
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/support', methods=['GET', 'POST'])
def support():
    if not session.get('user_name'):
        flash('Войдите, чтобы написать в поддержку!', 'error')
        return redirect(url_for('login'))
    if request.method == 'POST':
        content = request.form['content']
        current_time = time.strftime('%d.%m.%Y %H:%M')
        conn = sqlite3.connect('store.db')
        c = conn.cursor()
        c.execute("INSERT INTO messages (sender_name, sender_email, content, created_at, status) VALUES (?, ?, ?, ?, 'new')", 
                  (session['user_name'], session['user_email'], content, current_time))
        conn.commit()
        conn.close()
        flash('Сообщение отправлено! Администрация скоро ответит.', 'success')
        return redirect(url_for('index'))
    return render_template('support.html')

@app.route('/sell', methods=['GET', 'POST'])
def sell():
    if not session.get('user_name'):
        flash('Войдите, чтобы продавать!', 'error')
        return redirect(url_for('login'))
    if request.method == 'POST':
        conn = sqlite3.connect('store.db')
        c = conn.cursor()
        c.execute("INSERT INTO products (title, description, price, icon, author_name) VALUES (?, ?, ?, ?, ?)", 
                  (request.form['title'], request.form['description'], request.form['price'], request.form['icon'], session['user_name']))
        conn.commit()
        conn.close()
        flash('Товар выставлен на продажу!', 'success')
        return redirect(url_for('index'))
    return render_template('sell.html')

@app.route('/admin')
def admin():
    if session.get('user_role') not in ['admin', 'super_admin']:
        return redirect(url_for('index'))
    search_query = request.args.get('search', '').strip()
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("SELECT * FROM products")
    products = c.fetchall()
    if search_query:
        c.execute("SELECT id, name, email, phone, role, is_banned FROM users WHERE name LIKE ? OR email LIKE ? OR phone LIKE ?", (f'%{search_query}%', f'%{search_query}%', f'%{search_query}%'))
    else:
        c.execute("SELECT id, name, email, phone, role, is_banned FROM users")
    users = c.fetchall()
    c.execute("SELECT * FROM messages ORDER BY id DESC")
    messages = c.fetchall()
    conn.close()
    return render_template('admin.html', products=products, users=users, search_query=search_query, maintenance=MAINTENANCE_MODE, messages=messages)

@app.route('/add_product', methods=['POST'])
def add_product():
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("INSERT INTO products (title, description, price, icon, author_name) VALUES (?, ?, ?, ?, ?)", 
              (request.form['title'], request.form['description'], request.form['price'], request.form['icon'], 'Администрация'))
    conn.commit()
    conn.close()
    flash('Товар добавлен!', 'success')
    return redirect(url_for('admin'))

@app.route('/delete_product/<int:id>')
def delete_product(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin'))

@app.route('/delete_message/<int:id>')
def delete_message(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    flash('Сообщение удалено.', 'success')
    return redirect(url_for('admin'))

@app.route('/message/read/<int:id>')
def mark_read(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("UPDATE messages SET status = 'read' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin'))

@app.route('/message/resolve/<int:id>')
def mark_resolved(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("UPDATE messages SET status = 'resolved' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin'))

@app.route('/action/ban/<int:id>')
def ban_user(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE id = ?", (id,))
    target = c.fetchone()
    if target and target[0] == SUPER_ADMIN_EMAIL:
        flash('ОШИБКА: Вы не можете забанить Главного Босса!', 'error')
    else:
        c.execute("UPDATE users SET is_banned = 1 WHERE id = ?", (id,))
        conn.commit()
        flash('Пользователь забанен.', 'success')
    conn.close()
    return redirect(url_for('admin'))

@app.route('/action/unban/<int:id>')
def unban_user(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 0 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin'))

@app.route('/action/make_admin/<int:id>')
def make_admin(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("UPDATE users SET role = 'admin', notification = '🎉 Поздравляем! Вам выданы права администратора.' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    flash('Права администратора выданы.', 'success')
    return redirect(url_for('admin'))

@app.route('/action/remove_admin/<int:id>')
def remove_admin(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE id = ?", (id,))
    target = c.fetchone()
    if target and target[0] == SUPER_ADMIN_EMAIL:
        flash('ОШИБКА: Нельзя снять права с Главного Босса!', 'error')
    else:
        c.execute("UPDATE users SET role = 'user', notification = '⚠️ Ваши права администратора были сняты.' WHERE id = ?", (id,))
        conn.commit()
        flash('Права администратора сняты.', 'success')
    conn.close()
    return redirect(url_for('admin'))

@app.route('/action/toggle_maintenance', methods=['POST'])
def toggle_maintenance():
    global MAINTENANCE_MODE
    if session.get('user_role') != 'super_admin':
        flash('ОШИБКА: У вас нет прав для управления сервером!', 'error')
        return redirect(url_for('admin'))
    code = request.form.get('code')
    if code == 'STOP-2026':
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        status = "ВКЛЮЧЕН" if MAINTENANCE_MODE else "ВЫКЛЮЧЕН"
        flash(f'Режим обслуживания {status}.', 'success')
    else:
        flash('Неверный секретный код!', 'error')
    return redirect(url_for('admin'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)