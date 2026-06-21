import sqlite3
import threading
import os
from config import DATABASE_PATH

write_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

db = get_db()

def init_db():
    cursor = db.cursor()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('teacher', 'student')),
            display_name TEXT,
            linked_user_id INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS knowledge_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            category TEXT
        );

        CREATE TABLE IF NOT EXISTS knowledge_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_kp_id INTEGER NOT NULL REFERENCES knowledge_points(id),
            to_kp_id INTEGER NOT NULL REFERENCES knowledge_points(id),
            UNIQUE(from_kp_id, to_kp_id)
        );

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('single_choice', 'multi_choice', 'true_false', 'fill_blank', 'programming')),
            title TEXT NOT NULL,
            options TEXT,
            correct_answer TEXT NOT NULL,
            difficulty INTEGER CHECK(difficulty BETWEEN 1 AND 5),
            knowledge_points TEXT,
            aliases TEXT,
            test_cases TEXT,
            code_template TEXT,
            ai_code_template TEXT,
            reference_code TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            teacher_id INTEGER NOT NULL REFERENCES users(id),
            status TEXT DEFAULT 'draft' CHECK(status IN ('draft','published','in_progress','grading','completed')),
            duration_minutes INTEGER NOT NULL DEFAULT 60,
            speed_multiplier REAL DEFAULT 1.0,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS exam_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            points REAL DEFAULT 0,
            order_num INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS student_exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES users(id),
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending','in_progress','submitted','graded','timeout')),
            start_time TIMESTAMP,
            submit_time TIMESTAMP,
            score REAL,
            tab_switches INTEGER DEFAULT 0,
            preset_profile TEXT
        );

        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_exam_id INTEGER NOT NULL REFERENCES student_exams(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            answer_text TEXT,
            is_correct INTEGER DEFAULT 0,
            score REAL DEFAULT 0,
            is_draft INTEGER DEFAULT 1,
            preset_correct INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_exam_id INTEGER NOT NULL REFERENCES student_exams(id) ON DELETE CASCADE,
            total_score REAL DEFAULT 0,
            objective_score REAL DEFAULT 0,
            programming_score REAL DEFAULT 0,
            graded_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS wrong_question_book (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES users(id),
            question_id INTEGER NOT NULL REFERENCES questions(id),
            exam_id INTEGER REFERENCES exams(id) ON DELETE SET NULL,
            student_answer TEXT,
            is_correct INTEGER DEFAULT 0,
            mastered INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tab_switch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_exam_id INTEGER NOT NULL REFERENCES student_exams(id) ON DELETE CASCADE,
            switch_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(type);
        CREATE INDEX IF NOT EXISTS idx_exams_status ON exams(status);
        CREATE INDEX IF NOT EXISTS idx_student_exams_status ON student_exams(status);
        CREATE INDEX IF NOT EXISTS idx_answers_draft ON answers(is_draft);
        CREATE INDEX IF NOT EXISTS idx_wrong_book_student ON wrong_question_book(student_id);
    ''')
    db.commit()

def query(sql, params=(), one=False):
    cur = db.cursor()
    sql_upper = sql.strip().upper()
    if sql_upper.startswith('SELECT') or sql_upper.startswith('PRAGMA'):
        cur.execute(sql, params)
        if one:
            return cur.fetchone()
        return cur.fetchall()
    else:
        with write_lock:
            cur.execute(sql, params)
            db.commit()
            return cur.lastrowid

def query_one(sql, params=()):
    return query(sql, params, one=True)

def insert(sql, params=()):
    return query(sql, params)

def count(sql, params=()):
    row = query_one(sql, params)
    return row[0] if row else 0
