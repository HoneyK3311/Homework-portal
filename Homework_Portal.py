# Homework_Portal.py
# Render 배포용 최종 버전입니다.
# '확인완료'된 과제를 수정하는 기능이 추가되었습니다.

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from datetime import datetime
import os
import json
import requests
import time
import threading

from flask import Flask, jsonify, render_template, request

# --- Flask 앱 초기화 ---
app = Flask(__name__, template_folder='templates')

# --- 설정 ---
SOURCE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1myGZWyghHzEhziGpOzhpqvWxotyvfaGxmF4ddgFAeOc/edit?usp=sharing"
SOURCE_WORKSHEET_NAME = "(탈리)과제제출"
STUDENT_DB_SHEET_ID = "1Od9PfHV39MSfwfUgWtPun0Y9zCqAdURc-iwd2n0rgBI"
TARGET_SHEET_ID = "1VROqIZ2GmAlQSdw8kZyd_rC6oP_nqTsuVEnWIi0rS24"
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

# --- 알리고(Aligo) API 설정 (Render 환경 변수에서 읽어옴) ---
ALIGO_API_KEY = os.environ.get("ALIGO_API_KEY")
ALIGO_USER_ID = os.environ.get("ALIGO_USER_ID")
SENDER_PHONE_NUMBER = os.environ.get("SENDER_PHONE_NUMBER")

# --- 핵심 기능 함수 ---

def authenticate_gsheets():
    """Render 환경 변수에서 인증 정보를 읽어옵니다."""
    try:
        creds_json_str = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        if not creds_json_str:
            # 로컬 테스트용 fallback
            print("⚠️ GOOGLE_CREDENTIALS_JSON 환경 변수를 찾을 수 없습니다. 로컬 파일로 인증을 시도합니다.")
            creds = Credentials.from_service_account_file('sheets_service.json', scopes=SCOPES)
        else:
            creds_info = json.loads(creds_json_str)
            creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
            print("✅ Render 환경 변수에서 인증 성공")
        return gspread.authorize(creds)
    except Exception as e:
        print(f"❌ 구글 시트 인증 중 오류 발생: {e}")
        return None

def get_sheet_as_df(worksheet):
    """시트 데이터를 DataFrame으로 안전하게 변환합니다."""
    all_values = worksheet.get_all_values()
    if not all_values: return pd.DataFrame()
    headers = all_values[0]
    data = all_values[1:]
    non_empty_headers = [h for h in headers if h]
    num_cols = len(non_empty_headers)
    filtered_data = []
    for row in data:
        padded_row = row + [''] * (num_cols - len(row))
        filtered_data.append(padded_row[:num_cols])
    return pd.DataFrame(filtered_data, columns=non_empty_headers)

def send_sms_aligo(phone_number, message):
    """알리고 API를 사용하여 SMS를 발송합니다."""
    if not all([ALIGO_API_KEY, ALIGO_USER_ID, SENDER_PHONE_NUMBER]) or "여기에" in ALIGO_API_KEY:
        print("⚠️ 알리고 API 환경 변수가 설정되지 않아 발송을 건너뜁니다.")
        return

    try:
        url = "https://apis.aligo.in/send/"
        payload = { 'key': ALIGO_API_KEY, 'user_id': ALIGO_USER_ID, 'sender': SENDER_PHONE_NUMBER, 'receiver': phone_number, 'msg': message, 'msg_type': 'SMS' }
        response = requests.post(url, data=payload)
        result = response.json()
        if result.get("result_code") == "1":
            print(f"✅ SMS 발송 성공! -> 받는사람: {phone_number}")
        else:
            print(f"🚨 SMS 발송 실패: {result.get('message', '알 수 없는 오류')}")
    except Exception as e:
        print(f"🚨 SMS 발송 중 예외 발생: {e}")

def run_worker():
    """새로운 과제를 찾아 상태를 업데이트하고 SMS를 발송하는 메인 함수"""
    print("⚙️  백그라운드 작업기 실행: 새로운 과제를 확인합니다...")
    gc = authenticate_gsheets()
    if not gc: return print("🚨 [Worker] 인증 실패.")

    try:
        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        submission_worksheet = source_sheet.worksheet(SOURCE_WORKSHEET_NAME)
        deadline_worksheet = source_sheet.worksheet("제출기한")
        student_db_sheet = gc.open_by_key(STUDENT_DB_SHEET_ID)
        roster_worksheet = student_db_sheet.worksheet("(통합) 학생DB")
        
        submissions_df = get_sheet_as_df(submission_worksheet)
        deadlines_df = get_sheet_as_df(deadline_worksheet)
        roster_df = get_sheet_as_df(roster_worksheet)

        if submissions_df.empty: return print("✅ [Worker] 처리할 과제가 없습니다.")
        unprocessed_submissions = submissions_df[submissions_df['제출상태'] == ''].copy()
        if unprocessed_submissions.empty: return print("✅ [Worker] 새로운 과제가 없습니다.")

        print(f"✨ [Worker] {len(unprocessed_submissions)}개의 새로운 과제를 발견했습니다. 처리 시작...")
        
        deadlines_df['제출기한_날짜'] = deadlines_df['제출기한'].str.extract(r'(\d{1,2}/\d{1,2})')
        current_year = datetime.now().year
        deadlines_df['제출마감_datetime'] = pd.to_datetime(
            f'{current_year}/' + deadlines_df['제출기한_날짜'], format='%Y/%m/%d', errors='coerce'
        ) + pd.to_timedelta('23 hours 59 minutes 59 seconds')

        for index, row in unprocessed_submissions.iterrows():
            row_index_in_sheet = index + 2
            submitted_at_kst = pd.to_datetime(row['Submitted at'], errors='coerce') + pd.Timedelta(hours=9)
            student_name = row['이름을 입력해주세요. (띄어쓰기 금지)']
            student_class = row['클래스를 선택해주세요.']
            assignment_name = row['과제 번호를 선택해주세요. (반드시 확인요망)']

            deadline_info = deadlines_df[(deadlines_df['클래스'] == student_class) & (deadlines_df['과제명'] == assignment_name)]
            status = "정상제출" if not deadline_info.empty and submitted_at_kst <= deadline_info.iloc[0]['제출마감_datetime'] else "지각제출"
            
            header = submission_worksheet.row_values(1)
            submission_status_col = header.index('제출상태') + 1
            teacher_status_col = header.index('교사확인상태') + 1
            submission_worksheet.update_cell(row_index_in_sheet, submission_status_col, status)
            submission_worksheet.update_cell(row_index_in_sheet, teacher_status_col, '미확인')
            print(f"  - {row_index_in_sheet}행: '{status}' / '미확인' (으)로 업데이트 완료")

            student_info = roster_df[(roster_df['학생이름'] == student_name) & (roster_df['클래스'] == student_class)]
            if not student_info.empty:
                phone_number = str(student_info.iloc[0]['학생전화'])
                if phone_number:
                    message = f"[김한이수학] {assignment_name} 제출 완료!"
                    send_sms_aligo(phone_number, message)
            else:
                print(f"⚠️ {student_class}의 {student_name} 학생을 학생DB에서 찾을 수 없습니다.")
        print("✅ [Worker] 모든 새로운 과제 처리를 완료했습니다.")
    except Exception as e:
        print(f"🚨 [Worker] 작업 중 오류 발생: {e}")

def background_worker_task():
    """백그라운드에서 run_worker 함수를 주기적으로 실행하는 함수"""
    while True:
        run_worker()
        time.sleep(30)

# --- Flask API 엔드포인트 ---
@app.route('/api/data')
def get_data():
    gc = authenticate_gsheets()
    if not gc: return jsonify({"error": "Google Sheets 인증 실패"}), 500
    
    source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
    submissions_df = get_sheet_as_df(source_sheet.worksheet(SOURCE_WORKSHEET_NAME))
    assignments_df = get_sheet_as_df(source_sheet.worksheet("과제목록"))
    
    submissions_df['Submitted at'] = pd.to_datetime(submissions_df['Submitted at'], errors='coerce')
    submissions_df['제출일시_KST'] = submissions_df['Submitted at'].dropna() + pd.Timedelta(hours=9)
    submissions_df['제출일시_KST_str'] = submissions_df['제출일시_KST'].apply(
        lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if pd.notna(x) else '-'
    )
    
    submissions_for_json = submissions_df.drop(columns=['Submitted at', '제출일시_KST'], errors='ignore')

    result = {
        "submissions": submissions_for_json.to_dict(orient='records'),
        "assignments": assignments_df.to_dict(orient='records'),
    }
    return jsonify(result)

@app.route('/api/get_result_details')
def get_result_details():
    submission_id = request.args.get('id')
    print(f"🔍 '{submission_id}'에 대한 채점 기록 조회 시작...")
    if not submission_id:
        return jsonify({"error": "Submission ID가 필요합니다."}), 400
    
    gc = authenticate_gsheets()
    if not gc: return jsonify({"error": "Google Sheets 인증 실패"}), 500

    try:
        target_sheet = gc.open_by_key(TARGET_SHEET_ID)
        worksheet = target_sheet.worksheet("과제제출현황")
        
        cell = worksheet.find(submission_id, in_column=10)
        if not cell:
            print(f"⚠️ '{submission_id}'에 대한 채점 기록을 찾을 수 없음.")
            return jsonify({"error": "채점 기록을 찾을 수 없습니다."}), 404
        
        print(f"✅ '{submission_id}' 기록 발견: {cell.row}행")
        row_data = worksheet.row_values(cell.row)
        result = { "wrongProblemTexts": row_data[6], "memo": row_data[7] }
        return jsonify(result)
    except Exception as e:
        print(f"❌ 채점 기록 조회 중 오류: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/update_status', methods=['POST'])
def update_status():
    data = request.json
    action = data.get('action')
    payload = data.get('payload')
    try:
        gc = authenticate_gsheets()
        if not gc: return jsonify({"error": "Google Sheets 인증 실패"}), 500
        
        source_sheet = gc.open_by_url(SOURCE_SHEET_URL)
        source_worksheet = source_sheet.worksheet(SOURCE_WORKSHEET_NAME)
        cell_source = source_worksheet.find(payload.get('submissionId'))
        if not cell_source: return jsonify({"success": False, "message": "원본 시트에서 해당 과제를 찾을 수 없습니다."}), 404
        
        target_row_source = cell_source.row
        new_status = "확인완료" if action == 'confirm' else "반려"
        
        target_sheet = gc.open_by_key(TARGET_SHEET_ID)
        
        if action == 'confirm':
            worksheet = target_sheet.worksheet("과제제출현황")
            cell_target = worksheet.find(payload.get('submissionId'), in_column=10)
            
            new_row_data = [
                payload.get('className'), payload.get('studentName'), payload.get('assignmentName'),
                payload.get('submissionStatus'), payload.get('totalProblems'), payload.get('wrongProblemCount'),
                ", ".join(payload.get('wrongProblemTexts', [])),
                payload.get('memo'), datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                payload.get('submissionId')
            ]

            if cell_target:
                worksheet.update(f'A{cell_target.row}:J{cell_target.row}', [new_row_data])
                message = "채점 결과가 수정되었습니다."
            else:
                worksheet.append_row(new_row_data)
                message = "채점 결과가 저장되었습니다."

        elif action == 'reject':
            confirm_worksheet = target_sheet.worksheet("과제제출현황")
            cell_to_delete = confirm_worksheet.find(payload.get('submissionId'), in_column=10)
            if cell_to_delete:
                confirm_worksheet.delete_rows(cell_to_delete.row)

            reject_worksheet = target_sheet.worksheet("과제반려현황")
            new_row = [
                payload.get('className'), payload.get('studentName'), payload.get('assignmentName'),
                payload.get('reason'), datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                payload.get('submissionId')
            ]
            reject_worksheet.append_row(new_row)
            message = "반려 정보가 저장되었습니다."

            student_db_sheet = gc.open_by_key(STUDENT_DB_SHEET_ID)
            roster_worksheet = student_db_sheet.worksheet("(통합) 학생DB")
            roster_df = get_sheet_as_df(roster_worksheet)
            student_info = roster_df[(roster_df['학생이름'] == payload.get('studentName')) & (roster_df['클래스'] == payload.get('className'))]
            if not student_info.empty:
                phone_number = str(student_info.iloc[0]['학생전화'])
                if phone_number:
                    sms_message = f"[김한이수학] {payload.get('assignmentName')}이(가) 반려됨ㅠ ({payload.get('reason')})"
                    send_sms_aligo(phone_number, sms_message)
        
        source_worksheet.update_cell(target_row_source, 9, new_status)

        return jsonify({"success": True, "message": message})
    except Exception as e:
        print(f"❌ 상태 업데이트 중 오류: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/')
def index():
    return render_template('index.html')

# --- 서버 실행 및 백그라운드 작업 시작 ---
worker_thread_started = False
@app.before_request
def start_worker_thread():
    """첫 번째 요청이 들어왔을 때 백그라운드 작업을 딱 한 번만 시작합니다."""
    global worker_thread_started
    if not worker_thread_started:
        worker_thread = threading.Thread(target=background_worker_task, daemon=True)
        worker_thread.start()
        worker_thread_started = True
        print("✅ 첫 요청 감지: 백그라운드 작업 스레드를 시작합니다.")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
