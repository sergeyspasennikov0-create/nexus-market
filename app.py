import sqlite3
import random
import os
import time
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_vr_project'

# --- НАСТРОЙКИ ПОЧТЫ ---
app.config['MAIL_SERVER'] = 'smtp.mail.ru'
app.config['MAIL_PORT'] = 465
app.config['MAIL_USE_SSL'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USER')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASS')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USER')
app.config['MAIL_SUPPRESS_SEND'] = False
mail = Mail(app)

login_attempts = {}
MAINTENANCE_MODE = False # Глобальный переключатель обслуживания
SUPER_ADMIN_EMAIL = 'admin@nexus-market.ru' # Твой неприкосновенный аккаунт

def init_db():
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    # Обновленная таблица пользователей (добавлен статус бана)
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, phone TEXT, password TEXT, role TEXT, is_banned INTEGER DEFAULT 0)''')
    # Обновленная таблица товаров (добавлен ID автора)
    c.execute('''CREATE TABLE IF NOT EXISTS products 
                 (id INTEGER PRIMARY KEY, title TEXT, description TEXT, price INTEGER, icon TEXT, author_name TEXT)''')
    
    # Проверяем, есть ли Супер-Админ
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

# Проверка режима обслуживания перед каждым запросом
@app.before_request
def check_maintenance():
    global MAINTENANCE_MODE
    if MAINTENANCE_MODE:
        # Если сайт выключен, пускаем только админов на их страницы
        if session.get('user_role') not in ['admin', 'super_admin']:
            if request.endpoint not in ['maintenance', 'static']:
                return render_template('maintenance.html'), 503

@app.route('/maintenance')
def maintenance():
    return render_template('maintenance.html')

@app.route('/')
def index():
    if MAINTENANCE_MODE and session.get('user_role') not in ['admin', 'super_admin']:
        return render_template('maintenance.html'), 503
        
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("SELECT * FROM products")
    products = c.fetchall()
    conn.close()
    return render_template('index.html', products=products)

@app.route('/health')
def health():
    return "OK", 200

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if 'consent' not in request.form:
            flash('Вы должны согласиться на обработку данных!', 'error')
            return redirect(url_for('register'))

        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        
        code = str(random.randint(1000, 9999))
        verification_codes[email] = code
        
        try:
            msg = Message('Код NEXUS Market', recipients=[email])
            msg.body = f'Ваш код: {code}'
            mail.send(msg)
        except Exception as e:
            flash('Ошибка отправки почты. Проверьте адрес.', 'error')
            return redirect(url_for('register'))
        
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
        hashed_pw = generate_password_hash(request.form['password'])
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
    user_ip = request.remote_addr
    current_time = time.time()

    if user_ip in login_attempts and current_time < login_attempts[user_ip].get('lock_until', 0):
        remaining = int(login_attempts[user_ip]['lock_until'] - current_time)
        flash(f'Заблокировано. Ждите {remaining} сек.', 'error')
        return render_template('auth.html', mode='login', lockout_time=remaining)

    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = sqlite3.connect('store.db')
        c = conn.cursor()
        c.execute("SELECT name, password, role, is_banned FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()
        
        if user:
            if user[3] == 1: # Если забанен
                flash('Ваш аккаунт заблокирован администрацией!', 'error')
                return render_template('auth.html', mode='login')
                
            if check_password_hash(user[1], password):
                login_attempts[user_ip] = {'count': 0, 'lock_until': 0}
                session['user_name'] = user[0]
                session['user_role'] = user[2]
                session['user_email'] = email
                return redirect(url_for('index'))
        
        # Если не пустило
        if user_ip not in login_attempts: login_attempts[user_ip] = {'count': 0, 'lock_until': 0}
        login_attempts[user_ip]['count'] += 1
        if login_attempts[user_ip]['count'] >= 10:
            login_attempts[user_ip]['lock_until'] = current_time + 1800
            return render_template('auth.html', mode='login', lockout_time=1800)
        else:
            flash(f'Неверные данные. Осталось попыток: {10 - login_attempts[user_ip]["count"]}', 'error')

    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# --- ЛИЧНЫЙ КАБИНЕТ (Продажа для пользователей) ---
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

# --- АДМИНКА ---
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
    conn.close()
    
    return render_template('admin.html', products=products, users=users, search_query=search_query, maintenance=MAINTENANCE_MODE)

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

# --- ДЕЙСТВИЯ С ПОЛЬЗОВАТЕЛЯМИ (ЗАЩИТА СУПЕР-АДМИНА) ---
@app.route('/action/ban/<int:id>')
def ban_user(id):
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    # Проверяем почту того, кого банят. Если это Супер-Админ - запрещаем.
    c.execute("SELECT email FROM users WHERE id = ?", (id,))
    target_email = c.fetchone()[0]
    
    if target_email == SUPER_ADMIN_EMAIL:
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
    c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (id,))
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
    target_email = c.fetchone()[0]
    
    if target_email == SUPER_ADMIN_EMAIL:
        flash('ОШИБКА: Нельзя снять права с Главного Босса!', 'error')
    else:
        c.execute("UPDATE users SET role = 'user' WHERE id = ?", (id,))
        conn.commit()
        flash('Права администратора сняты.', 'success')
    conn.close()
    return redirect(url_for('admin'))

# --- УПРАВЛЕНИЕ СЕРВЕРОМ ---
@app.route('/action/toggle_maintenance', methods=['POST'])
def toggle_maintenance():
    global MAINTENANCE_MODE
    if session.get('user_role') not in ['admin', 'super_admin']: return redirect(url_for('index'))
    
    code = request.form.get('code')
    if code == 'STOP-2026': # Секретный код для остановки
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        status = "ВКЛЮЧЕН" if MAINTENANCE_MODE else "ВЫКЛЮЧЕН"
        flash(f'Режим обслуживания {status}.', 'success')
    else:
        flash('Неверный секретный код!', 'error')
    return redirect(url_for('admin'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)