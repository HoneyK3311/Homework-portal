# Homework_Portal.py
import gspread
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
import requests
import time as thread_time
import threading
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
import pytz
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, text

# --- DB 설정 (Raw SQL 직통 연결) ---
# 선생님의 Render 클라우드 DB 주소를 기본값으로 세팅 (보안상 환경변수 권장)
DB_URI = os.getenv("DATABASE_URL", "postgresql://student_db_cgwz_user:hj1xqwey7VPWyaWLBO25ifbhr2y14rGl@dpg-d3ch9fili9vc73diitj0-a.oregon-postgres.render.com/student_db_cgwz")
engine = create_engine(DB_URI)

# --- Flask 앱 초기화 ---
app = Flask(__name__, template_folder='templates')
app.secret_key = 'a_very_secret_and_secure_key_for_session_final' # 세션용 비밀키

# FIX: 알림이 발송된 마지막 날짜 기록 변수
LAST_NOTIFICATION_DATE = None
KST = pytz.timezone('Asia/Seoul')

# --- 전역 설정 ---
SERVICE_ACCOUNT_FILE = 'sheets_service.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
SOURCE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1-9RECRW9CY0TExlsVvTNRVqAonHh5Apyjq18HEwOPII/edit?usp=sharing"
STUDENT_DB_ID = "1Od9PfHV39MSfwfUgWtPun0Y9zCqAdURc-iwd2n0rgBI"
TARGET_SHEET_ID = "1VROqIZ2GmAlQSdw8kZyd_rC6oP_nqTsuVEnWIi0rS24"
NON_SUBMISSION_SHEET_ID = "1-9RECRW9CY0TExlsVvTNRVqAonHh5Apyjq18HEwOPII"

# --- 워크시트 이름 ---
SOURCE_WORKSHEET_NAME = "(탈리)과제제출"
STUDENT_DB_WORKSHEET_NAME = "(통합) 학생DB"
DEADLINE_WORKSHEET_NAME = "제출기한"

# --- 텔레그램 봇 설정 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8355384706:AAG55OSbESovxFJwFI6ZuccbEYEk0J0aPMY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5233769738")

# --- 알리고 (Aligo) API 설정 ---
ALIGO_API_KEY = "fdqm21jhh1zffm5213uvgze5z85go3px"
ALIGO_USER_ID = "kr308"
SENDER_PHONE_NUMBER = "01098159412"

# --- 교직원 계정 설정 ---
STAFF_CREDENTIALS = {
    "kr308": ["!!djqkdntflsdk", "admin"],
    "윤지희": ["04094517", "teacher"],
    "박하린": ["24275057", "teacher"],
    "윤하연": ["53077146", "teacher"]
}

# --- 🚀 글로벌 메모리 캐시 (초고속 렌더링용) ---
GLOBAL_CACHE = {
    'assignments': [],
    'student_levels': {},
    'last_updated': None
}

# ----------------------------------------------------------------
# --- 헬퍼 함수 ---
# ----------------------------------------------------------------
def authenticate_gsheets():
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
    return gspread.authorize(creds)

def get_sheet_as_df(worksheet):
    all_values = worksheet.get_all_values()
    if not all_values: return pd.DataFrame()
    headers, data = all_values[0], all_values[1:]
    df = pd.DataFrame(data)
    if not df.empty: df.columns = headers[:len(df.columns)]
    return df

def send_telegram_message(chat_id, message):
    if "여기에" in TELEGRAM_BOT_TOKEN:
        print(f" (텔레그램 시뮬레이션) 메시지: {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"🚨 텔레그램 메시지 발송 중 예외 발생: {e}")

def send_sms_aligo(phone_number, message):
    if "여기에" in ALIGO_API_KEY:
        print(f" (SMS 시뮬레이션) 받는사람: {phone_number}, 메시지: {message}")
        return
    try:
        url = "https://apis.aligo.in/send/"
        payload = {'key': ALIGO_API_KEY, 'user_id': ALIGO_USER_ID, 'sender': SENDER_PHONE_NUMBER, 'receiver': phone_number, 'msg': message, 'msg_type': 'SMS'}
        response = requests.post(url, data=payload)
        result = response.json()
        if result.get("result_code") != "1":
            print(f"🚨 SMS 발송 실패: {result.get('message', '알 수 없는 오류')}")
    except Exception as e:
        print(f"🚨 SMS 발송 중 예외 발생: {e}")

# ----------------------------------------------------------------
# --- 🚀 핵심 1: 로그인 시 1회 메모리 캐싱 ---
# ----------------------------------------------------------------
def refresh_global_cache():
    """스태프 로그인 시 백그라운드에서 구글 시트를 한 번 읽어 캐시를 최신화합니다."""
    print("🔄 전역 메모리 캐싱 시작... (문항정보 & 레벨)")
    try:
        gc = authenticate_gsheets()
        
        # ✨ 에러 방지: get_all_records() 대신 튼튼한 get_sheet_as_df() 사용
        # 1. 과제 목록(문항 정보) 캐싱
        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        assignments_df = get_sheet_as_df(source_sheet.worksheet("과제목록"))
        GLOBAL_CACHE['assignments'] = assignments_df.to_dict(orient='records')

        # 2. 학생 레벨 캐싱
        roster_df = get_sheet_as_df(gc.open_by_key(STUDENT_DB_ID).worksheet("(통합) 학생DB"))
        level_map = {}
        
        for _, row in roster_df.iterrows():
            if str(row.get('현재상태', '')).strip() == '등록중':
                key = f"{row.get('학생이름')}_{row.get('클래스')}"
                level_map[key] = str(row.get('Level', ''))
                
        GLOBAL_CACHE['student_levels'] = level_map

        GLOBAL_CACHE['last_updated'] = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
        print(f"✅ 메모리 캐싱 완료! (과제 {len(GLOBAL_CACHE['assignments'])}건, 레벨 {len(level_map)}명)")
    except Exception as e:
        print(f"🚨 캐싱 중 오류 발생: {e}")

# ----------------------------------------------------------------
# --- 라우팅: 스태프 전용으로 완전 개편 (학생 기능 삭제) ---
# ----------------------------------------------------------------
@app.route('/')
def landing():
    return redirect(url_for('staff_login_page'))

@app.route('/staff_login')
def staff_login_page():
    return render_template('staff_login.html')

@app.route('/api/staff_login', methods=['POST'])
def handle_staff_login():
    data = request.json
    user_id = data.get('id')
    password = data.get('password')

    user_info = STAFF_CREDENTIALS.get(user_id)

    if user_info and user_info[0] == password:
        session['user_id'] = user_id
        session['user_role'] = user_info[1]
        
        # ✨ 스태프 로그인 성공 시, 백그라운드 스레드로 캐싱 돌리기!
        threading.Thread(target=refresh_global_cache, daemon=True).start()
        
        redirect_url = '/admin' if session['user_role'] == 'admin' else '/grader'
        return jsonify({"success": True, "redirect_url": redirect_url})
    else:
        return jsonify({"success": False, "message": "ID 또는 비밀번호가 올바르지 않습니다."}), 401

@app.route('/staff_logout')
def staff_logout():
    session.pop('user_id', None)
    session.pop('user_role', None)
    return redirect(url_for('staff_login_page'))

# ----------------------------------------------------------------
# --- 🚀 핵심 2: 탈리 웹훅(Webhook) 수신 ---
# ----------------------------------------------------------------
@app.route('/webhook/tally', methods=['POST'])
def handle_tally_webhook():
    """탈리에서 제출 시 실시간 수신하여 DB에 즉시 Insert (API 0회)"""
    payload = request.json
    if not payload: return jsonify({"error": "No payload"}), 400

    try:
        data = payload.get('data', {})
        submission_id = data.get('submissionId')
        created_at = data.get('createdAt')
        
        # 탈리 필드 파싱
        fields = {f.get('label', ''): f.get('value') for f in data.get('fields', [])}
        student_name = fields.get('이름을 입력해주세요. (띄어쓰기 금지)')
        class_name = fields.get('클래스를 선택해주세요.')
        assignment_name = fields.get('과제 번호를 선택해주세요. (반드시 확인요망)')
        # ✨ 추가: 사진 URL 받아오기
        image_url = fields.get('과제 사진을 업로드해주세요.', '')

        if not student_name or not class_name:
            return jsonify({"status": "ignored", "reason": "Missing info"}), 200

        with engine.begin() as conn:
            st_query = text('SELECT "학생ID" FROM students WHERE "학생이름" = :name AND "클래스" = :cls')
            student_id = conn.execute(st_query, {"name": student_name, "cls": class_name}).scalar()
            
            season_query = text("SELECT 마지막동기화시간 FROM sync_status WHERE 작업이름 = 'current_season'")
            season_name = conn.execute(season_query).scalar() or "미분류"

            if not student_id:
                print(f"⚠️ 웹훅 경고: {class_name} {student_name} 학생을 DB에서 찾을 수 없음.")
                return jsonify({"status": "student_not_found"}), 404

            # ✨ 수정: image_url 컬럼 추가하여 Insert
            log_id = f"HW-{datetime.now(KST).strftime('%f')}-{student_id}"
            insert_query = text('''
                INSERT INTO homework_logs 
                ("로그ID", "학생ID", "과제ID", "과제명", "시즌", "제출일시", "제출상태", "교사확인상태", "점수", "오답문항", image_url) 
                VALUES (:log_id, :st_id, :hw_id, :name, :season, :sub_at, :sub_status, :t_status, '', '', :img)
            ''')
            conn.execute(insert_query, {
                "log_id": log_id, "st_id": student_id, "hw_id": submission_id, "name": assignment_name,
                "season": season_name, "sub_at": created_at, "sub_status": "정상제출", "t_status": "미확인", "img": image_url
            })
        
        msg = f"📩 <b>[새 과제 도착]</b>\n{class_name} {student_name}\n과제명: {assignment_name}"
        send_telegram_message(TELEGRAM_CHAT_ID, msg)
        
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"🚨 웹훅 처리 중 오류: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# --- 🚀 핵심 3: 채점 대시보드 (Read 최적화) ---
# ----------------------------------------------------------------
@app.route('/grader')
def index():
    """채점 대시보드 화면(HTML)을 띄워주는 라우트"""
    if session.get('user_role') not in ['teacher', 'admin']:
        return redirect(url_for('staff_login_page'))
    return render_template('index.html', user_role=session.get('user_role'))

@app.route('/api/data')
def get_data():
    """구글 시트 대신 내부 DB를 0.1초 만에 읽어서 렌더링"""
    try:
        submissions = []
        with engine.connect() as conn:
            # ✨ 1. DB에서 '현재 시즌'이 무엇인지 파악합니다.
            season_query = text("SELECT 마지막동기화시간 FROM sync_status WHERE 작업이름 = 'current_season'")
            current_season = conn.execute(season_query).scalar() or "미분류"

            # ✨ 2. WHERE 조건에 `h."시즌" = :season`을 추가하여 옛날 데이터를 쳐냅니다!
            query = text('''
                SELECT h."과제ID", h."제출일시", s."학생이름", s."클래스", h."과제명", h."제출상태", h."교사확인상태", s."level", h.image_url
                FROM homework_logs h
                JOIN students s ON h."학생ID" = s."학생ID"
                WHERE h."교사확인상태" != '반려'
                  AND h."시즌" = :season 
                ORDER BY h."제출일시" DESC
            ''')
            result = conn.execute(query, {"season": current_season})

            for row in result:
                level = row[7] if row[7] else GLOBAL_CACHE['student_levels'].get(f"{row[2]}_{row[3]}", "")
                img_url = row[8] if row[8] else ""
                
                submitted_at_raw = row[1]
                kst_time_str = "-"
                try:
                    if submitted_at_raw:
                        dt = pd.to_datetime(submitted_at_raw)
                        if dt.tzinfo is None:
                            dt = dt + pd.Timedelta(hours=9)
                        else:
                            dt = dt.tz_convert('Asia/Seoul')
                        kst_time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    kst_time_str = str(submitted_at_raw)

                submissions.append({
                    "Submission ID": row[0],
                    "Submitted at": row[1],
                    "제출일시_KST_str": kst_time_str, 
                    "과제 사진을 업로드해주세요.": img_url, 
                    "이름을 입력해주세요. (띄어쓰기 금지)": row[2],
                    "클래스를 선택해주세요.": row[3],
                    "과제 번호를 선택해주세요. (반드시 확인요망)": row[4],
                    "제출상태": row[5],
                    "교사확인상태": row[6],
                    "학생레벨": level
                })

        return jsonify({
            "submissions": submissions,
            "assignments": GLOBAL_CACHE.get('assignments', []) 
        })
    except Exception as e:
        print(f"🚨 /api/data 조회 오류: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# --- 🚀 핵심 4: 채점 완료 (동시 타격: DB + 구글 시트) ---
# ----------------------------------------------------------------
@app.route('/api/update_status', methods=['POST'])
def update_status():
    data = request.json
    action = data.get('action') # 'confirm' or 'reject'
    payload = data.get('payload')
    submission_id = payload.get('submissionId')
    
    teacher_name = session.get('user_id', '알수없음')
    student_class = payload.get('className')
    student_name = payload.get('studentName')
    assignment_name = payload.get('assignmentName')
    
    try:
        new_status = "확인완료" if action == 'confirm' else "반려"
        score_str = f"{payload.get('wrongProblemCount')}/{payload.get('totalProblems')}" if action == 'confirm' else ''
        wrong_ans_str = ", ".join(payload.get('wrongProblemTexts', [])) if action == 'confirm' else ''
        
        student_id_val = ""

        # --- 1. DB 다이렉트 업데이트 (화면 즉시 반영 & 파이프라인 부담 해소) ---
        with engine.begin() as conn:
            # 먼저 학생 ID 조회
            st_query = text('SELECT "학생ID" FROM students WHERE "학생이름"=:n AND "클래스"=:c')
            student_id_val = conn.execute(st_query, {"n": student_name, "c": student_class}).scalar()

            update_query = text('''
                UPDATE homework_logs 
                SET "교사확인상태" = :status, "점수" = :score, "오답문항" = :wrong
                WHERE "과제ID" = :sub_id
            ''')
            conn.execute(update_query, {
                "status": new_status, "score": score_str, "wrong": wrong_ans_str, "sub_id": submission_id
            })

        # --- 2. 구글 시트 동시 타격 (타 프로그램 연동용) ---
        gc = authenticate_gsheets()
        kst_now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
        
        # 2-1) 타겟 통합 파일 (과제제출현황/반려현황) 업데이트
        target_sheet = gc.open_by_key(TARGET_SHEET_ID)
        
        if action == 'confirm':
            worksheet = target_sheet.worksheet("과제제출현황")
            new_row_data = [
                student_class, student_name, assignment_name, payload.get('submissionStatus'), 
                payload.get('totalProblems'), payload.get('wrongProblemCount'), 
                wrong_ans_str, payload.get('memo', ''), kst_now_str, submission_id, student_id_val
            ]
            worksheet.append_row(new_row_data, value_input_option='USER_ENTERED')
            
            msg = f"👍 <b>{teacher_name} 선생님</b>\n{student_class} {student_name}\n'{assignment_name}' 확인 완료\n(결과: {score_str}개 오답)"
            send_telegram_message(TELEGRAM_CHAT_ID, msg)
            
        elif action == 'reject':
            worksheet = target_sheet.worksheet("과제반려현황")
            new_row_data = [
                student_class, student_name, assignment_name, payload.get('reason'), 
                kst_now_str, submission_id, student_id_val
            ]
            worksheet.append_row(new_row_data, value_input_option='USER_ENTERED')
            
            # DB에서 번호 조회 후 SMS 전송
            with engine.connect() as conn:
                phone_query = text('SELECT student_phone FROM students WHERE "학생ID"=:id')
                phone = conn.execute(phone_query, {"id": student_id_val}).scalar()
                if phone:
                    sms_msg = f"[김한이수학] {assignment_name}이 반려되었습니다. ({payload.get('reason')})"
                    send_sms_aligo(phone, sms_msg)
            
            msg = f"❗️ <b>{teacher_name} 선생님</b>\n{student_class} {student_name}\n'{assignment_name}' 반려 처리\n(사유: {payload.get('reason')})"
            send_telegram_message(TELEGRAM_CHAT_ID, msg)

        # 2-2) 탈리 원본 시트 상태도 변경 (선택적 유지 로직)
        try:
            source_worksheet = gc.open_by_url(SOURCE_SHEET_URL).worksheet(SOURCE_WORKSHEET_NAME)
            cell_source = source_worksheet.find(submission_id)
            if cell_source:
                teacher_status_col = source_worksheet.row_values(1).index('교사확인상태') + 1
                source_worksheet.update_cell(cell_source.row, teacher_status_col, new_status)
        except Exception as e:
            print(f"⚠️ 원본 시트 상태 변경 실패 (무시 가능): {e}")

        return jsonify({"success": True, "message": f"DB 및 구글 시트에 {new_status} 처리 완료."})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/get_result_details')
def get_result_details():
    """상세 채점 정보도 무거운 시트 대신 DB에서 0.1초 만에 가져옵니다."""
    if session.get('user_role') not in ['teacher', 'admin']: return jsonify({"error": "Unauthorized"}), 403
    submission_id = request.args.get('id')
    if not submission_id: return jsonify({"error": "Submission ID is required"}), 400

    try:
        with engine.connect() as conn:
            query = text('SELECT "오답문항" FROM homework_logs WHERE "과제ID" = :id')
            wrong_ans = conn.execute(query, {"id": submission_id}).scalar() or ""
            
        return jsonify({
            "memo": "", # 필요시 DB 구조 확장 가능
            "wrongProblemTexts": wrong_ans
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# --- 관리자 페이지 (DB 기반 쿼리 최적화) ---
# ----------------------------------------------------------------
@app.route('/admin')
def admin_page():
    if session.get('user_role') != 'admin':
        return redirect(url_for('staff_login_page'))
    return render_template('admin.html', user_role=session.get('user_role'))

@app.route('/api/admin_dashboard')
def get_admin_dashboard_data():
    if session.get('user_role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
    
    try:
        # 기존 Pandas 로직을 유지하되, 
        # 느려터진 gspread 대신 DB에서 pd.read_sql 로 빛의 속도로 DataFrame을 만듭니다.
        roster_df = pd.read_sql(text('SELECT "학생이름", "클래스" FROM students WHERE "현재상태" = \'등록중\''), engine)
        submissions_df = pd.read_sql(text('SELECT "과제명", s."학생이름", "제출상태", "교사확인상태" FROM homework_logs h JOIN students s ON h."학생ID" = s."학생ID" WHERE "교사확인상태" != \'반려\''), engine)
        deadlines_df = pd.read_sql(text('SELECT "클래스", "과제명", "제출기한" FROM homework_definitions'), engine)

        # 컬럼명을 기존 프론트엔드가 이해하던 방식으로 매핑
        submissions_df = submissions_df.rename(columns={"학생이름": "이름을 입력해주세요. (띄어쓰기 금지)", "과제명": "과제 번호를 선택해주세요. (반드시 확인요망)"})

        if roster_df.empty:
            return jsonify({"summary_stats": {"total_required": 0, "total_completed": 0, "total_missing": 0}, "charts_data": {"by_assignment": {}, "overall_by_class": []}, "honor_rank": {"top10": [], "bottom10": []}})

        kst_now = datetime.now(ZoneInfo('Asia/Seoul'))
        today_start_aware = kst_now.replace(hour=0, minute=0, second=0, microsecond=0)
        current_year = kst_now.year
        
        deadlines_df['기한_날짜'] = deadlines_df['제출기한'].str.extract(r'(\d{1,2}/\d{1,2})').iloc[:, 0]
        deadlines_df['제출마감_datetime'] = pd.to_datetime(f'{current_year}/' + deadlines_df['기한_날짜'], format='%Y/%m/%d', errors='coerce')
        past_due_assignments_df = deadlines_df[deadlines_df['제출마감_datetime'] < today_start_aware.replace(tzinfo=None)].dropna(subset=['제출마감_datetime'])
        
        if past_due_assignments_df.empty:
            return jsonify({"summary_stats": {"total_required": 0, "total_completed": 0, "total_missing": 0}, "charts_data": {"by_assignment": {}, "overall_by_class": []}, "honor_rank": {"top10": [], "bottom10": []}})

        required_submissions = pd.merge(roster_df, past_due_assignments_df, on='클래스')

        merged_df = pd.merge(
            required_submissions,
            submissions_df,
            left_on=['학생이름', '과제명'],
            right_on=['이름을 입력해주세요. (띄어쓰기 금지)', '과제 번호를 선택해주세요. (반드시 확인요망)'],
            how='left'
        )
        
        def determine_status(row):
            if pd.isna(row['제출상태']): return '미제출'
            if '정상' in str(row['제출상태']): return '정상제출'
            return '지각제출'
        merged_df['final_status'] = merged_df.apply(determine_status, axis=1)

        student_performance = merged_df.groupby(['학생이름', '클래스'])['final_status'].value_counts().unstack(fill_value=0)
        for col in ['정상제출', '지각제출', '미제출']:
            if col not in student_performance: student_performance[col] = 0
        student_performance = student_performance.rename(columns={'정상제출': 'on_time', '지각제출': 'late', '미제출': 'missing'})

        class_summary_agg = merged_df.groupby('클래스').agg(
            required=('final_status', 'size'),
            completed=('final_status', lambda x: (x != '미제출').sum())
        ).reset_index()

        assignment_summary_agg = merged_df.groupby(['클래스', '과제명']).agg(
            total=('final_status', 'size'),
            completed=('final_status', lambda x: (x != '미제출').sum())
        ).reset_index()

        summary_stats = {"total_required": int(class_summary_agg['required'].sum()), "total_completed": int(class_summary_agg['completed'].sum()), "total_missing": int(class_summary_agg['required'].sum() - class_summary_agg['completed'].sum())}
        
        chart_overall_by_class = [{"class_name": row['클래스'], "rate": round((row['completed'] / row['required'] * 100) if row['required'] > 0 else 0, 1), "details": f"{row['completed']} / {row['required']}건"} for _, row in class_summary_agg.iterrows()]

        chart_data_by_assignment = {}
        for _, row in assignment_summary_agg.iterrows():
            c_name = row['클래스']
            if c_name not in chart_data_by_assignment: chart_data_by_assignment[c_name] = []
            rate = (row['completed'] / row['total'] * 100) if row['total'] > 0 else 0
            chart_data_by_assignment[c_name].append({"assignment_name": row['과제명'], "submission_rate": round(rate, 1), "details": f"{row['completed']} / {row['total']}명"})
        
        ranked_students = student_performance.reset_index().sort_values(by=['missing', 'late', 'on_time'], ascending=[False, False, True])
        
        grouped_ranks = []
        if not ranked_students.empty:
            for stats_tuple, group in ranked_students.groupby(['missing', 'late', 'on_time']):
                stats_dict = {'missing': int(stats_tuple[0]), 'late': int(stats_tuple[1]), 'on_time': int(stats_tuple[2])}
                grouped_ranks.append({"stats": stats_dict, "names": [(n, c) for n, c in group[['학생이름', '클래스']].values]})
        grouped_ranks.sort(key=lambda x: (x['stats']['missing'], x['stats']['late'], -x['stats']['on_time']))

        dashboard_data = {
            "summary_stats": summary_stats, 
            "charts_data": { "by_assignment": chart_data_by_assignment, "overall_by_class": chart_overall_by_class }, 
            "honor_rank": { "top10": grouped_ranks[:10], "bottom10": grouped_ranks[-10:][::-1] }
        }
        return jsonify(dashboard_data)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# --- 백그라운드 워커: 매일 오전 11시 미제출 알림 발송 ---
# ----------------------------------------------------------------
def run_worker():
    global LAST_NOTIFICATION_DATE
    kst_now = datetime.now(ZoneInfo('Asia/Seoul'))
    
    # 매일 오전 11시에 한 번만 실행 (부팅 시 최초 1회 발송 로직 포함)
    if kst_now.hour >= 11 and LAST_NOTIFICATION_DATE != kst_now.date():
        print("\n✨ 미제출 과제 알림 발송 시간입니다. 작업을 시작합니다.")
        notification_sent_students = []
        
        try:
            gc = authenticate_gsheets()
            
            # --- 1. '시즌' 탭에서 오늘 날짜에 해당하는 '현재 시즌 시트ID' 찾기 ---
            season_sheet = gc.open_by_key(TARGET_SHEET_ID).worksheet("시즌")
            season_df = get_sheet_as_df(season_sheet)
            
            today = kst_now.date()
            current_season_sheet_id = None
            current_season_name = "알수없음"
            
            print(f"\n  🔍 [디버그] 오늘 날짜({today})가 포함된 시즌을 찾습니다...")
            
            for _, row in season_df.iterrows():
                try:
                    # ✨ 사진에 맞춰서 헤더(컬럼명)를 완벽하게 매칭했습니다!
                    start_str = str(row.get('시작일', '')).strip().replace('.', '-').replace('/', '-')
                    end_str = str(row.get('종료일', '')).strip().replace('.', '-').replace('/', '-')
                    sheet_id = str(row.get('과제제출_파일ID', '')).strip()  # <- 범인 검거 1
                    season_name = str(row.get('시즌이름', '')).strip()      # <- 범인 검거 2
                    
                    if start_str and end_str and sheet_id:
                        # 텍스트가 아닌 진짜 날짜(Date) 객체로 변환하여 KST 오늘 날짜와 비교
                        start_date = pd.to_datetime(start_str).date()
                        end_date = pd.to_datetime(end_str).date()
                        
                        print(f"  👀 [탐색 중] 읽어온 데이터 -> 시즌: {season_name}, 기간: {start_date} ~ {end_date}")
                        
                        # 오늘 날짜가 시작일과 종료일 사이에 있다면!
                        if start_date <= today <= end_date:
                            current_season_sheet_id = sheet_id
                            current_season_name = season_name
                            print(f"  - 🎯 빙고! 현재 시즌을 찾았습니다: {current_season_name} (시트ID: {current_season_sheet_id})")
                            break
                except Exception as e:
                    print(f"  - ⚠️ [경고] '{season_name}' 행의 날짜 형식을 읽지 못했습니다. (오류: {e})")
                    continue 
                    
            if not current_season_sheet_id:
                print("  - 🚨 [결과] '시즌' 시트를 다 뒤졌지만 오늘 날짜가 포함된 시즌이 없습니다! 발송을 종료합니다.")
                LAST_NOTIFICATION_DATE = kst_now.date() 
                return

            # --- 2. 찾아낸 현재 시즌 시트에서 '미제출현황' 열기 ---
            try:
                non_submission_sheet = gc.open_by_key(current_season_sheet_id).worksheet("미제출현황")
            except Exception as e:
                print(f"  - 🚨 해당 시즌 시트에서 '미제출현황' 탭을 찾을 수 없습니다: {e}")
                LAST_NOTIFICATION_DATE = kst_now.date()
                return

            # 발송 로그는 통합 파일(TARGET_SHEET_ID)에 계속 누적 기록
            log_sheet = gc.open_by_key(TARGET_SHEET_ID).worksheet("문자발송로그")
            
            non_submission_df = get_sheet_as_df(non_submission_sheet)
            log_df = get_sheet_as_df(log_sheet)

            # --- 3. 미제출자 필터링 및 SMS 발송 ---
            non_submission_df.dropna(subset=['미제출과제번호'], inplace=True)
            non_submission_df = non_submission_df[non_submission_df['미제출과제번호'] != '']
            non_submission_df['미제출과제번호'] = non_submission_df['미제출과제번호'].astype(str)
            
            if non_submission_df.empty:
                print(f"  - [{current_season_name}] 알림을 보낼 미제출 과제가 없습니다.")
            else:
                reminders = non_submission_df.groupby(['클래스', '이름'])['미제출과제번호'].apply(list).reset_index()
                today_str = kst_now.strftime('%Y-%m-%d')

                # DB 직통 연결로 전화번호 초고속 조회
                with engine.connect() as conn:
                    for index, row in reminders.iterrows():
                        class_name = row['클래스']
                        student_name = row['이름']
                        
                        # 오늘 이미 보냈는지 중복 확인 (로그 시트)
                        already_sent = False
                        if not log_df.empty and '이름' in log_df.columns:
                            sent_log = log_df[
                                (log_df['이름'] == student_name) & 
                                (log_df['클래스'] == class_name) & 
                                (log_df['발송일'] == today_str) & 
                                (log_df['종류'] == '미제출알림')
                            ]
                            if not sent_log.empty: already_sent = True
                        
                        if already_sent:
                            print(f"  - [SKIP] {class_name} {student_name} 학생은 오늘 이미 알림을 받았습니다.")
                            continue

                        # DB에서 전화번호 추출
                        st_query = text('SELECT student_phone FROM students WHERE "학생이름"=:n AND "클래스"=:c')
                        phone_number = conn.execute(st_query, {"n": student_name, "c": class_name}).scalar()

                        if phone_number:
                            hw_numbers = ", ".join(sorted(row['미제출과제번호']))
                            message = f"[김한이수학] 과제 {hw_numbers}가 미제출 중.....😰"
                            
                            send_sms_aligo(phone_number, message)
                            
                            # 로그 시트에 발송 기록 추가
                            log_row = [today_str, class_name, student_name, '미제출알림', message]
                            log_sheet.append_row(log_row, value_input_option='USER_ENTERED')
                            
                            notification_sent_students.append(f"{class_name} {student_name}")
                        else:
                            print(f"  - ⚠️ {class_name} {student_name} 학생의 전화번호를 DB에서 찾을 수 없습니다.")

            # 관리자 텔레그램 보고
            if notification_sent_students:
                report_message = f"[{current_season_name} 미제출알림 발송완료]\n총 {len(notification_sent_students)}명\n\n" + "\n".join(notification_sent_students)
                telegram_report_title = f"🔔 <b>미제출 과제 알림 요약 ({kst_now.strftime('%m/%d')})</b>\n\n"
                send_telegram_message(TELEGRAM_CHAT_ID, telegram_report_title + report_message)
            else:
                send_telegram_message(TELEGRAM_CHAT_ID, f"[{current_season_name} 미제출알림] {kst_now.strftime('%m/%d')} 신규 발송 대상자가 없습니다.")
            
            # 발송 완료 마킹
            LAST_NOTIFICATION_DATE = kst_now.date()
            print(f"🎉 미제출 알림 발송 완료. 다음 알림은 내일입니다.\n")

        except Exception as e:
            print(f"🚨 [Worker/미제출알림] 오류 발생: {e}\n")

def background_worker_task():
    print("✅ 백그라운드 작업(문자 스케줄러) 루프를 시작합니다.")
    while True:
        try:
            run_worker()
        except Exception as e:
            print(f"🚨 백그라운드 스레드 오류: {e}")
        thread_time.sleep(60)

worker_thread = threading.Thread(target=background_worker_task, daemon=True)
worker_thread.start()

# ----------------------------------------------------------------
# --- 동기화 진단 툴 (관리자용) ---
# ----------------------------------------------------------------
@app.route('/sync')
def sync_graded_data():
    return "<h1>[안내] DB-GS 실시간 연동이 완료되어 수동 동기화 라우트는 비활성화되었습니다.</h1>", 200

# ----------------------------------------------------------------
# --- 프론트엔드 호환용: 초고속 레벨 조회 API ---
# ----------------------------------------------------------------
@app.route('/api/get_student_level')
def get_student_level():
    """프론트엔드의 기존 요청을 받아 메모리 캐시에서 0.001초 만에 레벨을 반환합니다."""
    if session.get('user_role') not in ['teacher', 'admin']:
        return jsonify({"error": "Unauthorized"}), 403

    student_name = request.args.get('student_name')
    class_name = request.args.get('class_name')
    
    if not student_name or not class_name:
        return jsonify({"error": "학생 이름과 클래스가 필요합니다."}), 400

    try:
        # 구글 시트를 열지 않고, 로그인할 때 저장해둔 글로벌 캐시에서 바로 꺼냅니다!
        cache_key = f"{student_name}_{class_name}"
        student_level = GLOBAL_CACHE['student_levels'].get(cache_key, "")
        
        return jsonify({"level": student_level})

    except Exception as e:
        print(f"🚨 학생 레벨 조회 중 오류: {e}")
        return jsonify({"error": "레벨확인"}), 500

# ----------------------------------------------------------------
# --- 서버 실행 ---
# ----------------------------------------------------------------
if __name__ == '__main__':
    # ✨ 서버 부팅 시점에 무조건 캐싱을 1회 실행하여 텅 빈 메모리를 방지합니다!
    print("🚀 서버 부팅 중... 초기 데이터 캐싱을 시작합니다.")
    refresh_global_cache()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)