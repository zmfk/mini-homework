import os
import io
import zipfile
import requests
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24).hex())
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(DATA_DIR, 'database.db')
app.config['UPLOAD_FOLDER'] = os.path.join(DATA_DIR, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

def get_bj_time():
    return datetime.utcnow() + timedelta(hours=8)

def get_folder_size(folder_path):
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                try:
                    total_size += os.path.getsize(fp)
                except:
                    pass
    return round(total_size / (1024 * 1024), 2)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    class_name = db.Column(db.String(50), default='默认班级')

class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    publish_time = db.Column(db.DateTime, default=get_bj_time) 
    deadline = db.Column(db.DateTime, nullable=False)
    is_active = db.Column(db.Boolean, default=False)
    is_forced_closed = db.Column(db.Boolean, default=False) 
    is_archived = db.Column(db.Boolean, default=False)
    last_download_count = db.Column(db.Integer, default=0)
    class_name = db.Column(db.String(50), default='全部班级')

class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('assignment.id'), nullable=False)
    student_id = db.Column(db.String(50), nullable=False)
    student_name = db.Column(db.String(50), nullable=False)
    file_name = db.Column(db.String(150), nullable=False)
    submit_time = db.Column(db.DateTime, server_default=db.func.now())
    is_cleared = db.Column(db.Boolean, default=False) 

class SystemConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    is_maintenance = db.Column(db.Boolean, default=False)
    message = db.Column(db.String(255), default='系统正在进行维护升级，暂时无法访问。预计很快恢复，请稍后再试。')
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.before_request
def check_maintenance():
    # 移除了 change_password，使得维护期间学生无法修改密码
    allowed_endpoints = ['static', 'login', 'logout', 'maintenance_page']
    if request.endpoint in allowed_endpoints:
        return

    if current_user.is_authenticated and current_user.role in ['admin', 'teacher']:
        return

    try:
        config = SystemConfig.query.first()
        if config and config.is_maintenance:
            if request.method == 'POST' or request.path.startswith('/api/'):
                return jsonify({"error": "maintenance", "message": config.message}), 503
            return redirect(url_for('maintenance_page'))
    except Exception:
        pass 

@app.route('/maintenance')
def maintenance_page():
    config = SystemConfig.query.first()
    msg = config.message if config else '系统维护中...'
    start_str = config.start_time.strftime('%Y-%m-%d %H:%M') if config and config.start_time else None
    end_str = config.end_time.strftime('%Y-%m-%d %H:%M') if config and config.end_time else None
    return render_template('maintenance.html', message=msg, start_time=start_str, end_time=end_str)

def get_pa_stats():
    token = os.environ.get('PA_TOKEN')
    username = os.environ.get('PA_USERNAME')
    
    cpu_data = {}
    if token and username:
        try:
            headers = {'Authorization': f'Token {token}'}
            cpu_res = requests.get(f'https://www.pythonanywhere.com/api/v0/user/{username}/cpu/', headers=headers, timeout=3)
            if cpu_res.status_code == 200:
                cpu_data = cpu_res.json()
        except Exception:
            pass

    cpu_usage = cpu_data.get('daily_cpu_total_usage_seconds', 0)
    cpu_limit = cpu_data.get('daily_cpu_limit_seconds', 100)
    cpu_pct = round((cpu_usage / cpu_limit) * 100, 1) if cpu_limit else 0
    
    upload_size = get_folder_size(app.config['UPLOAD_FOLDER'])
    base_size = 5.25 
    app_disk_usage = round(base_size + upload_size, 2)
    
    disk_limit = 512.0
    disk_pct = round((app_disk_usage / disk_limit) * 100, 1)
    
    return {
        'cpu_usage': round(cpu_usage, 1),
        'cpu_limit': cpu_limit,
        'cpu_pct': cpu_pct,
        'cpu_color': 'bg-red-500' if cpu_pct > 70 else 'bg-yellow-400' if cpu_pct > 40 else 'bg-green-500',
        'disk_usage': app_disk_usage,
        'disk_limit': disk_limit,
        'disk_pct': disk_pct,
        'disk_color': 'bg-red-500' if disk_pct > 80 else 'bg-yellow-400' if disk_pct > 50 else 'bg-green-500',
        'api_connected': bool(token and username)
    }

@app.route('/')
def index():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return redirect(url_for(f'{current_user.role}'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            login_user(user)
            return redirect(url_for('index'))
        flash('学号或密码错误', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old_pwd = request.form.get('old_password')
        new_pwd = request.form.get('new_password')
        confirm_pwd = request.form.get('confirm_password')

        if not old_pwd or not new_pwd or not confirm_pwd:
            flash('所有输入框均为必填项！', 'error')
            return redirect(url_for('change_password'))

        if not check_password_hash(current_user.password_hash, old_pwd):
            flash('原密码错误，请重试！', 'error')
            return redirect(url_for('change_password'))

        if len(new_pwd.strip()) < 6:
            flash('新密码长度不能少于6位！', 'error')
            return redirect(url_for('change_password'))

        if new_pwd != confirm_pwd:
            flash('两次输入的新密码不一致！', 'error')
            return redirect(url_for('change_password'))

        if new_pwd == old_pwd:
            flash('新密码不能与原密码相同！', 'error')
            return redirect(url_for('change_password'))

        current_user.password_hash = generate_password_hash(new_pwd.strip())
        db.session.commit()
        
        flash('密码修改成功！请牢记新密码。', 'success')
        return redirect(url_for(f'{current_user.role}'))

    contact_email = os.environ.get('CONTACT_EMAIL', '')
    return render_template('change_password.html', contact_email=contact_email)

@app.route('/api/submission_count')
@login_required
def submission_count():
    if current_user.role not in ['admin', 'teacher']:
        return jsonify({"error": "unauthorized"}), 403
    total = Submission.query.count()
    return jsonify({"total": total})

@app.route('/admin/toggle_maintenance', methods=['POST'])
@login_required
def toggle_maintenance():
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    config = SystemConfig.query.first()
    if config:
        config.message = request.form.get('message', config.message).strip()
        
        start_str = request.form.get('start_time')
        end_str = request.form.get('end_time')
        
        if start_str:
            config.start_time = datetime.strptime(start_str, '%Y-%m-%dT%H:%M')
        if end_str:
            config.end_time = datetime.strptime(end_str, '%Y-%m-%dT%H:%M')
            
        config.is_maintenance = not config.is_maintenance
        db.session.commit()
        status = "开启" if config.is_maintenance else "关闭"
        flash(f'系统维护模式已{status}！', 'success')
    return redirect(url_for('admin'))

@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    
    if request.method == 'POST' and 'create_assignment' in request.form:
        title = request.form.get('title')
        deadline_str = request.form.get('deadline')
        target_class = request.form.get('target_class', '').strip()
        
        if not target_class:
            flash('发布失败：目标班级不能为空！如果要发给所有人，请填“全部班级”。', 'error')
            return redirect(url_for('admin'))

        if title and deadline_str:
            deadline = datetime.strptime(deadline_str, '%Y-%m-%dT%H:%M')
            db.session.add(Assignment(
                title=title, 
                deadline=deadline, 
                class_name=target_class,
                publish_time=get_bj_time()
            ))
            db.session.commit()
            flash('作业创建成功！', 'success')
            
    if request.method == 'POST' and 'batch_import' in request.form:
        student_data = request.form.get('student_data')
        lines = student_data.strip().split('\n')
        count = 0
        for line in lines:
            line_str = line.strip()
            if not line_str or '学号' in line_str or '姓名' in line_str:
                continue
            
            line_str = line_str.replace('\t', ' ').replace(',', ' ').replace('，', ' ')
            parts = line_str.split()
            
            if len(parts) >= 2:
                username = parts[0].strip()
                name = parts[1].strip()
                class_name = parts[2].strip() if len(parts) >= 3 else '默认班级'
                role = 'student'
                
                if len(parts) >= 4 and parts[3].strip().lower() in ['admin', 'teacher']:
                    role = parts[3].strip().lower()
                elif len(parts) == 3 and parts[2].strip().lower() in ['admin', 'teacher']:
                    role = parts[2].strip().lower()
                    class_name = '默认班级'

                if not User.query.filter_by(username=username).first():
                    new_user = User(
                        username=username,
                        password_hash=generate_password_hash(username),
                        role=role,
                        name=name,
                        class_name=class_name
                    )
                    db.session.add(new_user)
                    count += 1
                else:
                    existing_user = User.query.filter_by(username=username).first()
                    existing_user.class_name = class_name
                    if role != 'student':
                        existing_user.role = role
        db.session.commit()
        flash(f'成功处理 {count} 名新用户！', 'success')

    if request.method == 'POST' and 'change_password' in request.form:
        user = User.query.get(request.form.get('user_id'))
        action = request.form.get('action')
        if user:
            if action == 'reset':
                user.password_hash = generate_password_hash(user.username)
                db.session.commit()
                flash(f'已将 {user.name} 的密码重置为学号', 'success')
            elif action == 'change':
                new_pwd = request.form.get('new_password')
                if new_pwd:
                    user.password_hash = generate_password_hash(new_pwd)
                    db.session.commit()
                    flash(f'已修改 {user.name} 的密码', 'success')

    assignments = Assignment.query.order_by(Assignment.id.desc()).all()
    users = User.query.order_by(User.role.asc(), User.class_name.asc(), User.username.asc()).all()
    
    all_classes = sorted(list(set([u.class_name for u in users if u.role in ['student', 'admin'] and u.username != 'admin' and u.class_name])))
    student_class_map = {u.username: u.class_name for u in users if u.role in ['student', 'admin'] and u.username != 'admin'}
    
    stats = {}
    report = {}
    need_backup = []
    now_bj = get_bj_time()
    
    for a in assignments:
        subs = Submission.query.filter_by(assignment_id=a.id).all()
        submitted_ids = [s.student_id for s in subs]
        
        for sub in subs:
            sub.class_name = student_class_map.get(sub.student_id, '默认班级')
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
            sub.file_exists = os.path.exists(filepath)
            sub_time_bj = sub.submit_time + timedelta(hours=8)
            sub.is_late = sub_time_bj > a.deadline
        
        if a.class_name == '全部班级':
            target_students = [u for u in users if u.role in ['student', 'admin'] and u.username != 'admin']
        else:
            target_students = [u for u in users if u.role in ['student', 'admin'] and u.username != 'admin' and u.class_name == a.class_name]
            
        not_submitted = [s for s in target_students if s.username not in submitted_ids]
        
        stats[a.id] = {'total': len(subs), 'files': [s.file_name for s in subs]}
        
        subs.sort(key=lambda x: (x.class_name, x.student_id))
        not_submitted.sort(key=lambda x: (x.class_name, x.username))
        
        # 提前计算丢失文件名单，方便前端调用
        lost_names = [sub.student_name for sub in subs if not sub.file_exists and not sub.is_cleared]
        
        report[a.id] = {
            'submitted': subs, 
            'not_submitted': not_submitted,
            'lost_names': lost_names,
            'lost_names_str': '、'.join(lost_names)
        }
        
        is_closed = a.is_forced_closed or (now_bj > a.deadline and not a.is_active)
        if is_closed and len(subs) > 0 and len(subs) > a.last_download_count:
            need_backup.append(a.title)

    pa_stats = get_pa_stats()
    current_total_subs = Submission.query.count()
    sys_config = SystemConfig.query.first()

    return render_template('admin.html', 
                           users=users, 
                           assignments=assignments, 
                           stats=stats, 
                           report=report, 
                           now=now_bj,
                           need_backup=need_backup,
                           current_total_subs=current_total_subs,
                           pa_stats=pa_stats,
                           all_classes=all_classes,
                           sys_config=sys_config)

@app.route('/admin/export_summary')
@login_required
def export_summary():
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    
    users = User.query.order_by(User.class_name.asc(), User.username.asc()).all()
    assignments = Assignment.query.order_by(Assignment.id.asc()).all()
    
    all_classes = sorted(list(set([u.class_name for u in users if u.role in ['student', 'admin'] and u.username != 'admin' and u.class_name])))
    
    style_submitted = {'font': Font(color="000000")} 
    style_late = {'font': Font(color="000000")} 
    style_pending = {'font': Font(color="ED7D31", bold=True)} 
    style_overdue = {
        'fill': PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"), 
        'font': Font(color="9C0006", bold=True) 
    }
    
    now_bj = get_bj_time()

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cls in all_classes:
            students = [u for u in users if u.role in ['student', 'admin'] and u.username != 'admin' and u.class_name == cls]
            if not students:
                continue
                
            cls_assignments = [a for a in assignments if a.class_name == cls or a.class_name == '全部班级']
            
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = f"{cls}班总表"
            
            header = ['学号', '姓名'] + [a.title for a in cls_assignments]
            ws.append(header)
            
            header_fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal='center', vertical='center')
            
            for stu in students:
                row = [str(stu.username), stu.name]
                col_styles = [] 
                
                for a in cls_assignments:
                    is_overdue = now_bj > a.deadline
                    
                    sub = Submission.query.filter_by(assignment_id=a.id, student_id=stu.username).first()
                    if sub:
                        sub_time_bj = sub.submit_time + timedelta(hours=8)
                        if sub_time_bj > a.deadline:
                            row.append('已交(补交)')
                            col_styles.append(style_late)
                        else:
                            row.append('已交')
                            col_styles.append(style_submitted)
                    else:
                        row.append('未交')
                        if is_overdue:
                            col_styles.append(style_overdue) 
                        else:
                            col_styles.append(style_pending) 
                            
                ws.append(row)
                
                current_row = ws.max_row
                for idx, style_dict in enumerate(col_styles):
                    cell = ws.cell(row=current_row, column=idx + 3)
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    if 'fill' in style_dict:
                        cell.fill = style_dict['fill']
                    if 'font' in style_dict:
                        cell.font = style_dict['font']
                
            for col_idx, column_cells in enumerate(ws.columns, 1):
                max_length = 0
                column_letter = get_column_letter(col_idx)
                for cell in column_cells:
                    try:
                        if cell.value:
                            length = sum(2 if ord(c) > 127 else 1 for c in str(cell.value))
                            if length > max_length:
                                max_length = length
                    except:
                        pass
                ws.column_dimensions[column_letter].width = max_length + 4
                
            excel_stream = io.BytesIO()
            wb.save(excel_stream)
            excel_stream.seek(0)
            
            zf.writestr(f"{cls}_作业总表.xlsx", excel_stream.getvalue())
            
    memory_file.seek(0)
    return send_file(
        memory_file, 
        mimetype='application/zip', 
        as_attachment=True, 
        download_name=f"分班作业总表_{get_bj_time().strftime('%Y%m%d')}.zip"
    )

@app.route('/admin/toggle_archive/<int:assignment_id>')
@login_required
def toggle_archive(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(assignment_id)
    if a:
        if not a.is_archived:
            now_bj = get_bj_time()
            is_closed = a.is_forced_closed or (now_bj > a.deadline and not a.is_active)
            if not is_closed:
                flash('🚨 归档失败：该作业目前仍在开放中！请先点击【强制截止】或等待截止时间结束，再进行归档整理。', 'error')
                return redirect(request.referrer or url_for('admin'))
        
        a.is_archived = not a.is_archived
        db.session.commit()
        status = "已归档" if a.is_archived else "已取消归档"
        flash(f'作业 [{a.title}] {status}', 'success')
    return redirect(request.referrer or url_for('admin'))

@app.route('/admin/delete_user/<int:user_id>')
@login_required
def delete_user(user_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    user = User.query.get(user_id)
    if user and user.username != 'admin' and user.username != current_user.username:
        db.session.delete(user)
        db.session.commit()
        flash(f'用户 {user.name} 已删除', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/delete_assignment/<int:id>')
@login_required
def delete_assignment(id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(id)
    if a:
        subs = Submission.query.filter_by(assignment_id=id).all()
        if len(subs) > 0 and len(subs) > a.last_download_count:
            flash('🚨 危险操作被拦截：该作业还有未打包下载的文件，为防止数据丢失，禁止删除！请先打包下载。', 'error')
            return redirect(url_for('admin'))

        for sub in subs:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
            if os.path.exists(filepath): os.remove(filepath)
            db.session.delete(sub)
        db.session.delete(a)
        db.session.commit()
        flash('作业及所有提交文件已彻底删除', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/clear_files/<int:assignment_id>')
@login_required
def clear_files(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(assignment_id)
    subs = Submission.query.filter_by(assignment_id=assignment_id).all()
    count = 0
    
    if len(subs) > a.last_download_count:
        flash('🚨 危险操作被拦截：该作业还有未打包下载的文件，为防止数据丢失，禁止清空！请先打包下载。', 'error')
        return redirect(url_for('admin'))

    for sub in subs:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
        if os.path.exists(filepath):
            os.remove(filepath)
            count += 1
        sub.is_cleared = True
    
    db.session.commit()
    flash(f'已清空 {count} 个物理文件，已交名单和记录已保留！', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/mark_lost_as_cleared/<int:assignment_id>')
@login_required
def mark_lost_as_cleared(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    subs = Submission.query.filter_by(assignment_id=assignment_id).all()
    fixed_count = 0
    for sub in subs:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
        if not os.path.exists(filepath) and not sub.is_cleared:
            sub.is_cleared = True
            fixed_count += 1
    db.session.commit()
    flash(f'已将 {fixed_count} 个丢失的文件记录标记为“管理员已清理”，名单已保留。', 'success')
    return redirect(request.referrer or url_for('admin'))

@app.route('/admin/toggle_assignment/<int:id>')
@login_required
def toggle_assignment(id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(id)
    if a:
        if a.is_active:
            a.is_active = False
        else:
            a.is_active = True
            a.is_forced_closed = False 
        db.session.commit()
        flash(f'作业 [{a.title}] 状态已切换', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/force_close/<int:assignment_id>')
@login_required
def force_close(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(assignment_id)
    if a:
        a.is_active = False
        a.is_forced_closed = True
        db.session.commit()
        flash(f'作业 [{a.title}] 已强制截止', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/cancel_force_close/<int:assignment_id>')
@login_required
def cancel_force_close(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(assignment_id)
    if a:
        a.is_forced_closed = False
        db.session.commit()
        flash(f'作业 [{a.title}] 已解除强制截止', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/update_deadline/<int:assignment_id>', methods=['POST'])
@login_required
def update_deadline(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(assignment_id)
    new_time_str = request.form.get('new_deadline')
    if a and new_time_str:
        try:
            new_time = datetime.strptime(new_time_str, '%Y-%m-%dT%H:%M')
            a.deadline = new_time
            a.is_forced_closed = False 
            db.session.commit()
            flash(f'作业 [{a.title}] 截止时间已更新', 'success')
        except Exception:
            flash('时间格式错误，更新失败', 'error')
    return redirect(url_for('admin'))

@app.route('/admin/update_class/<int:assignment_id>', methods=['POST'])
@login_required
def update_class(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    a = Assignment.query.get(assignment_id)
    new_class = request.form.get('new_class', '').strip()
    if a:
        if not new_class:
            new_class = '全部班级'
        a.class_name = new_class
        db.session.commit()
        flash(f'作业 [{a.title}] 目标班级已修改为: {new_class}', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/delete_submission/<int:submission_id>')
@login_required
def delete_submission(submission_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限访问", 403
    sub = Submission.query.get(submission_id)
    if sub:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
        if os.path.exists(filepath): os.remove(filepath)
        db.session.delete(sub)
        db.session.commit()
        flash(f'已删除 {sub.student_name} 的提交记录。', 'success')
    return redirect(request.referrer or url_for('admin'))

@app.route('/teacher')
@login_required
def teacher():
    if current_user.role != 'teacher': return "无权限访问", 403
    assignments = Assignment.query.order_by(Assignment.id.desc()).all()
    submissions = Submission.query.all()
    
    all_students = User.query.filter(User.role.in_(['student', 'admin']), User.username != 'admin').all()
    student_class_map = {u.username: u.class_name for u in all_students}
    all_classes = sorted(list(set([u.class_name for u in all_students if u.class_name])))
    
    now_bj = get_bj_time()
    report = {}
    for a in assignments:
        sub_list = [s for s in submissions if s.assignment_id == a.id]
        submitted_ids = [s.student_id for s in sub_list]
        
        for sub in sub_list:
            sub.class_name = student_class_map.get(sub.student_id, '默认班级')
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
            sub.file_exists = os.path.exists(filepath)
            sub_time_bj = sub.submit_time + timedelta(hours=8)
            sub.is_late = sub_time_bj > a.deadline
        
        if a.class_name == '全部班级':
            target_students = all_students
        else:
            target_students = [s for s in all_students if s.class_name == a.class_name]
            
        not_submitted = [s for s in target_students if s.username not in submitted_ids]
        
        sub_list.sort(key=lambda x: (x.class_name, x.student_id))
        not_submitted.sort(key=lambda x: (x.class_name, x.username))
            
        report[a.id] = {'submitted': sub_list, 'not_submitted': not_submitted}
        
    current_total_subs = Submission.query.count()
    return render_template('teacher.html', assignments=assignments, report=report, now=now_bj, current_total_subs=current_total_subs, all_classes=all_classes)

@app.route('/student', methods=['GET', 'POST'])
@login_required
def student():
    if current_user.role not in ['student', 'admin']: 
        flash('老师不需要交作业哦！', 'error')
        return redirect(url_for('teacher'))
        
    if request.method == 'POST':
        assignment_id = request.form.get('assignment_id')
        file = request.files.get('file')
        assignment = Assignment.query.get(assignment_id)
        
        now_bj = get_bj_time()
        if not assignment or (assignment.class_name != '全部班级' and assignment.class_name != current_user.class_name):
            flash('越权操作：该作业不属于你的班级！', 'error')
            return redirect(url_for('student'))
            
        if assignment.is_forced_closed or (now_bj > assignment.deadline and not assignment.is_active):
            flash('该作业已截止提交！', 'error')
            return redirect(url_for('student'))

        if file and file.filename.lower().endswith(('.pdf', '.doc', '.docx')):
            ext = os.path.splitext(file.filename)[1].lower()
            
            if ext == '.docx':
                try:
                    with zipfile.ZipFile(file.stream) as zf:
                        if 'word/vbaProject.bin' in zf.namelist():
                            flash('检测到文件包含恶意宏，已被拒绝提交！', 'error')
                            return redirect(url_for('student'))
                except Exception:
                    flash('文件损坏或格式不正确！', 'error')
                    return redirect(url_for('student'))
                finally:
                    file.stream.seek(0) 

            safe_title = assignment.title
            for char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|', '\n', '\r']:
                safe_title = safe_title.replace(char, '')
            filename = f"{current_user.username}_{current_user.name}_{safe_title}{ext}"
            
            try:
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            except Exception as e:
                flash(f'文件保存失败，请联系老师。错误: {str(e)}', 'error')
                return redirect(url_for('student'))
            
            old_sub = Submission.query.filter_by(assignment_id=assignment_id, student_id=current_user.username).first()
            if old_sub:
                old_path = os.path.join(app.config['UPLOAD_FOLDER'], old_sub.file_name)
                if os.path.exists(old_path): os.remove(old_path)
                db.session.delete(old_sub)
            
            db.session.add(Submission(assignment_id=assignment_id, student_id=current_user.username, student_name=current_user.name, file_name=filename, is_cleared=False))
            db.session.commit()
            flash('作业提交成功！', 'success')
        else:
            flash('只支持上传 PDF, DOC, DOCX 格式！', 'error')
            
    assignments = Assignment.query.filter(
        (Assignment.class_name == current_user.class_name) | (Assignment.class_name == '全部班级')
    ).order_by(Assignment.id.desc()).all()
    
    my_subs = {s.assignment_id: s for s in Submission.query.filter_by(student_id=current_user.username).all()}
    
    for s in my_subs.values():
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], s.file_name)
        s.file_exists = os.path.exists(filepath)
        
    now_bj = get_bj_time()

    pending_assignments = []
    overdue_assignments = []
    submitted_assignments = []
    lost_files = [] 

    for a in assignments:
        sub = my_subs.get(a.id)
        is_expired = a.is_forced_closed or (now_bj > a.deadline and not a.is_active)
        
        if sub:
            submitted_assignments.append(a)
            if not sub.file_exists and not sub.is_cleared:
                lost_files.append(a)
        elif is_expired:
            overdue_assignments.append(a)
        else:
            pending_assignments.append(a)

    total_count = len(assignments)
    submitted_count = len(submitted_assignments)
    pending_count = len(pending_assignments)
    overdue_count = len(overdue_assignments)

    percent = 0
    if total_count > 0:
        percent = int(round(submitted_count / total_count * 100))

    return render_template('student.html', 
                           pending_assignments=pending_assignments,
                           overdue_assignments=overdue_assignments,
                           submitted_assignments=submitted_assignments,
                           lost_files=lost_files, 
                           my_subs=my_subs, 
                           now=now_bj,
                           total_count=total_count,
                           submitted_count=submitted_count,
                           pending_count=pending_count,
                           overdue_count=overdue_count,
                           percent=percent)

@app.route('/student/delete/<int:assignment_id>')
@login_required
def student_delete(assignment_id):
    if current_user.role not in ['student', 'admin']: return "无权限访问", 403
    sub = Submission.query.filter_by(assignment_id=assignment_id, student_id=current_user.username).first()
    if sub:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
        if os.path.exists(filepath): os.remove(filepath)
        db.session.delete(sub)
        db.session.commit()
        flash('提交已撤回，你可以重新上传了。', 'success')
    return redirect(url_for('student'))

@app.route('/download/<filename>')
@login_required
def download_file(filename):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(filepath):
        flash('文件不存在或已被清理！', 'error')
        return redirect(request.referrer or url_for('index'))

    if current_user.role not in ['admin', 'teacher']:
        sub = Submission.query.filter_by(student_id=current_user.username, file_name=filename).first()
        if not sub:
            flash('安全拦截：你无权下载其他人的作业！', 'error')
            return redirect(request.referrer or url_for('index'))

    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

@app.route('/download_zip/<int:assignment_id>')
@login_required
def download_zip(assignment_id):
    if current_user.role not in ['admin', 'teacher']: return "无权限", 403
    assignment = Assignment.query.get(assignment_id)
    subs = Submission.query.filter_by(assignment_id=assignment_id).all()
    
    if not subs:
        flash('该作业暂无学生提交，无法打包。', 'error')
        return redirect(request.referrer)

    assignment.last_download_count = len(subs)
    db.session.commit()

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        all_students = User.query.filter(User.role.in_(['student', 'admin']), User.username != 'admin').all()
        student_class_map = {u.username: u.class_name for u in all_students}
        
        if assignment.class_name != '全部班级':
            target_students = [s for s in all_students if s.class_name == assignment.class_name]
            sub_ids = [s.student_id for s in subs]
            not_sub = [f"{s.username}-{s.name}" for s in target_students if s.username not in sub_ids]
            
            submitted_lines = []
            for s in subs:
                sub_time_bj = s.submit_time + timedelta(hours=8)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], s.file_name)
                file_exists = os.path.exists(filepath)
                
                if sub_time_bj > assignment.deadline:
                    late_tag = " (补交)"
                elif not file_exists:
                    late_tag = " (已清理)" if s.is_cleared else " (文件丢失)"
                else:
                    late_tag = ""
                    
                submitted_lines.append(f"{s.student_id}-{s.student_name}{late_tag}")
            
            list_content = f"【{assignment.title}】(目标: {assignment.class_name})\n\n已交名单({len(subs)}人):\n" + "\n".join(submitted_lines)
            list_content += f"\n\n未交名单({len(not_sub)}人):\n" + "\n".join(not_sub)
            zf.writestr('名单汇总.txt', list_content.encode('utf-8'))
            
            for sub in subs:
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
                if os.path.exists(filepath):
                    zf.write(filepath, sub.file_name)
        else:
            classes_involved = sorted(list(set([u.class_name for u in all_students if u.class_name])))
            
            for cls in classes_involved:
                cls_students = [s for s in all_students if s.class_name == cls]
                cls_subs = [s for s in subs if student_class_map.get(s.student_id) == cls]
                cls_sub_ids = [s.student_id for s in cls_subs]
                cls_not_sub = [f"{s.username}-{s.name}" for s in cls_students if s.username not in cls_sub_ids]
                
                cls_submitted_lines = []
                for s in cls_subs:
                    sub_time_bj = s.submit_time + timedelta(hours=8)
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], s.file_name)
                    file_exists = os.path.exists(filepath)
                    
                    if sub_time_bj > assignment.deadline:
                        late_tag = " (补交)"
                    elif not file_exists:
                        late_tag = " (已清理)" if s.is_cleared else " (文件丢失)"
                    else:
                        late_tag = ""
                        
                    cls_submitted_lines.append(f"{s.student_id}-{s.student_name}{late_tag}")
                
                cls_list_content = f"【{assignment.title}】- {cls}班\n\n已交名单({len(cls_subs)}人):\n" + "\n".join(cls_submitted_lines)
                cls_list_content += f"\n\n未交名单({len(cls_not_sub)}人):\n" + "\n".join(cls_not_sub)
                
                zf.writestr(f"{cls}/名单汇总_{cls}.txt", cls_list_content.encode('utf-8'))
                
                for sub in cls_subs:
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], sub.file_name)
                    if os.path.exists(filepath):
                        zf.write(filepath, f"{cls}/{sub.file_name}")
                
    memory_file.seek(0)
    
    safe_class = assignment.class_name.replace('/', '_').replace('\\', '_')
    safe_title = assignment.title.replace('/', '_').replace('\\', '_')
    
    return send_file(memory_file, download_name=f"{safe_class}_{safe_title}_打包.zip", as_attachment=True)

with app.app_context():
    db.create_all()
    
    db_path = os.path.join(DATA_DIR, 'database.db')
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA table_info(user)")
        user_columns = [col[1] for col in cursor.fetchall()]
        if 'class_name' not in user_columns:
            cursor.execute("ALTER TABLE user ADD COLUMN class_name VARCHAR(50) DEFAULT '默认班级'")
            conn.commit()
            
        cursor.execute("PRAGMA table_info(assignment)")
        assign_columns = [col[1] for col in cursor.fetchall()]
        if 'class_name' not in assign_columns:
            cursor.execute("ALTER TABLE assignment ADD COLUMN class_name VARCHAR(50) DEFAULT '全部班级'")
            conn.commit()
        if 'is_forced_closed' not in assign_columns:
            cursor.execute("ALTER TABLE assignment ADD COLUMN is_forced_closed BOOLEAN DEFAULT 0")
            conn.commit()
        if 'is_archived' not in assign_columns:
            cursor.execute("ALTER TABLE assignment ADD COLUMN is_archived BOOLEAN DEFAULT 0")
            conn.commit()
        if 'publish_time' not in assign_columns:
            cursor.execute("ALTER TABLE assignment ADD COLUMN publish_time DATETIME")
            cursor.execute("UPDATE assignment SET publish_time = CURRENT_TIMESTAMP WHERE publish_time IS NULL")
            conn.commit()

        cursor.execute("PRAGMA table_info(submission)")
        sub_columns = [col[1] for col in cursor.fetchall()]
        if 'is_cleared' not in sub_columns:
            cursor.execute("ALTER TABLE submission ADD COLUMN is_cleared BOOLEAN DEFAULT 0")
            conn.commit()

        cursor.execute("PRAGMA table_info(system_config)")
        sys_columns = [col[1] for col in cursor.fetchall()]
        if sys_columns: 
            if 'start_time' not in sys_columns:
                cursor.execute("ALTER TABLE system_config ADD COLUMN start_time DATETIME")
                conn.commit()
            if 'end_time' not in sys_columns:
                cursor.execute("ALTER TABLE system_config ADD COLUMN end_time DATETIME")
                conn.commit()
            
        conn.close()

    if not User.query.filter_by(username='admin').first():
        db.session.add(User(username='admin', password_hash=generate_password_hash('admin123'), role='admin', name='超级管理员', class_name='系统'))
        db.session.commit()
    
    if not SystemConfig.query.first():
        db.session.add(SystemConfig())
        db.session.commit()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)