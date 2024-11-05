from fastapi import FastAPI, HTTPException, Form, BackgroundTasks, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import httpx
import json
import time
import uuid
import os
import logging
from datetime import datetime

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 배치 상태 및 로그 저장 디렉토리
BATCH_STATUS_DIR = "batch_status"
BATCH_LOG_DIR = "batch_logs"
os.makedirs(BATCH_STATUS_DIR, exist_ok=True)
os.makedirs(BATCH_LOG_DIR, exist_ok=True)

# 로깅 설정
logging.basicConfig(filename=os.path.join(BATCH_LOG_DIR, 'batch_error.log'), level=logging.INFO)
error_logger = logging.getLogger("batch_error")

# DB 설정 로드
CONFIG_PATH = "config/config.json"

def load_db_config():
    with open(CONFIG_PATH, "r") as config_file:
        config = json.load(config_file)
    return config.get("databases", {})

db_configs = load_db_config()

# DB 연결 관리
def get_db_connection(selected_db):
    config = db_configs.get(selected_db)
    if not config:
        raise HTTPException(status_code=404, detail=f"{selected_db} 설정이 없습니다.")
    try:
        engine = create_engine(config["uri"])
        connection = engine.connect()
        return connection
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"DB 연결 실패: {str(e)}")

# 배치 상태 파일 생성 함수
def create_batch_status(batch_id, total_rows):
    batch_status = {
        "batch_id": batch_id,
        "total_rows": total_rows,
        "processed_rows": 0,
        "status": "in_progress",
        "start_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "last_update_ts": time.strftime('%Y-%m-%d %H:%M:%S')
    }
    with open(f"{BATCH_STATUS_DIR}/{batch_id}.json", "w") as f:
        json.dump(batch_status, f)
    logging.info(f"Batch {batch_id} started at {batch_status['start_time']} with {total_rows} total rows.")

# 배치 상태 업데이트 함수
def update_batch_status(batch_id, processed_rows, status="in_progress"):
    batch_file = f"{BATCH_STATUS_DIR}/{batch_id}.json"
    if not os.path.exists(batch_file):
        raise HTTPException(status_code=404, detail="배치 ID가 존재하지 않습니다.")

    with open(batch_file, "r") as f:
        batch_status = json.load(f)

    batch_status["processed_rows"] = processed_rows
    batch_status["status"] = status
    batch_status["last_update_ts"] = time.strftime('%Y-%m-%d %H:%M:%S')

    with open(batch_file, "w") as f:
        json.dump(batch_status, f)

# 배치 상태 조회 함수
@app.get("/batch-status/{batch_id}")
async def get_batch_status(batch_id: str):
    batch_file = f"{BATCH_STATUS_DIR}/{batch_id}.json"
    if not os.path.exists(batch_file):
        raise HTTPException(status_code=404, detail="배치 ID가 존재하지 않습니다.")

    with open(batch_file, "r") as f:
        batch_status = json.load(f)
    # 오류 상태일 경우, 로그 경로 추가
    if batch_status.get("status") == "failed":
        batch_status["error_log_path"] = os.path.join(BATCH_LOG_DIR, 'batch_error.log')
    return batch_status

# 오류 로그 읽기 함수
@app.get("/error-log")
async def read_error_log():
    log_path = os.path.join(BATCH_LOG_DIR, 'batch_error.log')
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="오류 로그 파일이 존재하지 않습니다.")

    with open(log_path, "r") as f:
        logs = f.readlines()
    return {"logs": logs}

# 홈 경로
@app.get("/", response_class=HTMLResponse)
async def main_page(request: Request):
    db_options = list(db_configs.keys())
    return templates.TemplateResponse("index.html", {"request": request, "db_options": db_options})

# DB 연결 테스트
@app.post("/test-connection")
async def test_connection(selected_db: str = Form(...)):
    try:
        conn = get_db_connection(selected_db)
        conn.close()
        return JSONResponse(content={"message": "DB 연결 성공"}, status_code=200)
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"DB 연결 실패: {str(e)}")

# 샘플 데이터 쿼리
@app.post("/query-sample")
async def query_sample(selected_db: str = Form(...), query: str = Form(...)):
    try:
        conn = get_db_connection(selected_db)
        result = conn.execute(text(query)).fetchmany(10)
        conn.close()

        if result:
            sample_data = [row[0] for row in result]
            return {"sample_data": sample_data}
        else:
            raise HTTPException(status_code=404, detail="데이터 없음")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"쿼리 실패: {str(e)}")

# CryptoHub API 호출
@app.post("/convert-sample")
async def convert_sample(sample_data: list):
    url = "https://i-dev-cryptohub.apddev.com/v1/convertDamo2CrytoData_list/awsuser11"
    headers = {
        "Authorization": "Bearer eyJraWQiOiJ3SnNTVzBHMk1GU2hPcGhLdVwvVmt6ZHNEUmI4Z3RPYVBMc21vQ2tlZzRkcz0iLCJhbGciOiJSUzI1NiJ9..."
    }
    payload = {
        "instId": "server1",
        "ScpDbAgentApiVo": [{"inStr": data} for data in sample_data]
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

# 배치 작업 시작
@app.post("/batch-task")
async def start_batch_task(background_tasks: BackgroundTasks, selected_db: str = Form(...), query: str = Form(...)):
    batch_id = str(uuid.uuid4())
    conn = get_db_connection(selected_db)
    total_rows = conn.execute(text(f"SELECT COUNT(*) FROM ({query}) AS total_rows")).scalar()
    conn.close()

    create_batch_status(batch_id, total_rows)
    background_tasks.add_task(process_batch, selected_db, query, batch_id)

    return JSONResponse(content={"message": "배치 작업이 시작되었습니다.", "batch_id": batch_id})

# 백그라운드 배치 작업 실행
async def process_batch(selected_db, query, batch_id):
    conn = get_db_connection(selected_db)
    batch_size = 100
    offset = 0
    processed_rows = 0

    try:
        while True:
            result = conn.execute(text(f"{query} LIMIT {batch_size} OFFSET {offset}")).fetchall()
            if not result:
                break

            data_batch = [row[0] for row in result]
            response = await convert_sample(data_batch)
            results = response["ScpDbAgentApiVo"]

            for i, row in enumerate(result):
                result_data = results[i]
                conn.execute(
                    text("UPDATE migration_results SET datacryto = :outStr, rsltCd = :rsltCd, rsltMsg = :rsltMsg WHERE id = :id"),
                    {"outStr": result_data["outStr"], "rsltCd": result_data["rsltCd"], "rsltMsg": result_data["rsltMsg"], "id": row["id"]}
                )

            conn.commit()
            processed_rows += len(result)
            offset += batch_size
            update_batch_status(batch_id, processed_rows)

            # 로그에 진행 상태를 기록
            if processed_rows % batch_size == 0:
                logging.info(f"Batch {batch_id}: Processed {processed_rows}/{offset + len(result)} rows.")

        update_batch_status(batch_id, processed_rows, 'completed')
        logging.info(f"Batch {batch_id} completed successfully with {processed_rows} rows processed.")
    except Exception as e:
        # 오류 발생 시 상태를 'failed'로 업데이트하고 오류 로그 기록
        update_batch_status(batch_id, processed_rows, 'failed')
        error_logger.error(f"Batch ID {batch_id} 실패: {str(e)}")
    finally:
        conn.close()
