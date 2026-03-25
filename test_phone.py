import os
from sqlalchemy import create_engine, text

# DB 설정
DB_URI = os.getenv("DATABASE_URL", "postgresql://student_db_cgwz_user:hj1xqwey7VPWyaWLBO25ifbhr2y14rGl@dpg-d3ch9fili9vc73diitj0-a.oregon-postgres.render.com/student_db_cgwz")
engine = create_engine(DB_URI)

def check_strict_students():
    print("🔍 [정밀 진단] '2603' 시즌 + '등록중' + '33정규' 학생 추출\n")
    
    with engine.connect() as conn:
        # ✨ 핵심: 3가지 엄격한 조건을 모두 만족하는 학생만 쏙 뽑아내는 쿼리
        query = text('''
            SELECT * FROM students 
            WHERE "시즌" = '2603' 
              AND "현재상태" = '등록중' 
              AND "클래스" = '33정규'
        ''')
        result = conn.execute(query)
        
        columns = result.keys()
        count = 0
        
        for row in result:
            count += 1
            print(f"\n[타겟 학생 {count}]")
            for col_name, value in zip(columns, row):
                # 우리가 눈여겨봐야 할 핵심 컬럼들은 반짝이(✨) 표시
                if col_name in ['학생이름', '클래스', '현재상태', '시즌', '학생연락처']:
                    print(f"  ✨ {col_name} : '{value}'")
                else:
                    print(f"  - {col_name} : {value}")
                    
        if count == 0:
            print("  ⚠️ 조건에 맞는 학생이 DB에 단 한 명도 없습니다. (마이그레이션 오류 의심)")
        else:
            print(f"\n✅ 총 {count}명의 '진짜 타겟' 학생이 완벽하게 검색되었습니다!")

if __name__ == "__main__":
    check_strict_students()