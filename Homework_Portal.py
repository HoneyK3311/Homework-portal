import gspread
import pandas as pd
# 기존 인증 방식에 필요한 라이브러리
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta, time
import os
import json
import requests
import time as thread_time
import threading
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
import pytz
from zoneinfo import ZoneInfo

# --- Flask 앱 초기화 ---
app = Flask(__name__, template_folder='templates')
app.secret_key = 'a_very_secret_and_secure_key_for_session_final' # 세션용 비밀키

# FIX: 알림이 발송된 마지막 날짜를 기록할 변수 추가
LAST_NOTIFICATION_DATE = None

# --- 전역 설정 ---
SERVICE_ACCOUNT_FILE = 'sheets_service.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
SOURCE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1vB_YA_wRnr9t3HnoKNOJG3K_d365gsK4wN7zs-8IYdA/edit?usp=sharing"
STUDENT_DB_ID = "1Od9PfHV39MSfwfUgWtPun0Y9zCqAdURc-iwd2n0rgBI"
TARGET_SHEET_ID = "1VROqIZ2GmAlQSdw8kZyd_rC6oP_nqTsuVEnWIi0rS24"
NON_SUBMISSION_SHEET_ID = "1vB_YA_wRnr9t3HnoKNOJG3K_d365gsK4wN7zs-8IYdA"

# --- 워크시트 이름 ---
SOURCE_WORKSHEET_NAME = "(탈리)과제제출"
STUDENT_DB_WORKSHEET_NAME = "(통합) 학생DB"
DEADLINE_WORKSHEET_NAME = "제출기한"

# --- 알리고 (Aligo) API 설정 ---
ALIGO_API_KEY = "fdqm21jhh1zffm5213uvgze5z85go3px"
ALIGO_USER_ID = "kr308"
SENDER_PHONE_NUMBER = "01098159412"

# --- 교직원 계정 설정 ---
# 형식: "ID": ["비밀번호", "역할"]
STAFF_CREDENTIALS = {
    # --- 관리자 계정 ---
    "kr308": ["!!djqkdntflsdk", "admin"],   # 관리자는 한 명

    # --- 스태프(교사) 계정들 ---
    "윤지혜": ["04094517", "teacher"], # A 선생님
    "박하린": ["24275057", "teacher"], # B 선생님
    "박세린": ["24273738", "teacher"], # C 선생님
    "윤하연": ["53077146", "teacher"]  # D 선생님
    # 필요한 만큼 "ID": ["비번", "teacher"] 형식으로 계속 추가...
}



# --- 핵심 기능 함수 ---
def authenticate_gsheets():
    """구글 시트 인증 (기존 oauth2client 방식)"""
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPES)
    return gspread.authorize(creds)

def get_sheet_as_df(worksheet):
    """시트 데이터를 DataFrame으로 변환 (안정성 강화 버전)"""
    all_values = worksheet.get_all_values()
    if not all_values:
        return pd.DataFrame() # 시트가 비어있으면 빈 DataFrame 반환
    
    headers = all_values[0]
    data = all_values[1:]
    
    # 데이터가 헤더보다 짧은 경우를 대비하여 헤더 길이를 데이터에 맞춤
    df = pd.DataFrame(data)
    if not df.empty:
        df.columns = headers[:len(df.columns)]
    
    return df

def get_student_id(roster_df, student_name, class_name):
    """학생 이름과 클래스로 학생ID를 찾아서 반환하는 함수"""
    try:
        student_info = roster_df[(roster_df['학생이름'] == student_name) & (roster_df['클래스'] == class_name)]
        
        if not student_info.empty:
            # A열(첫 번째 컬럼)을 직접 가져오기
            student_id = student_info.iloc[0].iloc[0]  # A열 = 인덱스 0
            return str(student_id) if student_id else ""
        else:
            print(f"⚠️ {class_name}의 {student_name} 학생을 학생DB에서 찾을 수 없습니다.")
            return ""
    except Exception as e:
        print(f"🚨 학생ID 조회 중 오류: {e}")
        return ""

# ----------------------------------------------------------------
# --- 문자 발송 및 백그라운드 워커 ---
# ----------------------------------------------------------------
def send_sms_aligo(phone_number, message):
    if "여기에" in ALIGO_API_KEY:
        print(f" (SMS 시뮬레이션) 받는사람: {phone_number}, 메시지: {message}")
        return
    try:
        url = "https://apis.aligo.in/send/"
        payload = { 'key': ALIGO_API_KEY, 'user_id': ALIGO_USER_ID, 'sender': SENDER_PHONE_NUMBER, 'receiver': phone_number, 'msg': message, 'msg_type': 'SMS' }
        response = requests.post(url, data=payload)
        result = response.json()
        if result.get("result_code") == "1": print(f"✅ SMS 발송 성공! -> 받는사람: {phone_number}")
        else: print(f"🚨 SMS 발송 실패: {result.get('message', '알 수 없는 오류')}")
    except Exception as e:
        print(f"🚨 SMS 발송 중 예외 발생: {e}")

def run_worker():
    # --- 1. 새로운 과제 처리 ---
    try:
        kst_now_for_submission = datetime.now(ZoneInfo('Asia/Seoul'))
        print(f"⚙️ 백그라운드 작업기 실행... (현재 시간: {kst_now_for_submission.strftime('%H:%M:%S')})")
        
        gc = authenticate_gsheets()
        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        submission_worksheet = source_sheet.worksheet(SOURCE_WORKSHEET_NAME)
        deadline_worksheet = source_sheet.worksheet(DEADLINE_WORKSHEET_NAME)
        roster_sheet = gc.open_by_key(STUDENT_DB_ID).worksheet(STUDENT_DB_WORKSHEET_NAME)
        
        submissions_df = get_sheet_as_df(submission_worksheet)
        deadlines_df = get_sheet_as_df(deadline_worksheet)
        roster_df = get_sheet_as_df(roster_sheet)

        if submissions_df.empty: 
            print("✅ [Worker] 처리할 과제가 없습니다.")
        else:
            unprocessed_submissions = submissions_df[submissions_df['제출상태'] == ''].copy()
            if unprocessed_submissions.empty: 
                print("✅ [Worker] 새로운 과제가 없습니다.")
            else:
                print(f"✨ [Worker] {len(unprocessed_submissions)}개의 새로운 과제를 발견했습니다.")
                current_year = datetime.now().year
                deadlines_df['제출기한_날짜'] = deadlines_df['제출기한'].str.extract(r'(\d{1,2}/\d{1,2})')
                deadlines_df['제출마감_datetime'] = pd.to_datetime(f'{current_year}/' + deadlines_df['제출기한_날짜'], format='%Y/%m/%d', errors='coerce') + pd.to_timedelta('23 hours 59 minutes 59 seconds')

                for index, row in unprocessed_submissions.iterrows():
                    row_index_in_sheet = index + 2
                    submitted_at_utc = pd.to_datetime(row['Submitted at'], errors='coerce')
                    submitted_at_kst = submitted_at_utc.tz_localize('UTC').tz_convert('Asia/Seoul') if pd.notna(submitted_at_utc) else None

                    student_name = row['이름을 입력해주세요. (띄어쓰기 금지)']
                    student_class = row['클래스를 선택해주세요.']
                    assignment_name = row['과제 번호를 선택해주세요. (반드시 확인요망)']

                    deadline_info = deadlines_df[(deadlines_df['클래스'] == student_class) & (deadlines_df['과제명'] == assignment_name)]
                    status = "정상제출" if not deadline_info.empty and submitted_at_kst and submitted_at_kst.tz_localize(None) <= deadline_info.iloc[0]['제출마감_datetime'] else "지각제출"
                    
                    # 1) 학생ID 가져오기
                    student_id = get_student_id(roster_df, student_name, student_class)
                    
                    header = submission_worksheet.row_values(1)
                    submission_status_col = header.index('제출상태') + 1
                    teacher_status_col = header.index('교사확인상태') + 1
                    student_id_col = 10  # J열 (1부터 시작하므로 10)
                    
                    submission_worksheet.update_cell(row_index_in_sheet, submission_status_col, status)
                    submission_worksheet.update_cell(row_index_in_sheet, teacher_status_col, '미확인')
                    submission_worksheet.update_cell(row_index_in_sheet, student_id_col, student_id)
                    
                    print(f"  - {row_index_in_sheet}행: '{status}' / '미확인' / 학생ID '{student_id}' 업데이트 완료")

                    student_info = roster_df[(roster_df['학생이름'] == student_name) & (roster_df['클래스'] == student_class)]
                    if not student_info.empty:
                        phone_number = str(student_info.iloc[0]['학생전화'])
                        if phone_number:
                            message = f"[김한이수학] {assignment_name} 제출 완료! ({status})"
                            send_sms_aligo(phone_number, message)
                    else:
                        print(f"⚠️ {student_class}의 {student_name} 학생을 학생DB에서 찾을 수 없습니다.")
                
                print("✅ [Worker] 모든 새로운 과제 처리를 완료했습니다.")
    except Exception as e:
        print(f"🚨 [Worker/과제처리] 작업 중 오류 발생: {e}")


    # --- 2. 매일 오전 11시에 미제출 알림 발송 (로직 수정) ---
    global LAST_NOTIFICATION_DATE
    
    kst_now = datetime.now(ZoneInfo('Asia/Seoul'))
    
    # FIX: 알림 시간을 오전 11시로 변경
    if kst_now.hour >= 11 and LAST_NOTIFICATION_DATE != kst_now.date():
        print("\n✨ 미제출 과제 알림 발송 시간입니다. (11시) 작업을 시작합니다.")
        
        notification_sent_students = []  # 4) 발송자 명단을 저장할 리스트
        
        try:
            gc = authenticate_gsheets()
            non_submission_sheet = gc.open_by_key(NON_SUBMISSION_SHEET_ID).worksheet("미제출현황")
            roster_sheet = gc.open_by_key(STUDENT_DB_ID).worksheet("(통합) 학생DB")
            
            non_submission_df = get_sheet_as_df(non_submission_sheet)
            roster_df = get_sheet_as_df(roster_sheet)

            non_submission_df.dropna(subset=['미제출과제번호'], inplace=True)
            non_submission_df = non_submission_df[non_submission_df['미제출과제번호'] != '']
            non_submission_df['미제출과제번호'] = non_submission_df['미제출과제번호'].astype(str)
            
            if non_submission_df.empty:
                print("  - 알림을 보낼 미제출 과제가 없습니다.")
            else:
                reminders = non_submission_df.groupby(['클래스', '이름'])['미제출과제번호'].apply(list).reset_index()
                print(f"  - 총 {len(reminders)}명의 학생에게 미제출 알림을 발송합니다.")

                for index, row in reminders.iterrows():
                    class_name = row['클래스']
                    student_name = row['이름']
                    hw_numbers = ", ".join(sorted(row['미제출과제번호']))
                    
                    student_info = roster_df[(roster_df['클래스'] == class_name) & (roster_df['학생이름'] == student_name)]
                    
                    if not student_info.empty:
                        phone_number = str(student_info.iloc[0]['학생전화'])
                        if phone_number:
                            message = f"[김한이수학] 과제 {hw_numbers}가 미제출 중.....😰"
                            print(f"  - {class_name} {student_name} 학생에게 발송...")
                            send_sms_aligo(phone_number, message)
                            notification_sent_students.append(f"{class_name} {student_name}")  # 발송자 명단에 추가
                    else:
                        print(f"  - ⚠️ {class_name} {student_name} 학생을 학생DB에서 찾을 수 없습니다.")
            
            # 4) 발송자 명단을 관리자에게 보고
            if notification_sent_students:
                report_message = f"[김한이수학 미제출알림 발송완료]\n총 {len(notification_sent_students)}명\n\n" + "\n".join(notification_sent_students)
                print(f"  - 관리자에게 발송자 명단 보고 중...")
                send_sms_aligo("01097559412", report_message)
            else:
                report_message = "[김한이수학 미제출알림] 오늘은 발송할 미제출 학생이 없습니다."
                send_sms_aligo("01097559412", report_message)
            
            LAST_NOTIFICATION_DATE = kst_now.date()
            print(f"🎉 미제출 과제 알림 발송 작업을 완료했습니다. ({LAST_NOTIFICATION_DATE}) 다음 알림은 내일입니다.\n")

        except Exception as e:
            print(f"🚨 [Worker/미제출알림] 작업 중 오류 발생: {e}\n")


def background_worker_task():
    """백그라운드에서 run_worker 함수를 주기적으로 실행하는 함수 (안정성 강화)"""
    print("✅ 백그라운드 작업 루프를 시작합니다.")
    while True:
        try:
            run_worker()
        except Exception as e:
            # run_worker 함수 자체에서 심각한 오류가 발생해도 루프는 계속됩니다.
            print(f"🚨🚨 [CRITICAL] 백그라운드 스레드에 치명적인 오류가 발생했습니다: {e}")
            print("    -> 15초 후 작업을 재시도합니다.")
        
        thread_time.sleep(20)  # 60초 → 20초로 변경

# --- 페이지 렌더링 ---
@app.route('/')
def landing():
    """새로운 랜딩 페이지를 보여줍니다."""
    return render_template('landing.html')




# ----------------------------------------------------------------
# --- 채점 페이지 (index.html) 관련 API ---
# ----------------------------------------------------------------
@app.route('/grader')
def index():
    """채점 페이지를 보여줍니다."""
    if session.get('user_role') not in ['teacher', 'admin']:
        return redirect(url_for('staff_login_page'))
    # 템플릿에 user_role 변수를 전달합니다.
    return render_template('index.html', user_role=session.get('user_role'))

@app.route('/api/data')
def get_data():
    try:
        gc = authenticate_gsheets()
        
        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        submissions_df = get_sheet_as_df(source_sheet.worksheet(SOURCE_WORKSHEET_NAME))
        assignments_df = get_sheet_as_df(source_sheet.worksheet("과제목록"))
        
        # 날짜 형식 변환 등 필요한 데이터 가공
        submissions_df['Submitted at'] = pd.to_datetime(submissions_df['Submitted at'], errors='coerce')
        submissions_df['제출일시_KST'] = submissions_df['Submitted at'].dropna() + pd.Timedelta(hours=9)
        submissions_df['제출일시_KST_str'] = submissions_df['제출일시_KST'].apply(
            lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if pd.notna(x) else '-'
        )
        
        result = {
            "submissions": submissions_df.to_dict(orient='records'),
            "assignments": assignments_df.to_dict(orient='records'),
        }
        return jsonify(result)
        
    except Exception as e:
        print(f"데이터 로딩 오류 (/api/data): {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/update_status', methods=['POST'])
def update_status():
    data = request.json
    action = data.get('action')
    payload = data.get('payload')
    try:
        gc = authenticate_gsheets()
        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        source_worksheet = source_sheet.worksheet(SOURCE_WORKSHEET_NAME)
        roster_sheet = gc.open_by_key(STUDENT_DB_ID).worksheet(STUDENT_DB_WORKSHEET_NAME)
        roster_df = get_sheet_as_df(roster_sheet)
        
        cell_source = source_worksheet.find(payload.get('submissionId'))
        if not cell_source:
            return jsonify({"success": False, "message": "원본 시트에서 해당 과제를 찾을 수 없습니다."}), 404
        
        target_row_source = cell_source.row
        new_status = "확인완료" if action == 'confirm' else "반려"
        
        target_sheet = gc.open_by_key(TARGET_SHEET_ID)
        
        # 2) 3) 학생ID 가져오기
        student_id = get_student_id(roster_df, payload.get('studentName'), payload.get('className'))
        
        message = ""
        
        if action == 'confirm':
            worksheet = target_sheet.worksheet("과제제출현황")
            
            wrong_problems_list = payload.get('wrongProblemTexts', [])
            wrong_problems_str = ", ".join(wrong_problems_list)
            
            kst_now = datetime.now(ZoneInfo('Asia/Seoul'))
            grading_timestamp_str = kst_now.strftime('%Y-%m-%d %H:%M:%S')
            
            # 2) 사용자 헤더 순서: 클래스, 이름, 과제명, 제출상태, 전체문항수, 틀린문항수, 오답문항, 메모확인, 시간, 과제ID, 학생ID
            new_row_data = [
                payload.get('className'),
                payload.get('studentName'),
                payload.get('assignmentName'),
                payload.get('submissionStatus'),
                payload.get('totalProblems'),
                payload.get('wrongProblemCount'),
                wrong_problems_str,
                payload.get('memo', ''), # 메모 정보 추가
                grading_timestamp_str,
                payload.get('submissionId'),
                student_id  # K열에 학생ID 추가
            ]
            
            df = get_sheet_as_df(worksheet)
            if not df.empty and '과제ID' in df.columns and payload.get('submissionId') in df['과제ID'].values:
                existing_row_index = df[df['과제ID'] == payload.get('submissionId')].index[0] + 2
                worksheet.update(f'A{existing_row_index}:K{existing_row_index}', [new_row_data])
                message = "채점 결과가 업데이트되었습니다."
            else:
                worksheet.append_row(new_row_data, value_input_option='USER_ENTERED')
                message = "채점 결과가 저장되었습니다."
                    
        elif action == 'reject':
            worksheet = target_sheet.worksheet("과제반려현황")
            
            kst_now = datetime.now(ZoneInfo('Asia/Seoul'))
            rejection_timestamp_str = kst_now.strftime('%Y-%m-%d %H:%M:%S')
            
            # 3) 사용자 헤더 순서: 클래스, 이름, 과제명, 반려사유, 반려시간, 과제ID, 학생ID
            new_row_data = [
                payload.get('className'),
                payload.get('studentName'),
                payload.get('assignmentName'),
                payload.get('reason'),
                rejection_timestamp_str,
                payload.get('submissionId'),
                student_id  # G열에 학생ID 추가
            ]
            
            df = get_sheet_as_df(worksheet)
            if not df.empty and '과제ID' in df.columns and payload.get('submissionId') in df['과제ID'].values:
                existing_row_index = df[df['과제ID'] == payload.get('submissionId')].index[0] + 2
                worksheet.update(f'A{existing_row_index}:G{existing_row_index}', [new_row_data])
                message = "반려 정보가 업데이트되었습니다."
            else:
                worksheet.append_row(new_row_data, value_input_option='USER_ENTERED')
                message = "반려 정보가 저장되었습니다."

            # SMS 발송 로직
            student_info = roster_df[(roster_df['학생이름'] == payload.get('studentName')) & (roster_df['클래스'] == payload.get('className'))]
            if not student_info.empty:
                phone_number = str(student_info.iloc[0]['학생전화'])
                if phone_number:
                    sms_message = f"[김한이수학] {payload.get('assignmentName')}이(가) 반려되었습니다. ({payload.get('reason')})"
                    send_sms_aligo(phone_number, sms_message)

        header = source_worksheet.row_values(1)
        teacher_status_col = header.index('교사확인상태') + 1
        source_worksheet.update_cell(target_row_source, teacher_status_col, new_status)
        
        return jsonify({"success": True, "message": message})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500
    
@app.route('/api/get_student_level')
def get_student_level():
    if session.get('user_role') not in ['teacher', 'admin']:
        return jsonify({"error": "Unauthorized"}), 403

    student_name = request.args.get('student_name')
    class_name = request.args.get('class_name')
    
    if not student_name or not class_name:
        return jsonify({"error": "학생 이름과 클래스가 필요합니다."}), 400

    try:
        gc = authenticate_gsheets()
        roster_sheet = gc.open_by_key(STUDENT_DB_ID).worksheet(STUDENT_DB_WORKSHEET_NAME)
        roster_df = get_sheet_as_df(roster_sheet)
        
        # 학생 정보 찾기
        student_info = roster_df[(roster_df['학생이름'] == student_name) & (roster_df['클래스'] == class_name)]
        
        if student_info.empty:
            return jsonify({"error": f"{class_name}의 {student_name} 학생을 찾을 수 없습니다."}), 404
        
        # L열에서 레벨 가져오기 (L열은 12번째 컬럼, 0부터 시작하므로 인덱스 11)
        if len(roster_df.columns) > 11:
            student_level = student_info.iloc[0].iloc[11] if len(student_info.iloc[0]) > 11 else ""
        else:
            student_level = ""
        
        # 빈 값이나 NaN 처리
        if pd.isna(student_level) or str(student_level).strip() == '':
            student_level = ""
        else:
            student_level = str(student_level).strip()
        
        return jsonify({"level": student_level})

    except Exception as e:
        print(f"학생 레벨 조회 중 오류: {e}")
        return jsonify({"error": "레벨확인"}), 500

# Homework_Portal.py 파일의 채점 페이지 관련 API 영역에 추가하세요.
@app.route('/api/get_result_details')
def get_result_details():
    if session.get('user_role') not in ['teacher', 'admin']:
        return jsonify({"error": "Unauthorized"}), 403

    submission_id = request.args.get('id')
    if not submission_id:
        return jsonify({"error": "Submission ID is required"}), 400

    try:
        gc = authenticate_gsheets()
        target_sheet = gc.open_by_key(TARGET_SHEET_ID)
        worksheet = target_sheet.worksheet("과제제출현황")

        df = get_sheet_as_df(worksheet)

        if df.empty or '과제ID' not in df.columns:
             return jsonify({"error": "Grading data not found or sheet is malformed"}), 404

        result_row = df[df['과제ID'] == submission_id]

        if result_row.empty:
            return jsonify({"error": "해당 과제에 대한 채점 기록을 찾을 수 없습니다."}), 404

        # DataFrame의 첫 번째 행을 사전(dict)으로 변환
        details = result_row.iloc[0].to_dict()

        # 프론트엔드가 기대하는 데이터 형식으로 가공
        response_data = {
            "memo": details.get("메모확인", ""),
            "wrongProblemTexts": details.get("오답문항", "")
        }

        return jsonify(response_data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# --- 관리자 페이지 (admin.html) 관련 API ---
# ----------------------------------------------------------------
@app.route('/admin')
def admin_page():
    """관리자 페이지를 보여줍니다."""
    if session.get('user_role') != 'admin':
        return redirect(url_for('staff_login_page'))
    # 템플릿에 user_role 변수를 전달합니다.
    return render_template('admin.html', user_role=session.get('user_role'))

@app.route('/api/admin_dashboard')
def get_admin_dashboard_data():
    try:
        gc = authenticate_gsheets()
        student_db_sheet = gc.open_by_key(STUDENT_DB_ID).worksheet(STUDENT_DB_WORKSHEET_NAME)
        roster_df = get_sheet_as_df(student_db_sheet)
        roster_df = roster_df[roster_df['현재상태'] == '등록중'].copy()

        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        deadline_sheet = source_sheet.worksheet(DEADLINE_WORKSHEET_NAME)
        deadlines_df = get_sheet_as_df(deadline_sheet)
        submission_sheet = source_sheet.worksheet(SOURCE_WORKSHEET_NAME)
        submissions_df = get_sheet_as_df(submission_sheet)
        submissions_df = submissions_df[submissions_df['교사확인상태'] != '반려'].copy()

        today_start = datetime.combine(datetime.now().date(), time.min)
        current_year = datetime.now().year
        
        deadlines_df['기한_날짜'] = deadlines_df['제출기한'].str.extract(r'(\d{1,2}/\d{1,2})').iloc[:, 0]
        deadlines_df['제출마감_datetime'] = pd.to_datetime(f'{current_year}/' + deadlines_df['기한_날짜'], format='%Y/%m/%d', errors='coerce')
        past_due_assignments_df = deadlines_df[deadlines_df['제출마감_datetime'] < today_start].dropna(subset=['제출마감_datetime'])

        class_counts = roster_df['클래스'].value_counts().to_dict()
        class_summary_data = {cn: {'required': 0, 'completed': 0} for cn in class_counts.keys()}
        chart_data_by_assignment = {}
        student_performance = { row['학생이름']: {'on_time': 0, 'late': 0, 'missing': 0, 'class': row['클래스']} for index, row in roster_df.iterrows() if row.get('학생이름') }

        for index, assignment in past_due_assignments_df.iterrows():
            class_name = assignment['클래스']
            assignment_name = assignment['과제명']
            if class_name not in chart_data_by_assignment: chart_data_by_assignment[class_name] = []
            student_count = class_counts.get(class_name, 0)
            if student_count == 0: continue
            
            completed_students_count = submissions_df[(submissions_df['클래스를 선택해주세요.'] == class_name) & (submissions_df['과제 번호를 선택해주세요. (반드시 확인요망)'] == assignment_name)]['이름을 입력해주세요. (띄어쓰기 금지)'].nunique()
            class_summary_data[class_name]['required'] += student_count
            class_summary_data[class_name]['completed'] += completed_students_count
            
            submission_rate = (completed_students_count / student_count * 100) if student_count > 0 else 0
            chart_data_by_assignment[class_name].append({"assignment_name": assignment_name, "submission_rate": round(submission_rate, 1), "details": f"{completed_students_count} / {student_count}명"})

            for student_name, student_info in student_performance.items():
                if student_info['class'] == class_name:
                    student_submission = submissions_df[(submissions_df['이름을 입력해주세요. (띄어쓰기 금지)'] == student_name) & (submissions_df['과제 번호를 선택해주세요. (반드시 확인요망)'] == assignment_name)]
                    if not student_submission.empty:
                        status = student_submission.iloc[0].get('제출상태', '지각제출')
                        if '정상' in status: student_performance[student_name]['on_time'] += 1
                        else: student_performance[student_name]['late'] += 1
                    else: student_performance[student_name]['missing'] += 1
        
        summary_stats = {"total_required": sum(d['required'] for d in class_summary_data.values()), "total_completed": sum(d['completed'] for d in class_summary_data.values()), "total_missing": sum(d['required'] - d['completed'] for d in class_summary_data.values())}
        chart_overall_by_class = []
        for class_name, data in class_summary_data.items():
            rate = (data['completed'] / data['required'] * 100) if data['required'] > 0 else 0
            chart_overall_by_class.append({"class_name": class_name, "rate": round(rate, 1), "details": f"{data['completed']} / {data['required']}건"})
        
        ranked_students = sorted(student_performance.items(), key=lambda item: (item[1]['missing'], item[1]['late'], -item[1]['on_time']))
        
        grouped_ranks = []
        if ranked_students:
            current_stats = ranked_students[0][1]
            current_group = {"stats": current_stats, "names": [(ranked_students[0][0], current_stats['class'])]}
            for name, stats in ranked_students[1:]:
                if stats['missing'] == current_stats['missing'] and stats['late'] == current_stats['late'] and stats['on_time'] == current_stats['on_time']:
                    current_group["names"].append((name, stats['class']))
                else:
                    grouped_ranks.append(current_group)
                    current_stats = stats
                    current_group = {"stats": current_stats, "names": [(name, stats['class'])]}
            grouped_ranks.append(current_group)

        honor_rank = { "top10": grouped_ranks[:10], "bottom10": grouped_ranks[-10:][::-1] }
        dashboard_data = {
            "summary_stats": summary_stats,
            "charts_data": { "by_assignment": chart_data_by_assignment, "overall_by_class": chart_overall_by_class },
            "honor_rank": honor_rank
        }
        return jsonify(dashboard_data)
    except Exception as e:
        print(f"관리자 대시보드 데이터 생성 중 오류: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# --- 학생 개인 페이지 (student_page.html) 관련 API ---
# ----------------------------------------------------------------
@app.route('/login')
def login_page():
    # 세션을 확인하여 관리자일 경우, 학생 선택 페이지를 보여줌
    if session.get('user_role') == 'admin':
        return render_template('admin_student_lookup.html')
    
    # 그 외의 경우(로그인 안 했거나, 학생)는 기존 학생 로그인 페이지를 보여줌
    return render_template('login.html')

@app.route('/api/get_all_students')
def get_all_students():
    # 관리자만 이 API를 사용할 수 있도록 권한 확인
    if session.get('user_role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    gc = authenticate_gsheets()
    roster_sheet = gc.open_by_key(STUDENT_DB_ID).worksheet(STUDENT_DB_WORKSHEET_NAME)
    roster_df = get_sheet_as_df(roster_sheet)
    
    # '등록중'인 학생만 필터링
    active_students = roster_df[roster_df['현재상태'] == '등록중']
    
    # 클래스별로 학생 이름 그룹핑
    students_by_class = active_students.groupby('클래스')['학생이름'].apply(list).to_dict()
    
    return jsonify(students_by_class)

@app.route('/api/admin_view_student', methods=['POST'])
def admin_view_student():
    if session.get('user_role') != 'admin':
        return jsonify({"success": False, "message": "권한이 없습니다."}), 403

    data = request.json
    student_name = data.get('name')

    # 학생 페이지가 해당 학생의 정보를 로드할 수 있도록 세션 설정
    session['student_name'] = student_name
    return jsonify({"success": True})

@app.route('/api/login', methods=['POST'])
def handle_login():
    data = request.json
    try:
        gc = authenticate_gsheets()
        student_db_sheet = gc.open_by_key(STUDENT_DB_ID).worksheet(STUDENT_DB_WORKSHEET_NAME)
        roster_df = get_sheet_as_df(student_db_sheet).astype(str)
        match = roster_df[(roster_df['학생이름'] == data.get('name')) & (roster_df['학생전화'] == data.get('student_phone')) & (roster_df['학부모전화'] == data.get('parent_phone'))]
        if not match.empty:
            session['student_name'] = data.get('name')
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "message": "입력한 정보가 올바르지 않습니다."}), 401
    except Exception as e:
        print(f"로그인 처리 중 오류: {e}")
        return jsonify({"success": False, "message": "서버 오류 발생"}), 500

@app.route('/my_page')
def student_my_page():
    if 'student_name' not in session:
        return redirect(url_for('login_page'))
    return render_template('student_page.html', student_name=session.get('student_name'))

@app.route('/logout')
def logout():
    session.pop('student_name', None)
    return redirect(url_for('login_page'))

@app.route('/api/my_page_data')
def get_my_page_data():
    if 'student_name' not in session:
        return jsonify({"error": "Not logged in"}), 401
    student_name = session['student_name']
    
    try:
        gc = authenticate_gsheets()
        
        student_db_spreadsheet = gc.open_by_key(STUDENT_DB_ID)
        roster_sheet = student_db_spreadsheet.worksheet(STUDENT_DB_WORKSHEET_NAME)
        roster_df = get_sheet_as_df(roster_sheet)
        
        student_info_series = roster_df[roster_df['학생이름'] == student_name]
        if student_info_series.empty: return jsonify({"error": "Student not found"}), 404
        class_name = student_info_series.iloc[0]['클래스']
        today = datetime.now().date()
        current_year = today.year

        attendance_book_sheet = student_db_spreadsheet.worksheet(f"출석부-{class_name}")
        official_dates = [val for val in attendance_book_sheet.col_values(1) if val != '날짜' and val != ''][1:]
        past_official_dates = []
        for d in official_dates:
            try:
                if d and datetime.strptime(d, "%Y-%m-%d").date() <= today:
                    past_official_dates.append(d)
            except ValueError:
                print(f"경고: '출석부-{class_name}' 시트에서 잘못된 날짜 형식 발견 - '{d}'")
                continue

        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        deadline_sheet = source_sheet.worksheet(DEADLINE_WORKSHEET_NAME)
        deadlines_df = get_sheet_as_df(deadline_sheet)
        deadlines_df['기한_날짜'] = deadlines_df['제출기한'].astype(str).str.extract(r'(\d{1,2}/\d{1,2})')
        deadlines_df.dropna(subset=['기한_날짜'], inplace=True)
        deadlines_df['제출마감_datetime'] = pd.to_datetime(str(current_year) + '/' + deadlines_df['기한_날짜'], format='%Y/%m/%d', errors='coerce')
        past_due_assignments_df = deadlines_df[(deadlines_df['클래스'] == class_name) & (deadlines_df['제출마감_datetime'].dt.date < today)]

        record_spreadsheet = gc.open_by_key(TARGET_SHEET_ID)
        attendance_sheet = record_spreadsheet.worksheet("출결")
        attendance_df = get_sheet_as_df(attendance_sheet)
        student_attendance_df = attendance_df[attendance_df['이름'] == student_name]
        
        clinic_sheet = record_spreadsheet.worksheet("클리닉")
        clinic_df = get_sheet_as_df(clinic_sheet)
        student_clinic_df = clinic_df[clinic_df['학생이름'] == student_name].copy()

        submission_sheet = source_sheet.worksheet(SOURCE_WORKSHEET_NAME)
        submissions_df = get_sheet_as_df(submission_sheet)
        student_submissions_df = submissions_df[submissions_df['이름을 입력해주세요. (띄어쓰기 금지)'] == student_name].copy()
        
        # --- 데이터 가공 및 요약 통계 계산 ---
        attendance_records = {row['날짜']: row['출결'] for index, row in student_attendance_df.iterrows()}
        final_attendance = [{"date": date_str, "status": attendance_records.get(date_str, "결석")} for date_str in past_official_dates]
        attendance_summary = {k: int(v) for k, v in pd.Series([item['status'] for item in final_attendance]).value_counts().to_dict().items()}
        attendance_summary['총일수'] = len(past_official_dates)

        student_clinic_df['datetime'] = pd.to_datetime(student_clinic_df['날짜'], format='%Y-%m-%d', errors='coerce')
        past_clinic_df = student_clinic_df[student_clinic_df['datetime'].dt.date <= today]
        clinic_records = past_clinic_df.sort_values(by='datetime', ascending=False).to_dict('records')
        clinic_summary = {k: int(v) for k, v in past_clinic_df['출결'].value_counts().to_dict().items()}
        clinic_summary['총클리닉'] = len(past_clinic_df)
        
        student_submissions_df.loc[:, 'Submitted at KST'] = pd.to_datetime(student_submissions_df['Submitted at'], errors='coerce') + pd.Timedelta(hours=9)
        
        # FIX: "반려" 로직 수정. 반려된 과제를 분리하고 계산에서 제외
        rejected_submissions_df = student_submissions_df[student_submissions_df['교사확인상태'] == '반려']
        non_rejected_submissions_df = student_submissions_df[student_submissions_df['교사확인상태'] != '반려']

        assignment_records = non_rejected_submissions_df.sort_values(by='Submitted at KST', ascending=False).to_dict('records')
        rejected_assignment_records = rejected_submissions_df.sort_values(by='Submitted at KST', ascending=False).to_dict('records')
        
        submitted_assignments = non_rejected_submissions_df['과제 번호를 선택해주세요. (반드시 확인요망)'].unique()
        unsubmitted_assignments = past_due_assignments_df[~past_due_assignments_df['과제명'].isin(submitted_assignments)]
        unsubmitted_list = [{"과제명": name, "제출상태": "미제출", "제출일시": deadline} for name, deadline in zip(unsubmitted_assignments['과제명'], unsubmitted_assignments['제출기한'])]
        
        assignment_summary = {k: int(v) for k, v in non_rejected_submissions_df['제출상태'].value_counts().to_dict().items()}
        assignment_summary['미제출'] = len(unsubmitted_assignments)

        page_data = {
            "student_info": student_info_series.iloc[0].to_dict(),
            "attendance": {"summary": attendance_summary, "details": sorted(final_attendance, key=lambda x: x['date'], reverse=True)},
            "assignments": {
                "summary": assignment_summary, 
                "details": assignment_records, 
                "unsubmitted": unsubmitted_list,
                "rejected": rejected_assignment_records # 반려 목록 추가
            },
            "clinic": {"summary": clinic_summary, "details": clinic_records},
        }
        return jsonify(page_data)

    except Exception as e:
        import traceback
        print(f"개인 페이지 데이터 생성 중 오류: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# --- 교직원 로그인/로그아웃 API ---
# ----------------------------------------------------------------
@app.route('/staff_login')
def staff_login_page():
    """교직원용 로그인 페이지를 보여줍니다."""
    return render_template('staff_login.html')

@app.route('/api/staff_login', methods=['POST'])
def handle_staff_login():
    data = request.json
    user_id = data.get('id')
    password = data.get('password')

    user_info = STAFF_CREDENTIALS.get(user_id) # [비밀번호, 역할] 리스트를 가져옴

    # ID가 존재하고 비밀번호가 일치하는지 확인
    if user_info and user_info[0] == password:
        session['user_role'] = user_info[1] # ID 대신 '역할'을 세션에 저장
        
        redirect_url = '/admin' if session['user_role'] == 'admin' else '/grader'
        return jsonify({"success": True, "redirect_url": redirect_url})
    else:
        return jsonify({"success": False, "message": "ID 또는 비밀번호가 올바르지 않습니다."}), 401

@app.route('/staff_logout')
def staff_logout():
    session.pop('user_id', None)
    return redirect(url_for('staff_login_page'))


@app.route('/sync')
def sync_graded_data():
    if session.get('user_role') != 'admin':
        return "권한이 없습니다.", 403

    try:
        gc = authenticate_gsheets()
        source_worksheet = gc.open_by_url(SOURCE_SHEET_URL).worksheet(SOURCE_WORKSHEET_NAME)
        submissions_df = get_sheet_as_df(source_worksheet)
        
        target_sheet = gc.open_by_key(TARGET_SHEET_ID)
        graded_worksheet = target_sheet.worksheet("과제제출현황")
        graded_df = get_sheet_as_df(graded_worksheet)
        rejected_worksheet = target_sheet.worksheet("과제반려현황")
        rejected_df = get_sheet_as_df(rejected_worksheet)

        # ID 컬럼 존재 여부 확인
        if 'Submission ID' not in submissions_df.columns or '과제ID' not in graded_df.columns or '과제ID' not in rejected_df.columns:
            return "<h1>오류: ID 컬럼('Submission ID' 또는 '과제ID')을 찾을 수 없습니다. 시트 헤더를 확인해주세요.</h1>", 500

        # 데이터 정제: ID를 모두 문자열로 변환하고, 공백 제거, 소문자로 통일하여 비교 정확도 향상
        submissions_df['Clean ID'] = submissions_df['Submission ID'].astype(str).str.strip().str.lower()
        graded_df['Clean ID'] = graded_df['과제ID'].astype(str).str.strip().str.lower()
        rejected_df['Clean ID'] = rejected_df['과제ID'].astype(str).str.strip().str.lower()
        
        existing_submission_ids = set(submissions_df['Clean ID'])
        header_tally = source_worksheet.row_values(1)
        new_rows_to_add = []

        if not graded_df.empty:
            missing_graded_df = graded_df[~graded_df['Clean ID'].isin(existing_submission_ids)]
            for index, row in missing_graded_df.iterrows():
                submitted_at = row.get('시간')
                new_row = {h: '' for h in header_tally}
                new_row['Submission ID'] = row.get('과제ID')
                new_row['Submitted at'] = submitted_at
                new_row['이름을 입력해주세요. (띄어쓰기 금지)'] = row.get('이름')
                new_row['클래스를 선택해주세요.'] = row.get('클래스')
                new_row['과제 번호를 선택해주세요. (반드시 확인요망)'] = row.get('과제명')
                new_row['제출상태'] = row.get('제출상태')
                new_row['교사확인상태'] = '확인완료'
                new_rows_to_add.append([new_row.get(h, '') for h in header_tally])

        if not rejected_df.empty:
            missing_rejected_df = rejected_df[~rejected_df['Clean ID'].isin(existing_submission_ids)]
            for index, row in missing_rejected_df.iterrows():
                submitted_at = row.get('반려시간')
                new_row = {h: '' for h in header_tally}
                new_row['Submission ID'] = row.get('과제ID')
                new_row['Submitted at'] = submitted_at
                new_row['이름을 입력해주세요. (띄어쓰기 금지)'] = row.get('이름')
                new_row['클래스를 선택해주세요.'] = row.get('클래스')
                new_row['과제 번호를 선택해주세요. (반드시 확인요망)'] = row.get('과제명')
                new_row['제출상태'] = ''
                new_row['교사확인상태'] = '반려'
                new_rows_to_add.append([new_row.get(h, '') for h in header_tally])

        if not new_rows_to_add:
            return "<h1>동기화 완료: 누락된 데이터가 없습니다.</h1>", 200
        
        source_worksheet.append_rows(new_rows_to_add, value_input_option='USER_ENTERED')
        return f"<h1>동기화 완료: 총 {len(new_rows_to_add)}개의 누락된 데이터(확인완료, 반려 포함)를 (탈리)과제제출 시트에 추가했습니다.</h1>", 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"동기화 중 오류 발생: {e}", 500


@app.route('/debug_sync')
def debug_sync_data():
    if session.get('user_role') != 'admin':
        return "권한이 없습니다.", 403

    try:
        gc = authenticate_gsheets()
        
        # 1. 각 시트 데이터 불러오기
        source_worksheet = gc.open_by_url(SOURCE_SHEET_URL).worksheet(SOURCE_WORKSHEET_NAME)
        submissions_df = get_sheet_as_df(source_worksheet)
        
        target_sheet = gc.open_by_key(TARGET_SHEET_ID)
        graded_worksheet = target_sheet.worksheet("과제제출현황")
        graded_df = get_sheet_as_df(graded_worksheet)
        rejected_worksheet = target_sheet.worksheet("과제반려현황")
        rejected_df = get_sheet_as_df(rejected_worksheet)

        # 2. ID 데이터 정제 (공백 제거, 소문자 변환)
        tally_ids = set(submissions_df['Submission ID'].astype(str).str.strip().str.lower())
        graded_ids = set(graded_df['과제ID'].astype(str).str.strip().str.lower())
        rejected_ids = set(rejected_df['과제ID'].astype(str).str.strip().str.lower())

        # 3. 누락된 ID 찾기
        missing_from_graded = graded_ids - tally_ids
        missing_from_rejected = rejected_ids - tally_ids

        # 4. 결과 출력
        output = "<h1>동기화 데이터 진단 결과</h1>"
        output += f"<p><b>(탈리)과제제출 시트 ID 개수:</b> {len(tally_ids)}개</p>"
        output += f"<p><b>과제제출현황 시트 ID 개수:</b> {len(graded_ids)}개</p>"
        output += f"<p><b>과제반려현황 시트 ID 개수:</b> {len(rejected_ids)}개</p>"
        output += "<hr>"
        output += f"<h2>(탈리)과제제출 시트에 누락된 ID 목록 (과제제출현황 기준):</h2>"
        if missing_from_graded:
            output += "<ul>" + "".join(f"<li>{id}</li>" for id in missing_from_graded) + "</ul>"
        else:
            output += "<p>없음</p>"
        
        output += f"<h2>(탈리)과제제출 시트에 누락된 ID 목록 (과제반려현황 기준):</h2>"
        if missing_from_rejected:
            output += "<ul>" + "".join(f"<li>{id}</li>" for id in missing_from_rejected) + "</ul>"
        else:
            output += "<p>없음</p>"
            
        return output

    except Exception as e:
        return f"진단 중 오류 발생: {e}", 500

# ----------------------------------------------------------------
# --- 백그라운드 작업 시작 (Gunicorn이 인식하도록 전역 범위에 위치) ---
# ----------------------------------------------------------------
worker_thread = threading.Thread(target=background_worker_task, daemon=True)
worker_thread.start()
print("Background worker thread started.")

# ----------------------------------------------------------------
# --- 서버 실행 (로컬 테스트 전용) ---
# ----------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)