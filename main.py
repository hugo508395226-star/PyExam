import json
import random
import io
import os
import re
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash

from config import SECRET_KEY, PORT, DEBUG
from database import init_db, query, query_one, count, insert
from sandbox import scan_code, run_code_sandbox
from analysis import analyze_code_quality, detect_ai_code, compute_similarity, build_similarity_matrix, generate_diff
import seed as seed_module

app = Flask(__name__)
app.secret_key = SECRET_KEY

@app.template_filter('from_json')
def from_json_filter(s):
    if not s:
        return []
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return []

chart_cache = {}
chart_cache_lock = threading.Lock()

executor = ThreadPoolExecutor(max_workers=5)

_baked_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'baked_scenarios.json')
_baked_data = None
_version_tracker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'version_tracker.json')

def _load_baked():
    global _baked_data
    if _baked_data is None:
        with open(_baked_path, 'r', encoding='utf-8') as f:
            _baked_data = json.load(f)

def _get_version(scenario):
    try:
        with open(_version_tracker_path, 'r', encoding='utf-8') as f:
            tracker = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        tracker = {}
    current = tracker.get(str(scenario), 'A')
    next_ver = 'B' if current == 'A' else 'A'
    tracker[str(scenario)] = next_ver
    with open(_version_tracker_path, 'w', encoding='utf-8') as f:
        json.dump(tracker, f)
    return current

DEMO_EXAM_TITLE = '【演示】标准100分试卷'

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def teacher_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'teacher':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def student_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'student':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    role = session.get('role')
    if role == 'teacher':
        return redirect(url_for('teacher_dashboard'))
    return redirect(url_for('student_dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = query_one("SELECT * FROM users WHERE username=?", (username,))
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['display_name'] = user['display_name'] or user['username']
            if user['role'] == 'teacher':
                return redirect(url_for('teacher_dashboard'))
            return redirect(url_for('student_dashboard'))
        return render_template('login.html', error='用户名或密码错误')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/switch_role/<target_role>')
@login_required
def switch_role(target_role):
    current = session.get('username', '')
    if current == 'guozicheng_teacher' and target_role == 'student':
        student = query_one("SELECT * FROM users WHERE linked_user_id=?", (session['user_id'],))
        if student:
            session['user_id'] = student['id']
            session['username'] = student['username']
            session['role'] = 'student'
            session['display_name'] = student['display_name'] or student['username']
    elif current == 'guozicheng_student' and target_role == 'teacher':
        teacher = query_one("SELECT * FROM users WHERE id=(SELECT linked_user_id FROM users WHERE id=?)", (session['user_id'],))
        if teacher:
            session['user_id'] = teacher['id']
            session['username'] = teacher['username']
            session['role'] = 'teacher'
            session['display_name'] = teacher['display_name'] or teacher['username']
    return redirect(url_for('index'))

@app.route('/teacher')
@teacher_required
def teacher_dashboard():
    exams = query("SELECT * FROM exams WHERE teacher_id=? ORDER BY created_at DESC", (session['user_id'],))
    return render_template('teacher_dashboard.html', exams=exams)

@app.route('/teacher/questions')
@teacher_required
def teacher_questions():
    qtype = request.args.get('type', '')
    page = int(request.args.get('page', 1))
    per_page = 20
    if qtype:
        total = count("SELECT COUNT(*) FROM questions WHERE type=?", (qtype,))
        questions = query("SELECT * FROM questions WHERE type=? ORDER BY id LIMIT ? OFFSET ?",
                         (qtype, per_page, (page-1)*per_page))
    else:
        total = count("SELECT COUNT(*) FROM questions")
        questions = query("SELECT * FROM questions ORDER BY id LIMIT ? OFFSET ?",
                         (per_page, (page-1)*per_page))
    return render_template('teacher_questions.html', questions=questions, qtype=qtype,
                          page=page, total=total, per_page=per_page)

@app.route('/teacher/questions/add', methods=['POST'])
@teacher_required
def add_question():
    qtype = request.form['type']
    title = request.form['title']
    correct_answer = request.form['correct_answer']
    difficulty = int(request.form.get('difficulty', 1))
    kps = request.form.get('knowledge_points', '[]')
    options = request.form.get('options')
    aliases = request.form.get('aliases')
    test_cases = request.form.get('test_cases')
    code_template = request.form.get('code_template')
    reference_code = request.form.get('reference_code')
    insert("INSERT INTO questions (type,title,options,correct_answer,difficulty,knowledge_points,aliases,test_cases,code_template,reference_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (qtype, title, options, correct_answer, difficulty, kps, aliases, test_cases, code_template, reference_code))
    return redirect(url_for('teacher_questions'))

@app.route('/teacher/questions/<int:qid>/delete', methods=['POST'])
@teacher_required
def delete_question(qid):
    insert("DELETE FROM questions WHERE id=?", (qid,))
    return redirect(url_for('teacher_questions'))

@app.route('/teacher/exams')
@teacher_required
def teacher_exams():
    exams = query("SELECT * FROM exams WHERE teacher_id=? ORDER BY created_at DESC", (session['user_id'],))
    return render_template('teacher_exams.html', exams=exams)

@app.route('/teacher/exams/create', methods=['GET', 'POST'])
@teacher_required
def create_exam():
    if request.method == 'POST':
        title = request.form['title']
        description = request.form.get('description', '')
        duration = int(request.form.get('duration', 60))
        speed = float(request.form.get('speed_multiplier', 1.0))
        auto_gen = request.form.get('auto_gen')
        qids = []
        if auto_gen:
            sc = int(request.form.get('sc_count', 1))
            mc = int(request.form.get('mc_count', 1))
            tf = int(request.form.get('tf_count', 1))
            fb = int(request.form.get('fb_count', 1))
            pr = int(request.form.get('pr_count', 1))
            for qtype, cnt in [('single_choice', sc), ('multi_choice', mc), ('true_false', tf),
                              ('fill_blank', fb), ('programming', pr)]:
                pool = [r['id'] for r in query("SELECT id FROM questions WHERE type=? ORDER BY RANDOM()", (qtype,))]
                selected = random.sample(pool, min(cnt, len(pool)))
                qids.extend(selected)
        else:
            qids = request.form.get('question_ids', '')
            if qids:
                qids = [int(x) for x in qids.split(',') if x.strip()]
        exam_id = insert("INSERT INTO exams (title,description,teacher_id,duration_minutes,speed_multiplier) VALUES (?,?,?,?,?)",
                         (title, description, session['user_id'], duration, speed))
        for i, qid in enumerate(qids):
            q = query_one("SELECT * FROM questions WHERE id=?", (qid,))
            pts = q['difficulty'] * 2 if q else 5
            insert("INSERT INTO exam_questions (exam_id,question_id,points,order_num) VALUES (?,?,?,?)",
                   (exam_id, qid, pts, i + 1))
        return redirect(url_for('edit_exam', exam_id=exam_id))

    questions = query("SELECT * FROM questions ORDER BY type, difficulty")
    existing = query("SELECT title FROM exams WHERE teacher_id=?", (session['user_id'],))
    nums = set()
    for row in existing:
        t = row['title'].strip()
        if t.isdigit():
            nums.add(int(t))
    n = 1
    while n in nums:
        n += 1
    return render_template('create_exam.html', questions=questions, default_title=str(n))

@app.route('/teacher/exams/<int:exam_id>/edit', methods=['GET', 'POST'])
@teacher_required
def edit_exam(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if not exam:
        return redirect(url_for('teacher_exams'))
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_questions':
            qids = request.form.get('question_ids', '')
            if qids:
                qids = [int(x) for x in qids.split(',') if x.strip()]
                max_order = query_one("SELECT MAX(order_num) FROM exam_questions WHERE exam_id=?", (exam_id,))
                max_order = (max_order[0] or 0) if max_order else 0
                for i, qid in enumerate(qids):
                    q = query_one("SELECT * FROM questions WHERE id=?", (qid,))
                    pts = q['difficulty'] * 2 if q else 5
                    insert("INSERT INTO exam_questions (exam_id,question_id,points,order_num) VALUES (?,?,?,?)",
                           (exam_id, qid, pts, max_order + i + 1))
        elif action == 'remove_question':
            eq_id = request.form.get('eq_id')
            insert("DELETE FROM exam_questions WHERE id=? AND exam_id=?", (eq_id, exam_id))
        elif action == 'update_points':
            eq_id = request.form.get('eq_id')
            pts = float(request.form.get('points', 5))
            insert("UPDATE exam_questions SET points=? WHERE id=?", (pts, eq_id))
        elif action == 'publish':
            return redirect(url_for('publish_exam', exam_id=exam_id))
        return redirect(url_for('edit_exam', exam_id=exam_id))

    eqs = query("""
        SELECT eq.*, q.title, q.type, q.options, q.correct_answer, q.difficulty
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (exam_id,))
    all_qs = query("SELECT * FROM questions WHERE id NOT IN (SELECT question_id FROM exam_questions WHERE exam_id=?) ORDER BY type, difficulty", (exam_id,))
    return render_template('edit_exam.html', exam=exam, eqs=eqs, all_qs=all_qs)

@app.route('/teacher/exams/<int:exam_id>/publish')
@teacher_required
def publish_exam(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if not exam:
        return redirect(url_for('teacher_exams'))
    students = query("SELECT * FROM users WHERE role='student' ORDER BY CASE WHEN username LIKE 'stu_%' THEN 0 ELSE 1 END, id")
    return render_template('publish_exam.html', exam=exam, students=students)

@app.route('/teacher/exams/<int:exam_id>/publish', methods=['POST'])
@teacher_required
def do_publish_exam(exam_id):
    student_ids = request.form.getlist('student_ids')
    if not student_ids:
        return redirect(url_for('publish_exam', exam_id=exam_id))
    start_time = datetime.now()
    exam = query_one("SELECT * FROM exams WHERE id=?", (exam_id,))
    duration = timedelta(minutes=exam['duration_minutes'])
    end_time = start_time + duration
    insert("UPDATE exams SET status='in_progress', start_time=?, end_time=? WHERE id=?",
           (start_time.strftime('%Y-%m-%d %H:%M:%S'), end_time.strftime('%Y-%m-%d %H:%M:%S'), exam_id))
    for sid in student_ids:
        existing = query_one("SELECT id FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, int(sid)))
        if not existing:
            insert("INSERT INTO student_exams (exam_id, student_id, status) VALUES (?,?,?)",
                   (exam_id, int(sid), 'pending'))
    return redirect(url_for('teacher_exam_results', exam_id=exam_id))

@app.route('/teacher/exams/<int:exam_id>/end', methods=['POST'])
@teacher_required
def force_end_exam(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if exam and exam['status'] not in ('completed',):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        insert("UPDATE student_exams SET status='timeout', submit_time=? WHERE exam_id=? AND status IN ('in_progress','pending')",
               (now, exam_id))
        trigger_grading(exam_id)
    return redirect(url_for('teacher_exam_results', exam_id=exam_id))

@app.route('/teacher/exams/<int:exam_id>/delete', methods=['POST'])
@teacher_required
def delete_exam(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if exam:
        insert("DELETE FROM exams WHERE id=?", (exam_id,))
    return redirect(request.referrer or url_for('teacher_exams'))

@app.route('/teacher/exams/<int:exam_id>/results')
@teacher_required
def teacher_exam_results(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if not exam:
        return redirect(url_for('teacher_exams'))
    ses = query("""
        SELECT se.*, u.username, u.display_name
        FROM student_exams se JOIN users u ON se.student_id=u.id
        WHERE se.exam_id=? ORDER BY u.id
    """, (exam_id,))
    eqs = query("""
        SELECT eq.*, q.type, q.title
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (exam_id,))
    score_map = {}
    for se in ses:
        score_map[se['id']] = {}
        for ans in query("SELECT * FROM answers WHERE student_exam_id=? AND is_draft=0", (se['id'],)):
            score_map[se['id']][ans['question_id']] = ans
    return render_template('exam_results.html', exam=exam, ses=ses, eqs=eqs, score_map=score_map)

@app.route('/teacher/exams/<int:exam_id>/analysis')
@teacher_required
def exam_analysis(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if not exam:
        return redirect(url_for('teacher_exams'))
    return render_template('exam_analysis.html', exam=exam)

@app.route('/teacher/exams/<int:exam_id>/code_review')
@teacher_required
def code_review(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if not exam:
        return redirect(url_for('teacher_exams'))
    pq_ids = [r['question_id'] for r in query(
        "SELECT eq.question_id FROM exam_questions eq JOIN questions q ON eq.question_id=q.id WHERE eq.exam_id=? AND q.type='programming'", (exam_id,))]
    prog_qs = query("SELECT * FROM questions WHERE id IN ({})".format(','.join('?'*len(pq_ids)) if pq_ids else '0'),
                    pq_ids if pq_ids else [0])
    ses = query("""
        SELECT se.*, u.username, u.display_name
        FROM student_exams se JOIN users u ON se.student_id=u.id
        WHERE se.exam_id=? ORDER BY u.id
    """, (exam_id,))
    return render_template('code_review.html', exam=exam, prog_qs=prog_qs, ses=ses)

@app.route('/teacher/exams/<int:exam_id>/export')
@teacher_required
def export_excel(exam_id):
    import openpyxl
    from openpyxl.styles import Font, Alignment
    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if not exam:
        return redirect(url_for('teacher_exams'))
    ses = query("""
        SELECT se.*, u.username, u.display_name
        FROM student_exams se JOIN users u ON se.student_id=u.id
        WHERE se.exam_id=? ORDER BY u.id
    """, (exam_id,))
    eqs = query("""
        SELECT eq.*, q.type, q.title
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (exam_id,))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '成绩汇总'
    headers = ['学号', '姓名', '状态', '总分']
    for eq in eqs:
        headers.append(f"Q{eq['order_num']}({eq['points']}分)")
    ws.append(headers)

    for se in ses:
        row = [se['username'], se['display_name'] or '', se['status'], se['score'] or 0]
        for eq in eqs:
            ans = query_one("SELECT score FROM answers WHERE student_exam_id=? AND question_id=?",
                           (se['id'], eq['question_id']))
            row.append(ans['score'] if ans else 0)
        ws.append(row)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, download_name=f'{exam["title"]}_成绩.xlsx',
                     as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/student')
@student_required
def student_dashboard():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    pending = query("""
        SELECT se.*, e.title, e.duration_minutes, e.speed_multiplier, e.end_time, e.status as exam_status
        FROM student_exams se JOIN exams e ON se.exam_id=e.id
        WHERE se.student_id=? AND se.status IN ('pending','in_progress')
        ORDER BY e.created_at DESC
    """, (session['user_id'],))
    completed = query("""
        SELECT se.*, e.title, e.duration_minutes, e.status as exam_status
        FROM student_exams se JOIN exams e ON se.exam_id=e.id
        WHERE se.student_id=? AND se.status IN ('submitted','graded','timeout')
        ORDER BY se.submit_time DESC
    """, (session['user_id'],))
    return render_template('student_dashboard.html', pending=pending, completed=completed, now=now)

@app.route('/student/exam/<int:exam_id>/start', methods=['POST'])
@student_required
def start_exam(exam_id):
    se = query_one("SELECT * FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, session['user_id']))
    if not se:
        return redirect(url_for('student_dashboard'))
    if se['status'] == 'pending':
        insert("UPDATE student_exams SET status='in_progress', start_time=? WHERE id=?",
               (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), se['id']))
    return redirect(url_for('take_exam', exam_id=exam_id))

@app.route('/student/exam/<int:exam_id>')
@student_required
def take_exam(exam_id):
    se = query_one("SELECT * FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, session['user_id']))
    if not se:
        return redirect(url_for('student_dashboard'))
    if se['status'] not in ('in_progress', 'pending'):
        return redirect(url_for('view_result', student_exam_id=se['id']))

    exam = query_one("SELECT * FROM exams WHERE id=?", (exam_id,))
    eqs_raw = query("""
        SELECT eq.*, q.title as q_title, q.type, q.options, q.correct_answer, q.code_template, q.test_cases
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (exam_id,))

    eqs = []
    for eq in eqs_raw:
        eq_dict = dict(eq)
        if eq_dict.get('options'):
            try:
                eq_dict['options_parsed'] = json.loads(eq_dict['options'])
            except (json.JSONDecodeError, TypeError):
                eq_dict['options_parsed'] = []
        else:
            eq_dict['options_parsed'] = []
        eqs.append(eq_dict)

    drafts = {}
    for ans in query("SELECT * FROM answers WHERE student_exam_id=? AND is_draft=1", (se['id'],)):
        drafts[ans['question_id']] = ans['answer_text']

    now = datetime.now()
    if se['start_time']:
        start = datetime.strptime(se['start_time'], '%Y-%m-%d %H:%M:%S')
        elapsed = (now - start).total_seconds()
        total = exam['duration_minutes'] * 60
        remaining = max(0, int(total - elapsed * exam['speed_multiplier']))
    else:
        remaining = exam['duration_minutes'] * 60

    return render_template('take_exam.html', exam=exam, se=se, eqs=eqs, drafts=drafts,
                          remaining=remaining, speed=exam['speed_multiplier'])

@app.route('/student/exam/<int:exam_id>/save_draft', methods=['POST'])
@student_required
def save_draft(exam_id):
    se = query_one("SELECT * FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, session['user_id']))
    if not se or se['status'] != 'in_progress':
        return jsonify({'ok': False, 'error': '考试未在进行中'})
    data = request.get_json()
    qid = data['question_id']
    answer = data.get('answer', '')
    existing = query_one("SELECT id FROM answers WHERE student_exam_id=? AND question_id=?",
                        (se['id'], qid))
    if existing:
        insert("UPDATE answers SET answer_text=?, updated_at=? WHERE id=?",
               (answer, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), existing['id']))
    else:
        insert("INSERT INTO answers (student_exam_id, question_id, answer_text, is_draft) VALUES (?,?,?,1)",
               (se['id'], qid, answer))
    return jsonify({'ok': True})

@app.route('/student/exam/<int:exam_id>/submit', methods=['POST'])
@student_required
def submit_exam(exam_id):
    se = query_one("SELECT * FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, session['user_id']))
    if not se:
        return jsonify({'ok': False, 'error': 'Not found'})
    data = request.get_json() or {}
    answers_data = data.get('answers', {})
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for qid_str, answer in answers_data.items():
        qid = int(qid_str)
        existing = query_one("SELECT id FROM answers WHERE student_exam_id=? AND question_id=?",
                            (se['id'], qid))
        if existing:
            insert("UPDATE answers SET answer_text=?, is_draft=0, updated_at=? WHERE id=?",
                   (answer, now, existing['id']))
        else:
            insert("INSERT INTO answers (student_exam_id, question_id, answer_text, is_draft) VALUES (?,?,?,0)",
                   (se['id'], qid, answer))
    insert("UPDATE answers SET is_draft=0, updated_at=? WHERE student_exam_id=? AND is_draft=1", (now, se['id']))
    insert("UPDATE student_exams SET status='submitted', submit_time=? WHERE id=?", (now, se['id']))

    exam = query_one("SELECT * FROM exams WHERE id=?", (exam_id,))
    check_exam_completion(exam_id)
    return jsonify({'ok': True, 'redirect': url_for('view_result', student_exam_id=se['id'])})

@app.route('/student/exam/<int:exam_id>/timeout', methods=['POST'])
@student_required
def timeout_exam(exam_id):
    se = query_one("SELECT * FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, session['user_id']))
    if not se:
        return jsonify({'ok': False})
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    insert("UPDATE answers SET is_draft=0 WHERE student_exam_id=? AND is_draft=1", (se['id'],))
    insert("UPDATE student_exams SET status='timeout', submit_time=? WHERE id=?", (now, se['id']))
    check_exam_completion(exam_id)
    return jsonify({'ok': True})

@app.route('/student/result/<int:student_exam_id>')
@student_required
def view_result(student_exam_id):
    se = query_one("SELECT * FROM student_exams WHERE id=? AND student_id=?", (student_exam_id, session['user_id']))
    if not se:
        return redirect(url_for('student_dashboard'))
    exam = query_one("SELECT * FROM exams WHERE id=?", (se['exam_id'],))
    eqs = query("""
        SELECT eq.*, q.title, q.type, q.options, q.correct_answer, q.aliases, q.code_template, q.test_cases, q.reference_code
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (se['exam_id'],))
    total_points = sum(eq['points'] for eq in eqs)
    answers_map = {}
    for a in query("SELECT * FROM answers WHERE student_exam_id=?", (student_exam_id,)):
        answers_map[a['question_id']] = a
    grade = query_one("SELECT * FROM grades WHERE student_exam_id=?", (student_exam_id,))
    return render_template('view_result.html', exam=exam, se=se, eqs=eqs,
                          answers_map=answers_map, grade=grade, total_points=total_points)

@app.route('/student/wrong_book')
@student_required
def wrong_book():
    items = query("""
        SELECT wb.*, q.title, q.type, q.options, q.correct_answer
        FROM wrong_question_book wb JOIN questions q ON wb.question_id=q.id
        WHERE wb.student_id=? AND wb.mastered=0 ORDER BY wb.created_at DESC
    """, (session['user_id'],))
    return render_template('wrong_book.html', items=items)

@app.route('/student/wrong_book/<int:wb_id>/redo', methods=['POST'])
@student_required
def redo_wrong(wb_id):
    wb = query_one("SELECT * FROM wrong_question_book WHERE id=? AND student_id=?", (wb_id, session['user_id']))
    if not wb:
        return jsonify({'ok': False})
    answer = (request.get_json(silent=True) or {}).get('answer', '')
    q = query_one("SELECT * FROM questions WHERE id=?", (wb['question_id'],))
    correct = check_answer(q, answer)
    if correct:
        insert("UPDATE wrong_question_book SET mastered=1 WHERE id=?", (wb_id,))
    return jsonify({'ok': True, 'correct': correct, 'correct_answer': q['correct_answer']})

@app.route('/student/wrong_book/batch_delete', methods=['POST'])
@student_required
def batch_delete_wrong():
    data = request.get_json() or {}
    ids = data.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({'ok': False, 'error': '无效的ID列表'})
    deleted = 0
    for wb_id in ids:
        wb = query_one("SELECT id FROM wrong_question_book WHERE id=? AND student_id=?", (int(wb_id), session['user_id']))
        if wb:
            insert("DELETE FROM wrong_question_book WHERE id=?", (wb_id,))
            deleted += 1
    return jsonify({'ok': True, 'deleted': deleted})

@app.route('/student/knowledge_graph')
@student_required
def knowledge_graph():
    student_id = session['user_id']
    return render_template('knowledge_graph.html', student_id=student_id)

@app.route('/student/programming/run', methods=['POST'])
@student_required
def run_code():
    data = request.get_json(silent=True) or {}
    code = data.get('code', '')
    question_id = data.get('question_id')
    test_input = data.get('input', '')

    if question_id:
        q = query_one("SELECT * FROM questions WHERE id=?", (question_id,))
        if q and q['test_cases']:
            test_cases = json.loads(q['test_cases'])
            scan_ok, scan_msg = scan_code(code)
            if not scan_ok:
                return jsonify({'ok': False, 'message': f'Security check failed: {scan_msg}'})
            results = []
            for tc in test_cases:
                full_code = code + '\nprint("__TEST_CALL__")\n' + tc.get('call', '')
                ok, stdout, stderr = run_code_sandbox(full_code, tc.get('input', ''), timeout=5, skip_scan=True)
                expected = tc.get('expected', '').strip()
                actual = stdout if ok else ''
                if ok and '__TEST_CALL__' in actual:
                    actual = actual.rsplit('__TEST_CALL__', 1)[-1].strip()
                passed = False
                if ok:
                    exp_val, exp_parsed = _parse_output(expected)
                    act_val, act_parsed = _parse_output(actual)
                    if exp_parsed and act_parsed:
                        passed = _deep_equal(exp_val, act_val)
                    else:
                        passed = actual == expected
                results.append({
                    'call': tc.get('call', ''),
                    'expected': expected,
                    'output': actual,
                    'error': stderr.strip() if not ok else '',
                    'passed': passed
                })
            return jsonify({'ok': True, 'results': results})

    ok, stdout, stderr = run_code_sandbox(code, test_input, timeout=5)
    return jsonify({'ok': ok, 'message': stderr or stdout, 'output': stdout})

@app.route('/api/exam_status/<int:student_exam_id>')
@login_required
def api_exam_status(student_exam_id):
    se = query_one("SELECT * FROM student_exams WHERE id=?", (student_exam_id,))
    if not se:
        return jsonify({'status': 'error'})
    exam = query_one("SELECT * FROM exams WHERE id=?", (se['exam_id'],))
    progress = 0
    total = 0
    if exam and exam['status'] in ('grading', 'completed'):
        total_prog = count("""
            SELECT COUNT(*) FROM student_exams se
            JOIN exam_questions eq ON eq.exam_id=se.exam_id
            JOIN questions q ON eq.question_id=q.id
            WHERE se.exam_id=? AND q.type='programming'
        """, (se['exam_id'],))
        graded_prog = count("""
            SELECT COUNT(*) FROM answers a
            JOIN questions q ON a.question_id=q.id
            WHERE a.student_exam_id IN (SELECT id FROM student_exams WHERE exam_id=?)
            AND q.type='programming' AND a.is_draft=0 AND a.score != 0
        """, (se['exam_id'],))
        progress = graded_prog
        total = total_prog
    return jsonify({
        'student_status': se['status'],
        'exam_status': exam['status'] if exam else 'unknown',
        'grading_progress': progress,
        'grading_total': total,
        'score': se['score']
    })

@app.route('/api/knowledge_graph/<int:student_id>')
@login_required
def api_knowledge_graph(student_id):
    kps = query("SELECT * FROM knowledge_points ORDER BY id")
    edges = query("SELECT * FROM knowledge_edges")
    all_qs = query("SELECT id, knowledge_points FROM questions")
    qs_kps = []
    for r in all_qs:
        try:
            qs_kps.append((r['id'], json.loads(r['knowledge_points'] or '[]')))
        except (json.JSONDecodeError, TypeError):
            qs_kps.append((r['id'], []))
    stats = {}
    for kp in kps:
        qids = [qid for qid, kps_list in qs_kps if kp['id'] in kps_list]
        if not qids:
            stats[kp['id']] = {'correct': 0, 'total': 0, 'rate': 0}
            continue
        total = 0
        correct = 0
        for qid in qids:
            answers = query("""
                SELECT a.is_correct FROM answers a
                JOIN student_exams se ON a.student_exam_id=se.id
                WHERE se.student_id=? AND a.question_id=? AND a.is_draft=0
            """, (student_id, qid))
            for a in answers:
                total += 1
                if a['is_correct']:
                    correct += 1
        rate = (correct / total * 100) if total > 0 else 0
        stats[kp['id']] = {'correct': correct, 'total': total, 'rate': round(rate, 1)}

    nodes = []
    for kp in kps:
        s = stats.get(kp['id'], {'rate': 0})
        color = '#4CAF50' if s['rate'] >= 80 else ('#FF9800' if s['rate'] >= 40 else '#F44336')
        nodes.append({
            'id': kp['id'], 'name': kp['name'], 'description': kp['description'],
            'category': kp['category'], 'rate': s['rate'],
            'itemStyle': {'color': color}
        })
    links = [{'source': e['from_kp_id'], 'target': e['to_kp_id']} for e in edges]
    return jsonify({'nodes': nodes, 'links': links})

@app.route('/api/knowledge_graph/<int:student_id>/questions/<int:kp_id>')
@login_required
def api_kp_questions(student_id, kp_id):
    all_qs = query("SELECT * FROM questions")
    qs = []
    for q in all_qs:
        try:
            if kp_id in json.loads(q['knowledge_points'] or '[]'):
                qs.append(q)
        except (json.JSONDecodeError, TypeError):
            pass
    result = []
    for q in qs:
        answers = query("""
            SELECT a.* FROM answers a
            JOIN student_exams se ON a.student_exam_id=se.id
            WHERE se.student_id=? AND a.question_id=? AND a.is_draft=0
            ORDER BY a.updated_at DESC LIMIT 1
        """, (student_id, q['id']))
        result.append({
            'id': q['id'], 'title': q['title'], 'type': q['type'],
            'difficulty': q['difficulty'],
            'last_correct': bool(answers[0]['is_correct']) if answers else None
        })
    return jsonify(result)

@app.route('/api/practice/question/<int:question_id>')
@login_required
def api_practice_question(question_id):
    q = query_one("SELECT * FROM questions WHERE id=?", (question_id,))
    if not q:
        return jsonify({'ok': False, 'msg': '题目不存在'}), 404
    try:
        opts = json.loads(q['options']) if q['options'] else []
    except Exception:
        opts = []
    return jsonify({
        'ok': True,
        'id': q['id'],
        'title': q['title'],
        'type': q['type'],
        'options': opts,
        'difficulty': q['difficulty'],
        'code_template': q['code_template'] or ''
    })

@app.route('/api/practice/check/<int:question_id>', methods=['POST'])
@login_required
def api_practice_check(question_id):
    q = query_one("SELECT * FROM questions WHERE id=?", (question_id,))
    if not q:
        return jsonify({'ok': False, 'msg': '题目不存在'}), 404
    answer = (request.get_json(silent=True) or {}).get('answer', '')
    try:
        is_correct, _ = grade_objective({
            'type': q['type'],
            'correct_answer': q['correct_answer'],
            'points': 1,
            'aliases': q['aliases']
        }, answer)
    except Exception as e:
        return jsonify({'ok': False, 'msg': f'判题错误: {str(e)}'}), 500
    return jsonify({
        'ok': True,
        'is_correct': is_correct,
        'correct_answer': q['correct_answer']
    })

@app.route('/api/wrong_book/add', methods=['POST'])
@login_required
def api_wrong_book_add():
    question_id = request.json.get('question_id') if request.is_json else request.form.get('question_id')
    student_answer = request.json.get('student_answer', '') if request.is_json else request.form.get('student_answer', '')
    is_correct = 1 if (request.json.get('is_correct') if request.is_json else request.form.get('is_correct')) else 0

    existing = query_one(
        "SELECT id FROM wrong_question_book WHERE student_id=? AND question_id=? AND exam_id IS NULL",
        (session['user_id'], question_id)
    )
    if existing:
        return jsonify({'ok': True, 'msg': '已在错题本中'})

    insert(
        "INSERT INTO wrong_question_book (student_id, question_id, exam_id, student_answer, is_correct) VALUES (?,?,NULL,?,?)",
        (session['user_id'], question_id, student_answer, is_correct)
    )
    return jsonify({'ok': True, 'msg': '已加入错题本'})

@app.route('/api/chart/score_distribution/<int:exam_id>')
@login_required
def api_score_distribution(exam_id):
    cache_key = f'score_dist_{exam_id}'
    with chart_cache_lock:
        if cache_key in chart_cache:
            return jsonify({'data': chart_cache[cache_key]})

    scores = [r[0] or 0 for r in query("SELECT score FROM student_exams WHERE exam_id=? AND score IS NOT NULL", (exam_id,))]
    if not scores:
        return jsonify({'data': None})

    bin_edges = list(range(0, 101, 10))
    bins = []
    for i in range(len(bin_edges) - 1):
        low, high = bin_edges[i], bin_edges[i + 1]
        if i == len(bin_edges) - 2:
            count = sum(1 for s in scores if low <= s <= high)
        else:
            count = sum(1 for s in scores if low <= s < high)
        label = f'{low}-{high}' if i < len(bin_edges) - 2 else f'{low}-{high}'
        bins.append({'label': label, 'count': count})

    avg = round(sum(scores) / len(scores), 1)
    data = {'bins': bins, 'average': avg}

    with chart_cache_lock:
        chart_cache[cache_key] = data
    return jsonify({'data': data})

@app.route('/api/chart/question_accuracy/<int:exam_id>')
@login_required
def api_question_accuracy(exam_id):
    cache_key = f'q_acc_{exam_id}'
    with chart_cache_lock:
        if cache_key in chart_cache:
            return jsonify({'data': chart_cache[cache_key]})

    eqs = query("""
        SELECT eq.*, q.title, q.type
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (exam_id,))
    data = []
    for eq in eqs:
        total = count("SELECT COUNT(*) FROM answers WHERE question_id=? AND is_draft=0 AND student_exam_id IN (SELECT id FROM student_exams WHERE exam_id=?)",
                     (eq['question_id'], exam_id))
        correct = count("SELECT COUNT(*) FROM answers WHERE question_id=? AND is_correct=1 AND is_draft=0 AND student_exam_id IN (SELECT id FROM student_exams WHERE exam_id=?)",
                       (eq['question_id'], exam_id))
        rate = (correct / total * 100) if total > 0 else 0
        data.append({'order': eq['order_num'], 'title': eq['title'][:30], 'type': eq['type'],
                     'total': total, 'correct': correct, 'rate': round(rate, 1)})
    with chart_cache_lock:
        chart_cache[cache_key] = data
    return jsonify({'data': data})

@app.route('/api/chart/code_radar/<int:student_exam_id>')
@login_required
def api_code_radar(student_exam_id):
    se = query_one("SELECT * FROM student_exams WHERE id=?", (student_exam_id,))
    if not se:
        return jsonify({})
    answers = query("""
        SELECT a.*, q.reference_code FROM answers a
        JOIN questions q ON a.question_id=q.id
        WHERE a.student_exam_id=? AND q.type='programming' AND a.is_draft=0
    """, (student_exam_id,))
    if not answers:
        return jsonify({})
    scores = []
    for ans in answers:
        scores.append(analyze_code_quality(ans['answer_text'] or ''))
    avg = {}
    keys = ['naming', 'structure', 'comments', 'complexity', 'conciseness', 'type_hints']
    for k in keys:
        avg[k] = round(sum(s[k] for s in scores) / len(scores), 1)
    return jsonify(avg)

@app.route('/api/chart/code_heatmap/<int:exam_id>')
@login_required
def api_code_heatmap(exam_id):
    ses = query("""
        SELECT se.id, u.display_name, u.username
        FROM student_exams se JOIN users u ON se.student_id=u.id
        WHERE se.exam_id=? ORDER BY u.id
    """, (exam_id,))
    pq_ids = [r[0] for r in query("SELECT eq.question_id FROM exam_questions eq JOIN questions q ON eq.question_id=q.id WHERE eq.exam_id=? AND q.type='programming' ORDER BY eq.order_num", (exam_id,))]
    if not pq_ids:
        return jsonify({'students': [], 'dimensions': [], 'data': []})

    dimensions = ['命名质量', '代码结构', '注释覆盖', '圈复杂度', '代码简洁', '类型注解']
    keys = ['naming', 'structure', 'comments', 'complexity', 'conciseness', 'type_hints']
    data = []
    students = []
    for se in ses:
        q_scores = []
        for pq_id in pq_ids:
            ans = query_one("SELECT answer_text FROM answers WHERE student_exam_id=? AND question_id=? AND is_draft=0",
                           (se['id'], pq_id))
            if ans and ans['answer_text']:
                q_scores.append(analyze_code_quality(ans['answer_text']))
        if q_scores:
            row = [round(sum(s[k] for s in q_scores) / len(q_scores)) for k in keys]
            data.append(row)
            students.append(se['display_name'] or se['username'])
    return jsonify({'students': students, 'dimensions': dimensions, 'data': data})

@app.route('/api/chart/similarity_matrix/<int:exam_id>')
@login_required
def api_similarity_matrix(exam_id):
    cache_key = f'sim_matrix_{exam_id}'
    with chart_cache_lock:
        if cache_key in chart_cache:
            return jsonify(chart_cache[cache_key])

    pq_ids = [r[0] for r in query("SELECT eq.question_id FROM exam_questions eq JOIN questions q ON eq.question_id=q.id WHERE eq.exam_id=? AND q.type='programming' ORDER BY eq.order_num", (exam_id,))]
    if not pq_ids:
        return jsonify({'students': [], 'matrix': []})

    ses = query("""
        SELECT se.id, u.username, u.display_name
        FROM student_exams se JOIN users u ON se.student_id=u.id
        WHERE se.exam_id=? ORDER BY u.id
    """, (exam_id,))
    labels = []
    ids_list = []
    codes_per_question = []
    for se in ses:
        parts = []
        for pq_id in pq_ids:
            ans = query_one("SELECT answer_text FROM answers WHERE student_exam_id=? AND question_id=? AND is_draft=0",
                           (se['id'], pq_id))
            parts.append(ans['answer_text'] if ans else '')
        codes_per_question.append(parts)
        labels.append(se['display_name'] or se['username'])
        ids_list.append(se['id'])

    matrix = build_similarity_matrix(list(zip(range(len(codes_per_question)), codes_per_question)))
    result = {'students': labels, 'ids': ids_list, 'matrix': matrix}
    with chart_cache_lock:
        chart_cache[cache_key] = result
    return jsonify(result)

@app.route('/api/diff/<int:se_id1>/<int:se_id2>')
@login_required
def api_diff(se_id1, se_id2):
    se1 = query_one("SELECT * FROM student_exams WHERE id=?", (se_id1,))
    se2 = query_one("SELECT * FROM student_exams WHERE id=?", (se_id2,))
    if not se1 or not se2:
        return jsonify({'diff': []})
    pq_ids = [r[0] for r in query("SELECT eq.question_id FROM exam_questions eq JOIN questions q ON eq.question_id=q.id WHERE eq.exam_id=? AND q.type='programming' ORDER BY eq.order_num", (se1['exam_id'],))]
    if not pq_ids:
        return jsonify({'diff': []})
    parts1 = []
    parts2 = []
    for pq_id in pq_ids:
        a1 = query_one("SELECT answer_text FROM answers WHERE student_exam_id=? AND question_id=?", (se_id1, pq_id))
        a2 = query_one("SELECT answer_text FROM answers WHERE student_exam_id=? AND question_id=?", (se_id2, pq_id))
        if a1 and a1['answer_text']:
            parts1.append(a1['answer_text'])
        if a2 and a2['answer_text']:
            parts2.append(a2['answer_text'])
    code1 = '\n'.join(parts1)
    code2 = '\n'.join(parts2)
    diff = generate_diff(code1, code2)
    return jsonify({'diff': diff, 'student1': se1['id'], 'student2': se2['id']})

@app.route('/api/ai_detection/<int:exam_id>')
@login_required
def api_ai_detection(exam_id):
    pq_ids = [r[0] for r in query("SELECT eq.question_id FROM exam_questions eq JOIN questions q ON eq.question_id=q.id WHERE eq.exam_id=? AND q.type='programming'", (exam_id,))]
    ses = query("""
        SELECT se.id, u.username, u.display_name
        FROM student_exams se JOIN users u ON se.student_id=u.id
        WHERE se.exam_id=? ORDER BY u.id
    """, (exam_id,))
    reason_labels = {
        'comment_ratio': '注释比例偏高',
        'avg_var_len': '变量名偏长',
        'avg_func_lines': '函数体过短',
        'advanced_syntax': '使用高级语法',
        'empty_ratio': '空行比例偏高',
    }
    def _severity(k, v):
        n = float(v)
        if k in ('comment_ratio', 'empty_ratio'):
            return min(n, 1.0)
        elif k == 'avg_var_len':
            return min(n / 15.0, 1.0)
        elif k == 'avg_func_lines':
            return max(0, 1.0 - n / 10.0)
        elif k == 'advanced_syntax':
            return min(n / 3.0, 1.0)
        return 0.5

    results = []
    for se in ses:
        any_question_ai = False
        total_flags = 0
        all_reasons = []
        for pq_id in pq_ids:
            ans = query_one("SELECT answer_text FROM answers WHERE student_exam_id=? AND question_id=? AND is_draft=0",
                           (se['id'], pq_id))
            code = ans['answer_text'] if ans else ''
            is_ai, flags, reasons = detect_ai_code(code)
            total_flags += flags
            if is_ai:
                any_question_ai = True
            for k, v in reasons.items():
                label = reason_labels.get(k, k)
                nv = float(v)
                sv = _severity(k, v)
                all_reasons.append({
                    'label': label,
                    'value': round(nv, 2),
                    'severity': round(sv, 2),
                })
        max_flags = len(pq_ids) * 5
        significant_reasons = [r for r in all_reasons if r['severity'] >= 0.3]
        results.append({
            'student': se['display_name'] or se['username'],
            'is_ai': any_question_ai,
            'flags': f'{total_flags}/{max_flags}',
            'reasons': significant_reasons,
        })
    return jsonify(results)

@app.route('/api/exam/<int:exam_id>/student/<int:student_id>/answers')
@login_required
def api_student_answers(exam_id, student_id):
    se = query_one("SELECT id FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, student_id))
    if not se:
        return jsonify({'answers': []})
    answers = query("""
        SELECT a.*, q.title, q.type, q.correct_answer, q.options
        FROM answers a JOIN questions q ON a.question_id=q.id
        WHERE a.student_exam_id=? AND a.is_draft=0
    """, (se['id'],))
    result = []
    for a in answers:
        result.append({
            'question_title': a['title'],
            'type': a['type'],
            'answer_text': a['answer_text'],
            'is_correct': bool(a['is_correct']),
            'score': a['score'],
            'correct_answer': a['correct_answer']
        })
    return jsonify({'answers': result})

@app.route('/api/student/<int:student_exam_id>/code_quality')
@login_required
def api_student_code_quality(student_exam_id):
    se = query_one("SELECT * FROM student_exams WHERE id=?", (student_exam_id,))
    if not se:
        return jsonify({})
    results = []
    answers = query("""
        SELECT a.*, q.title, q.code_template
        FROM answers a JOIN questions q ON a.question_id=q.id
        WHERE a.student_exam_id=? AND q.type='programming' AND a.is_draft=0
    """, (student_exam_id,))
    for ans in answers:
        quality = analyze_code_quality(ans['answer_text'] or '')
        is_ai, flags, ai_reasons = detect_ai_code(ans['answer_text'] or '')
        results.append({
            'question_title': ans['title'],
            'code': ans['answer_text'] or '',
            'quality': quality,
            'is_ai': is_ai,
            'ai_flags': flags,
            'ai_reasons': ai_reasons
        })
    return jsonify({'results': results})

@app.route('/api/exam/<int:exam_id>/code_review_data')
@login_required
def api_code_review_data(exam_id):
    pq_ids = [r[0] for r in query("SELECT eq.question_id FROM exam_questions eq JOIN questions q ON eq.question_id=q.id WHERE eq.exam_id=? AND q.type='programming'", (exam_id,))]
    ses = query("""
        SELECT se.id, se.student_id, u.username, u.display_name
        FROM student_exams se JOIN users u ON se.student_id=u.id
        WHERE se.exam_id=? ORDER BY u.id
    """, (exam_id,))
    questions_data = []
    for pq_id in pq_ids:
        q = query_one("SELECT title FROM questions WHERE id=?", (pq_id,))
        submissions = []
        all_scores = []
        all_qualities = []
        for se in ses:
            ans = query_one("SELECT * FROM answers WHERE student_exam_id=? AND question_id=? AND is_draft=0",
                           (se['id'], pq_id))
            if ans and ans['answer_text']:
                quality = analyze_code_quality(ans['answer_text'])
                is_ai, flags, _ = detect_ai_code(ans['answer_text'])
                submissions.append({
                    'student': se['display_name'] or se['username'],
                    'student_exam_id': se['id'],
                    'student_id': se['student_id'],
                    'code': ans['answer_text'],
                    'score': ans['score'],
                    'quality': quality,
                    'is_ai': is_ai,
                    'ai_flags': flags
                })
                all_scores.append(ans['score'] or 0)
                all_qualities.append(quality)
            else:
                submissions.append({
                    'student': se['display_name'] or se['username'],
                    'student_exam_id': se['id'],
                    'student_id': se['student_id'],
                    'code': '',
                    'score': None,
                    'quality': {'naming': 0, 'structure': 0, 'comments': 0, 'complexity': 0, 'conciseness': 0, 'type_hints': 0},
                    'is_ai': False,
                    'ai_flags': 0
                })
        avg_quality = {}
        if all_qualities:
            for k in ['naming', 'structure', 'comments', 'complexity', 'conciseness', 'type_hints']:
                avg_quality[k] = round(sum(qq[k] for qq in all_qualities) / len(all_qualities), 1)
        score_dist = {}
        for s in all_scores:
            bucket = int(s // 10) * 10
            score_dist[f'{bucket}-{bucket+9}'] = score_dist.get(f'{bucket}-{bucket+9}', 0) + 1
        best_sub = None
        if submissions:
            best_sub = max(submissions, key=lambda x: x['score'])
        questions_data.append({
            'question_title': q['title'] if q else '',
            'submissions': submissions,
            'avg_quality': avg_quality,
            'score_distribution': score_dist,
            'best_code': best_sub['code'] if best_sub else '',
            'best_student': best_sub['student'] if best_sub else ''
        })
    return jsonify({'questions': questions_data})

def trigger_grading(exam_id):
    exam = query_one("SELECT * FROM exams WHERE id=?", (exam_id,))
    if exam['status'] in ('completed',):
        return

    submitted = count("SELECT COUNT(*) FROM student_exams WHERE exam_id=? AND status IN ('submitted','timeout')", (exam_id,))
    pending = count("SELECT COUNT(*) FROM student_exams WHERE exam_id=? AND status IN ('pending','in_progress')", (exam_id,))
    if submitted == 0:
        return

    insert("UPDATE exams SET status='grading' WHERE id=? AND status!='grading'", (exam_id,))

    eqs = query("""
        SELECT eq.*, q.type, q.correct_answer, q.aliases, q.test_cases
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (exam_id,))

    ses = query("SELECT * FROM student_exams WHERE exam_id=? AND status IN ('submitted','timeout')", (exam_id,))

    for se in ses:
        if se['status'] == 'graded':
            continue
        grade_student_exam(se, eqs, exam_id)

    futures = []
    for se in ses:
        for eq in eqs:
            if eq['type'] == 'programming':
                presets = query_one("SELECT preset_correct FROM answers WHERE student_exam_id=? AND question_id=?",
                                   (se['id'], eq['question_id']))
                if presets and presets[0] is not None:
                    if presets[0]:
                        insert("UPDATE answers SET is_correct=1, score=? WHERE student_exam_id=? AND question_id=?",
                               (eq['points'], se['id'], eq['question_id']))
                    else:
                        insert("UPDATE answers SET is_correct=0, score=-0.1 WHERE student_exam_id=? AND question_id=?",
                               (se['id'], eq['question_id']))
                else:
                    futures.append(executor.submit(grade_programming_answer, se['id'], eq))

    executor.submit(_finalize_grading, exam_id, [se['id'] for se in ses], futures)

def grade_student_exam(se, eqs, exam_id):
    total = 0
    for eq in eqs:
        if eq['type'] == 'programming':
            continue
        ans = query_one("SELECT * FROM answers WHERE student_exam_id=? AND question_id=? AND is_draft=0",
                       (se['id'], eq['question_id']))
        if not ans:
            continue
        is_correct, score = grade_objective(eq, ans['answer_text'] or '')
        insert("UPDATE answers SET is_correct=?, score=? WHERE id=?", (1 if is_correct else 0, score, ans['id']))
        total += score
        student = query_one("SELECT username FROM users WHERE id=?", (se['student_id'],))
        if student and not student['username'].startswith('stu_'):
            if not is_correct or (eq['type'] == 'multi_choice' and score < eq['points']):
                existing = query_one("SELECT id FROM wrong_question_book WHERE student_id=? AND question_id=? AND exam_id=?",
                                    (se['student_id'], eq['question_id'], exam_id))
                if not existing:
                    insert("INSERT INTO wrong_question_book (student_id, question_id, exam_id, student_answer, is_correct) VALUES (?,?,?,?,?)",
                           (se['student_id'], eq['question_id'], exam_id, ans['answer_text'] or '', 1 if is_correct else 0))

    insert("UPDATE student_exams SET score=? WHERE id=?", (total, se['id']))

def _finalize_grading(exam_id, se_ids, futures):
    for f in futures:
        try:
            f.result(timeout=30)
        except Exception:
            pass
    time.sleep(2)
    for se_id in se_ids:
        insert("UPDATE answers SET score=0 WHERE student_exam_id=? AND score=-0.1", (se_id,))
    _complete_grading(exam_id)

def _complete_grading(exam_id):
    ses = query("SELECT * FROM student_exams WHERE exam_id=?", (exam_id,))
    for se in ses:
        total = query_one("SELECT COALESCE(SUM(score), 0) FROM answers WHERE student_exam_id=? AND is_draft=0", (se['id'],))
        insert("UPDATE student_exams SET score=?, status='graded' WHERE id=? AND status!='graded'",
               (total[0] if total else 0, se['id']))
        existing = query_one("SELECT id FROM grades WHERE student_exam_id=?", (se['id'],))
        obj_score = query_one("""
            SELECT COALESCE(SUM(a.score), 0) FROM answers a
            JOIN questions q ON a.question_id=q.id
            WHERE a.student_exam_id=? AND a.is_draft=0 AND q.type!='programming'
        """, (se['id'],))
        prog_score = query_one("""
            SELECT COALESCE(SUM(a.score), 0) FROM answers a
            JOIN questions q ON a.question_id=q.id
            WHERE a.student_exam_id=? AND a.is_draft=0 AND q.type='programming'
        """, (se['id'],))
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if existing:
            insert("UPDATE grades SET total_score=?, objective_score=?, programming_score=?, graded_at=? WHERE student_exam_id=?",
                   (total[0] if total else 0, obj_score[0] if obj_score else 0, prog_score[0] if prog_score else 0, now, se['id']))
        else:
            insert("INSERT INTO grades (student_exam_id, total_score, objective_score, programming_score, graded_at) VALUES (?,?,?,?,?)",
                   (se['id'], total[0] if total else 0, obj_score[0] if obj_score else 0, prog_score[0] if prog_score else 0, now))
    insert("UPDATE exams SET status='completed' WHERE id=? AND status!='completed'", (exam_id,))
    with chart_cache_lock:
        keys_to_del = [k for k in chart_cache if f'_{exam_id}' in k or k.endswith(str(exam_id))]
        for k in keys_to_del:
            del chart_cache[k]

def _parse_output(val):
    """Parse a string as a Python literal."""
    import ast as _ast
    try:
        return _ast.literal_eval(val), True
    except (ValueError, SyntaxError):
        return val, False

def _deep_equal(a, b):
    """Recursively compare two Python values for equality."""
    if type(a) != type(b):
        return False
    if isinstance(a, dict):
        if len(a) != len(b):
            return False
        for k in a:
            if k not in b:
                return False
            if not _deep_equal(a[k], b[k]):
                return False
        return True
    elif isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if not _deep_equal(x, y):
                return False
        return True
    elif isinstance(a, float):
        return abs(a - b) < 0.001
    else:
        return a == b

def grade_programming_answer(se_id, eq):
    ans = query_one("SELECT * FROM answers WHERE student_exam_id=? AND question_id=? AND is_draft=0",
                   (se_id, eq['question_id']))
    if not ans or not ans['answer_text']:
        insert("UPDATE answers SET is_correct=0, score=0 WHERE id=? AND id NOT IN (SELECT id FROM answers WHERE score > 0 AND student_exam_id=? AND question_id=?)",
               (ans['id'] if ans else 0, se_id, eq['question_id']))
        return

    code = ans['answer_text']
    test_cases = json.loads(eq['test_cases'] or '[]')
    scan_ok, scan_msg = scan_code(code)
    if not scan_ok:
        insert("UPDATE answers SET is_correct=0, score=0 WHERE id=?", (ans['id'],))
        return
    passed = 0
    for tc in test_cases:
        call_code = tc.get('call', '')
        expected = tc.get('expected', '').strip()
        full_code = code + '\nprint("__TEST_CALL__")\n' + call_code
        ok, stdout, stderr = run_code_sandbox(full_code, tc.get('input', ''), timeout=5, skip_scan=True)
        if not ok:
            continue
        actual = stdout
        if '__TEST_CALL__' in actual:
            actual = actual.rsplit('__TEST_CALL__', 1)[-1].strip()
        else:
            actual = actual.strip()
        exp_val, exp_parsed = _parse_output(expected)
        act_val, act_parsed = _parse_output(actual)
        if exp_parsed and act_parsed:
            if _deep_equal(exp_val, act_val):
                passed += 1
        elif actual == expected:
            passed += 1
    score = (passed / len(test_cases)) * eq['points'] if test_cases else 0
    insert("UPDATE answers SET is_correct=?, score=? WHERE id=?", (1 if passed == len(test_cases) else 0, score, ans['id']))

def grade_objective(eq, answer):
    qtype = eq['type']
    correct = eq['correct_answer']
    points = eq['points'] or 0
    if qtype == 'single_choice':
        return (answer.strip().upper() == correct.strip().upper(), points if answer.strip().upper() == correct.strip().upper() else 0)
    elif qtype == 'multi_choice':
        if not answer.strip():
            return (False, 0)
        student_set = set(a.strip().upper() for a in answer.split(',') if a.strip())
        correct_set = set(a.strip().upper() for a in correct.split(',') if a.strip())
        if student_set == correct_set:
            return (True, points)
        elif student_set.issubset(correct_set) and len(student_set) > 0:
            return (False, points / 2)
        else:
            return (False, 0)
    elif qtype == 'true_false':
        return (answer.strip().upper() == correct.strip().upper(), points if answer.strip().upper() == correct.strip().upper() else 0)
    elif qtype == 'fill_blank':
        ok = fuzzy_match(answer, eq)
        return ok, points if ok else 0
    return (False, 0)

def fuzzy_match(student_answer, eq):
    if not student_answer or not student_answer.strip():
        return False
    import difflib
    sa = re.sub(r'[^\w]', '', student_answer.strip().lower())
    correct = eq['correct_answer'].strip().lower()
    ca = re.sub(r'[^\w]', '', correct)
    if sa == ca:
        return True
    if sa in ca or ca in sa:
        return True
    aliases = json.loads(eq['aliases'] or '[]')
    for alias in aliases:
        aa = re.sub(r'[^\w]', '', alias.strip().lower())
        if sa == aa or sa in aa or aa in sa:
            return True
    if len(sa) > 0 and len(ca) > 0:
        ratio = difflib.SequenceMatcher(None, sa, ca).ratio()
        if ratio >= 0.8:
            return True
    return False

def check_answer(q, answer):
    if q['type'] == 'single_choice':
        return answer.strip().upper() == q['correct_answer'].strip().upper()
    elif q['type'] == 'multi_choice':
        student_set = set(a.strip().upper() for a in answer.split(',') if a.strip())
        correct_set = set(a.strip().upper() for a in q['correct_answer'].split(',') if a.strip())
        return student_set == correct_set
    elif q['type'] == 'true_false':
        return answer.strip().upper() == q['correct_answer'].strip().upper()
    elif q['type'] == 'fill_blank':
        return fuzzy_match(answer, q)
    return False

def check_exam_completion(exam_id):
    total = count("SELECT COUNT(*) FROM student_exams WHERE exam_id=?", (exam_id,))
    submitted = count("SELECT COUNT(*) FROM student_exams WHERE exam_id=? AND status IN ('submitted','timeout')", (exam_id,))
    if total > 0 and submitted >= total:
        trigger_grading(exam_id)

@app.route('/api/tab_switch', methods=['POST'])
@login_required
def log_tab_switch():
    data = request.get_json()
    student_exam_id = data.get('student_exam_id')
    if student_exam_id:
        insert("INSERT INTO tab_switch_log (student_exam_id) VALUES (?)", (student_exam_id,))
        insert("UPDATE student_exams SET tab_switches = tab_switches + 1 WHERE id=?", (student_exam_id,))
    return jsonify({'ok': True})

@app.route('/teacher/demo')
@teacher_required
def demo_page():
    demo_exam = query_one("SELECT * FROM exams WHERE title=? AND teacher_id=? ORDER BY id DESC LIMIT 1",
                          (DEMO_EXAM_TITLE, session['user_id'],))
    return render_template('demo.html', demo_exam=demo_exam)

@app.route('/demo/create_exam', methods=['POST'])
@teacher_required
def create_demo_exam():
    pass
    existing = query_one("SELECT * FROM exams WHERE title=? AND teacher_id=? ORDER BY id DESC LIMIT 1",
                         (DEMO_EXAM_TITLE, session['user_id'],))
    if existing:
        return jsonify({'ok': True, 'exam_id': existing['id'], 'status': existing['status']})

    exam_id = insert("INSERT INTO exams (title,description,teacher_id,duration_minutes,speed_multiplier,status) VALUES (?,?,?,?,?,?)",
                     (DEMO_EXAM_TITLE, '10单选×3 + 5多选×4 + 10判断×2 + 5填空×2 + 2编程×10 = 100分',
                      session['user_id'], 60, 10.0, 'in_progress'))

    configs = [
        ('single_choice', 10, 3), ('multi_choice', 5, 4),
        ('true_false', 10, 2), ('fill_blank', 5, 2), ('programming', 2, 10),
    ]
    order = 0
    for qtype, cnt, pts in configs:
        pool = [r['id'] for r in query("SELECT id FROM questions WHERE type=? ORDER BY id LIMIT ?", (qtype, cnt))]
        for qid in pool:
            order += 1
            insert("INSERT INTO exam_questions (exam_id, question_id, points, order_num) VALUES (?,?,?,?)",
                   (exam_id, qid, pts, order))

    now = datetime.now()
    duration = timedelta(minutes=60)
    end_time = now + duration
    insert("UPDATE exams SET start_time=?, end_time=? WHERE id=?",
           (now.strftime('%Y-%m-%d %H:%M:%S'), end_time.strftime('%Y-%m-%d %H:%M:%S'), exam_id))

    for sid in query("SELECT id FROM users WHERE username LIKE 'stu_%' ORDER BY id"):
        insert("INSERT INTO student_exams (exam_id, student_id, status) VALUES (?,?,?)",
               (exam_id, sid['id'], 'pending'))

    return jsonify({'ok': True, 'exam_id': exam_id, 'status': 'in_progress'})

@app.route('/demo/simulate', methods=['POST'])
@teacher_required
def run_simulation():
    data = request.get_json()
    scenario = str(data.get('scenario', 1))
    exam_id = data.get('exam_id')

    if not exam_id:
        return jsonify({'ok': False, 'error': 'No exam specified'})

    exam = query_one("SELECT * FROM exams WHERE id=? AND teacher_id=?", (exam_id, session['user_id']))
    if not exam:
        return jsonify({'ok': False, 'error': 'Exam not found'})

    _load_baked()
    if scenario not in _baked_data['scenarios']:
        return jsonify({'ok': False, 'error': f'Unknown scenario: {scenario}'})

    version = _get_version(scenario)
    scenario_data = _baked_data['scenarios'][scenario]
    baked_students = scenario_data['versions'][version]['students']

    eqs = query("""
        SELECT eq.*, q.type, q.correct_answer
        FROM exam_questions eq JOIN questions q ON eq.question_id=q.id
        WHERE eq.exam_id=? ORDER BY eq.order_num
    """, (exam_id,))

    if len(eqs) != 32:
        return jsonify({'ok': False, 'error': f'Demo exam must have exactly 32 questions, got {len(eqs)}. ' +
                       'Please use "创建演示考试" to create the correct exam.'})

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    start = (datetime.now() - timedelta(minutes=random.randint(10, 35))).strftime('%Y-%m-%d %H:%M:%S')

    if exam['status'] not in ('in_progress', 'grading', 'completed'):
        insert("UPDATE exams SET status='in_progress' WHERE id=?", (exam_id,))

    scenario_names = {
        '1': '正常班级', '2': '两极分化班', '3': '抄袭泛滥班',
        '4': 'AI风格班', '5': '高分低能班',
    }

    for baked in baked_students:
        student = query_one("SELECT id FROM users WHERE username=?", (baked['username'],))
        if not student:
            continue
        sid = student['id']

        se = query_one("SELECT id FROM student_exams WHERE exam_id=? AND student_id=?", (exam_id, sid))
        if not se:
            se_id = insert("INSERT INTO student_exams (exam_id, student_id, status) VALUES (?,?,?)",
                          (exam_id, sid, 'pending'))
        else:
            se_id = se['id']

        insert("DELETE FROM answers WHERE student_exam_id=?", (se_id,))

        for i, ans in enumerate(baked['answers']):
            eq = eqs[i]
            score = ans['score']
            is_correct = 1 if ans['is_correct'] else 0
            if score > 0 and not ans['is_correct']:
                is_correct = 0
            insert("INSERT INTO answers (student_exam_id, question_id, answer_text, is_correct, score, is_draft) VALUES (?,?,?,?,?,?)",
                   (se_id, eq['question_id'], ans['answer'], is_correct, score, 0))

        insert("UPDATE student_exams SET status='graded', start_time=?, submit_time=?, score=?, tab_switches=? WHERE id=?",
               (start, now, baked['total_score'], baked.get('tab_switches', 0), se_id))

        existing_grade = query_one("SELECT id FROM grades WHERE student_exam_id=?", (se_id,))
        if existing_grade:
            insert("UPDATE grades SET total_score=?, objective_score=?, programming_score=?, graded_at=? WHERE student_exam_id=?",
                   (baked['total_score'], baked.get('objective_score', 0), baked.get('programming_score', 0), now, se_id))
        else:
            insert("INSERT INTO grades (student_exam_id, total_score, objective_score, programming_score, graded_at) VALUES (?,?,?,?,?)",
                   (se_id, baked['total_score'], baked.get('objective_score', 0), baked.get('programming_score', 0), now))

    insert("UPDATE exams SET status='completed' WHERE id=?", (exam_id,))

    with chart_cache_lock:
        keys_to_del = [k for k in chart_cache if str(exam_id) in k]
        for k in keys_to_del:
            del chart_cache[k]

    return jsonify({
        'ok': True,
        'message': f'{scenario_names.get(scenario, "")} 模拟完成',
        'student_count': len(baked_students),
        'exam_id': exam_id,
        'scenario': scenario,
        'version': version,
        'scenario_names': scenario_names
    })

if __name__ == '__main__':
    print("Initializing database...")
    init_db()
    print("Loading seed data...")
    seed_module.run_seed()
    print(f"Starting server on http://localhost:{PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=DEBUG)
