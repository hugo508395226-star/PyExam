import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, 'data', 'exam_system.db')
SECRET_KEY = 'online-exam-system-secret-key-2024'
PORT = 5000
DEBUG = True
