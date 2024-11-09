from typing import List
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Form, BackgroundTasks, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Connection
from sqlalchemy.orm import sessionmaker, scoped_session
import httpx
import json
import time
import uuid
import os
import logging
from datetime import datetime
from starlette.staticfiles import StaticFiles

app = FastAPI()
app.mount("/images", StaticFiles(directory="images"), name="images")

templates = Jinja2Templates(directory="templates")

# 환경 변수 설정
IS_LOCAL = os.getenv("IS_LOCAL", "true") == "true"
CONFIG_PATH = "config/config.json"
BATCH_STATUS_DIR = "batch_status"
BATCH_LOG_DIR = "batch_logs"

# 로깅 설정
logging.basicConfig(level=logging.INFO)
os.makedirs(BATCH_STATUS_DIR, exist_ok=True)
os.makedirs(BATCH_LOG_DIR, exist_ok=True)

# 싱글톤 패턴의 DB 설정 및 연결 객체 관리 클래스
class DBConnectionManager:
    _configs = None
    _engines = {}
    _sessions = {}

    @classmethod
    def load_configs(cls):
        if cls._configs is None:
            with open(CONFIG_PATH, "r") as config_file:
                config = json.load(config_file)
                cls._configs = config.get("databases", {})
        return cls._configs

    @classmethod
    def get_session(cls, selected_db):
        if selected_db not in cls._sessions:
            config = cls.load_configs().get(selected_db)
            if not config:
                raise HTTPException(status_code=404, detail=f"{selected_db} 설정이 없습니다.")
            try:
                engine = create_engine(config["uri"])
                session_factory = sessionmaker(bind=engine)
                cls._engines[selected_db] = engine
                cls._sessions[selected_db] = scoped_session(session_factory)
            except SQLAlchemyError as e:
                raise HTTPException(status_code=500, detail=f"DB 연결 실패: {str(e)}")
        return cls._sessions[selected_db]

# 인스턴스를 미리 생성합니다.
DBConnectionManager.load_configs()

class BatchStorageHandler:
    def __init__(self):
        logging.basicConfig(filename=os.path.join(BATCH_LOG_DIR, 'batch_error.log'), level=logging.INFO)

    def save_status(self, batch_id, status_data):
        try:
            history = self.load_status_history(batch_id)
        except Exception:
            history = []
        history.append(status_data)
        with open(f"{BATCH_STATUS_DIR}/{batch_id}.json", "w") as f:
            for entry in history:
                f.write(json.dumps(entry) + '\n')

    def load_status_history(self, batch_id):
        with open(f"{BATCH_STATUS_DIR}/{batch_id}.json", "r") as f:
            history = [json.loads(line) for line in f]
        return history

    def load_status(self, batch_id):
        with open(f"{BATCH_STATUS_DIR}/{batch_id}.json", "r") as f:
            lines = f.readlines()
            if lines:
                return json.loads(lines[-1])
            else:
                raise HTTPException(status_code=404, detail="배치 ID가 존재하지 않습니다.")

    def log(self, batch_id, message):
        log_message = {
            "batch_id": batch_id,
            "message": message,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        with open(os.path.join(BATCH_LOG_DIR, 'batch_error.log'), 'a') as log_file:
            log_file.write(json.dumps(log_message) + "\n")

    def get_logs(self):
        log_path = os.path.join(BATCH_LOG_DIR, 'batch_error.log')
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                return [json.loads(line) for line in f]
        else:
            raise HTTPException(status_code=404, detail="오류 로그 파일이 존재하지 않습니다.")

batch_storage = BatchStorageHandler()

# 배치 상태 파일 생성 함수
def create_batch_status(batch_id, total_rows):
    batch_status = {
        "batch_id": batch_id,
        "total_rows": total_rows,
        "processed_rows": 0,
        "status": "in_progress",
        "start_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "last_update_ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    batch_storage.save_status(batch_id, batch_status)
    batch_storage.log(batch_id, f"Batch started at {batch_status['start_time']} with {total_rows} total rows.")

# 배치 상태 업데이트 함수
def update_batch_status(batch_id, processed_rows, total_rows, status="in_progress"):
    batch_status = batch_storage.load_status(batch_id)
    new_status = {
        "batch_id": batch_id,
        "total_rows": total_rows,
        "processed_rows": processed_rows,
        "status": status,
        "start_time": batch_status["start_time"],
        "last_update_ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    batch_storage.save_status(batch_id, new_status)
    batch_storage.log(batch_id, f"Updated status to {status} with {processed_rows} rows processed.")

# 배치 상태 조회 함수
@app.get("/batch-status/{batch_id}")
async def get_batch_status(batch_id: str):
    try:
        return batch_storage.load_status(batch_id)
    except Exception:
        raise HTTPException(status_code=404, detail="배치 ID가 존재하지 않습니다.")

# 진행 중인 모든 배치 작업 확인
@app.get("/ongoing-batches")
async def get_ongoing_batches():
    ongoing_batches = []
    for filename in os.listdir(BATCH_STATUS_DIR):
        if filename.endswith(".json"):
            with open(os.path.join(BATCH_STATUS_DIR, filename), "r") as f:
                batch_status = json.load(f)
                if batch_status["status"] == "in_progress":
                    ongoing_batches.append(batch_status["batch_id"])
    return {"ongoing_batches": ongoing_batches}

# 오류 로그 읽기 함수
@app.get("/error-log")
async def read_error_log():
    return {"logs": batch_storage.get_logs()}

# 홈 경로
@app.get("/", response_class=HTMLResponse)
async def main_page(request: Request):
    db_options = list(DBConnectionManager.load_configs().keys())
    return templates.TemplateResponse("index.html", {"request": request, "db_options": db_options})

# DB 연결 테스트
@app.post("/test-connection")
async def test_connection(selected_db: str = Form(...)):
    try:
        session = DBConnectionManager.get_session(selected_db)
        session.execute(text("SELECT 1"))
        return JSONResponse(content={"message": "DB 연결 성공"}, status_code=200)
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"DB 연결 실패: {str(e)}")

# 샘플 데이터 쿼리
@app.post("/query-sample")
async def query_sample(selected_db: str = Form(...), query: str = Form(...)):
    try:
        session = DBConnectionManager.get_session(selected_db)
        result = session.execute(text(query)).fetchmany(10)
        if result:
            sample_data = [row[0] for row in result]
            return {"sample_data": sample_data}
        else:
            raise HTTPException(status_code=404, detail="데이터 없음")
    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail=f"쿼리 실패: {str(e)}")

class RequestModel(BaseModel):
    instId: str
    ScpDbAgentApiVo: List[str]

# CryptoHub API 호출
@app.post("/convert-sample")
async def convert_sample(data: RequestModel):
    url = "https://i-dev-cryptohub.apddev.com/v1/convertDamo2CrytoData_list/awsuser11"
    headers = {
        "Authorization": "Bearer eyJraWQiOiJ3SnNTVzBHMk1GU2hPcGhLdVwvVmt6ZHNEUmI4Z3RPYVBMc21vQ2tlZzRkcz0iLCJhbGciOiJSUzI1NiJ9.eyJzdWIiOiI2NGU4MmQ3Yy03MDQxLTcwODgtOTUyMy0xNzM4MWExODU3OTIiLCJpc3MiOiJodHRwczpcL1wvY29nbml0by1pZHAuYXAtbm9ydGhlYXN0LTIuYW1hem9uYXdzLmNvbVwvYXAtbm9ydGhlYXN0LTJfTlJ2aVdIVFJvIiwiY2xpZW50X2lkIjoiNXN1Z2hscTQ1Z2lrZW5tZmt0cW9pYnRiYjYiLCJvcmlnaW5fanRpIjoiM2MzYWViNDQtZTQ4My00ZGIzLTkyZGItOWM0NGIzZDhkM2ZiIiwiZXZlbnRfaWQiOiI2ZWRkZjJmNC1iZGMzLTQwMzUtODVkOS04NWQ3YWQ3ZjU1ZDAiLCJ0b2tlbl91c2UiOiJhY2Nlc3MiLCJzY29wZSI6ImF3cy5jb2duaXRvLnNpZ25pbi51c2VyLmFkbWluIiwiYXV0aF90aW1lIjoxNzMxMDIxODc3LCJleHAiOjE3MzEwNjUwNzcsImlhdCI6MTczMTAyMTg3NywianRpIjoiYWQ5NTFlZTQtMWE2Mi00MDNmLWJlN2UtMzRmNmYwYTg2NDFhIiwidXNlcm5hbWUiOiJhd3N1c2VyMTEifQ.piyBvwlvpK9uIj_VG_x_zOvVW2bQPuPR-WA7S9RJbq2DJ1zX1RnQQSffCVK7UHlS8zpfgAnuV1msb2nICxiCBgeeIFnkclb5nC5pX73yo5ULc0IDphTr1SRBIMWPJFjo6MNXMksR7APQKXDwh05x5WIngXca8SU2-r4oh_PLNsH-UQ3vfFurKeTdKFOkPLQ5OwFuKfsHHM1AHq74dGt-8TSpPmWcDvhqEb1hOa9FdHGhlMB4ZpfeWXnPb6KoS5nQAfZo57J-i9j_eKsLos1rZ_zo1efrHJiSlQ-25v5hy14Hm-cGhgKwgEHkReLwYVpUVbTCUo4jGctR4R6tQwKzcw"
    }
    payload = {
        "instId": "X-Changer-server",
        "ScpDbAgentApiVo": data.ScpDbAgentApiVo
    }
    #print(f"payload : {payload}")
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

# 배치 작업 시작
@app.post("/batch-task")
async def start_batch_task(background_tasks: BackgroundTasks, selected_db: str = Form(...), query: str = Form(...)):
    batch_id = str(uuid.uuid4())
    session = DBConnectionManager.get_session(selected_db)
    total_rows = session.execute(text(f"SELECT COUNT(*) FROM ({query}) AS total_rows")).scalar()
    create_batch_status(batch_id, total_rows)
    background_tasks.add_task(process_batch, selected_db, query, batch_id, total_rows)
    return JSONResponse(content={"message": "배치 작업이 시작되었습니다.", "batch_id": batch_id})

@app.delete("/delete_all_log")
async def delete_all_logs():
    try:
        files = [f for f in os.listdir(BATCH_STATUS_DIR) if f.endswith('.json')]
        for f in files:
            os.remove(os.path.join(BATCH_STATUS_DIR, f))
        batch_storage.log('system', "All batch status files deleted")
        return {"message": "All batch status files deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# DB 유형 가져오기
def get_db_type(selected_db):
    config = DBConnectionManager.load_configs().get(selected_db)
    if not config:
        raise HTTPException(status_code=404, detail=f"{selected_db} 설정이 없습니다.")
    uri = config["uri"]
    if "oracle" in uri:
        return "oracle"
    elif "postgresql" in uri or "postgres" in uri:
        return "postgresql"
    elif "mysql" in uri:
        return "mysql"
    else:
        raise HTTPException(status_code=500, detail="지원되지 않는 데이터베이스 유형입니다.")

# 백그라운드 배치 작업 실행
async def process_batch(selected_db, query, batch_id, total_rows):
    session = DBConnectionManager.get_session(selected_db)
    db_type = get_db_type(selected_db)
    batch_size = 10  # 한 번에 처리할 데이터의 수
    sleep_time = 1
    offset = 0
    processed_rows = 0

    try:
        while processed_rows < total_rows:
            time.sleep(sleep_time)

            if db_type == "oracle":
                paginated_query = f"""
                SELECT * FROM (
                    SELECT inner_query.*, ROWNUM rnum FROM (
                        {query}
                    ) inner_query WHERE ROWNUM <= {offset + batch_size}
                )
                WHERE rnum > {offset}
                """
            else:
                paginated_query = f"""
                SELECT * FROM (
                    {query}
                ) AS subquery LIMIT {batch_size} OFFSET {offset}
                """
            result = session.execute(text(paginated_query)).fetchall()

            if not result:
                break

            data_batch = [row[0] for row in result]
            response = await convert_sample(RequestModel(instId="data-mig-server", ScpDbAgentApiVo=data_batch))
            results = response["ScpDbAgentApiVo"]

            update_query = text("""
                            UPDATE migration_results 
                            SET cryptohub = :outStr, rsltCd = :rsltCd, rsltMsg = :rsltMsg 
                            WHERE damo = :inStr
                            """)

            for i, row in enumerate(result):
                result_data = results[i]
                session.execute(
                    update_query,
                    {
                        "outStr": result_data.get("outStr"),
                        "rsltCd": result_data.get("rsltCd"),
                        "rsltMsg": result_data.get("rsltMsg"),
                        "inStr": result_data.get("inStr")
                    }
                )

            session.commit()
            processed_rows += len(result)
            offset += batch_size
            print(f"Batch {batch_id} - Processed {processed_rows} rows out of {total_rows}")

            update_batch_status(batch_id, processed_rows, total_rows)
            if processed_rows % batch_size == 0:
                batch_storage.log(batch_id, f"Processed {processed_rows} rows.")

        if processed_rows < total_rows:
            update_batch_status(batch_id, processed_rows, total_rows, 'incomplete')
            batch_storage.log(batch_id, f"Batch {batch_id} incomplete, processed only {processed_rows}/{total_rows} rows.")
        else:
            update_batch_status(batch_id, processed_rows, total_rows, 'completed')
            batch_storage.log(batch_id, f"Batch {batch_id} completed successfully with {processed_rows}/{total_rows} rows processed.")

    except Exception as e:
        update_batch_status(batch_id, processed_rows, total_rows, 'failed')
        batch_storage.log(batch_id, f"Error: {str(e)}")
    finally:
        # scoped_session을 사용하면 별도로 세션을 닫지 않아도 됨
        pass
