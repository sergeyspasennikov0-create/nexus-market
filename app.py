import sqlite3
import random
import os # <-- Добавили для работы с сервером
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_vr_project'

def init_db():
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, phone TEXT, password TEXT, role TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products 
                 (id INTEGER PRIMARY KEY, title TEXT, description TEXT, price INTEGER, icon TEXT)''')
    
    c.execute("SELECT COUNT(*) FROM products")
    if c.fetchone()[0] == 0:
        products = [
            ('Аккаунт Discord с Nitro', 'Полный доступ, почта в комплекте.', 1500, '🎮'),
            ('Прокачка приложения на Python', 'Напишу простой бэкенд для вашего проекта.', 5000, '🐍'),
            ('VR-очки NEXUS Pro', 'Погружение в виртуальную реальность.', 45000, '🥽'),
            ('Настройка компьютера', 'Установка Windows и нужных программ.', 2000, '💻')
        ]
        c.executemany("INSERT INTO products (title, description, price, icon) VALUES (?, ?, ?, ?)", products)
    conn.commit()
    conn.close()

init_db()
verification_codes = {}

@app.route('/')
def index():
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("SELECT * FROM products")
    products = c.fetchall()
    conn.close()
    return render_template('index.html', products=products)

# --- НОВЫЙ МАРШРУТ ДЛЯ UPTIMEROBOT ---
# Этот маршрут нужен, чтобы "будильник" проверял сайт и не давал ему уснуть
@app.route('/health')
def health():
    return "OK", 200
# -------------------------------------

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        
        code = str(random.randint(1000, 9999))
        verification_codes[email] = code
        
        print(f"\n=========================================")
        print(f"🔥 КОД ПОДТВЕРЖДЕНИЯ ДЛЯ {email}: {code}")
        print(f"=========================================\n")
        
        session['reg_name'] = name
        session['reg_email'] = email
        session['reg_phone'] = phone
        
        return redirect(url_for('verify'))
    return render_template('auth.html', mode='register')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if 'reg_email' not in session: return redirect(url_for('register'))
    if request.method == 'POST':
        user_code = request.form['code']
        email = session['reg_email']
        if verification_codes.get(email) == user_code:
            session['verified_email'] = email
            return redirect(url_for('set_password'))
        else:
            flash('Неверный код! Попробуйте снова.', 'error')
    return render_template('auth.html', mode='verify')

@app.route('/set_password', methods=['GET', 'POST'])
def set_password():
    if 'verified_email' not in session: return redirect(url_for('register'))
    if request.method == 'POST':
        password = request.form['password']
        
        name = session.get('reg_name')
        email = session.get('verified_email')
        phone = session.get('reg_phone')
        hashed_pw = generate_password_hash(password)
        
        try:
            conn = sqlite3.connect('store.db')
            c = conn.cursor()
            
            c.execute("SELECT COUNT(*) FROM users")
            is_first_user = c.fetchone()[0] == 0
            role = 'admin' if is_first_user else 'user'
            
            c.execute("INSERT INTO users (name, email, phone, password, role) VALUES (?, ?, ?, ?, ?)", 
                      (name, email, phone, hashed_pw, role))
            conn.commit()
            conn.close()
            
            session.pop('reg_email', None)
            session.pop('verified_email', None)
            session.pop('reg_name', None)
            session.pop('reg_phone', None)
            
            session['user_name'] = name
            session['user_role'] = role
            return redirect(url_for('index'))
            
        except sqlite3.IntegrityError:
            flash('Эта почта уже зарегистрирована!', 'error')
            return redirect(url_for('register'))
            
    return render_template('auth.html', mode='password')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = sqlite3.connect('store.db')
        c = conn.cursor()
        c.execute("SELECT name, password, role FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()
        
        if user and check_password_hash(user[1], password):
            session['user_name'] = user[0]
            session['user_role'] = user[2]
            return redirect(url_for('index'))
        else:
            flash('Неверная почта или пароль', 'error')
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/admin')
def admin():
    if session.get('user_role') != 'admin':
        flash('У вас нет доступа к этой странице!', 'error')
        return redirect(url_for('index'))
    return render_template('admin.html')

@app.route('/add_product', methods=['POST'])
def add_product():
    if session.get('user_role') != 'admin':
        return redirect(url_for('index'))
        
    title = request.form['title']
    description = request.form['description']
    price = request.form['price']
    icon = request.form['icon']
    
    conn = sqlite3.connect('store.db')
    c = conn.cursor()
    c.execute("INSERT INTO products (title, description, price, icon) VALUES (?, ?, ?, ?)", 
              (title, description, price, icon))
    conn.commit()
    conn.close()
    
    flash('Товар успешно добавлен!', 'success')
    return redirect(url_for('admin'))

# --- ОБНОВЛЕННЫЙ ЗАПУСК ДЛЯ RENDER ---
if __name__ == '__main__':
    # Render сам выдает порт, поэтому мы его запрашиваем
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)