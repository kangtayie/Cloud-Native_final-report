"""
[음성 입력(STT) Lambda]  POST /transcribe,  GET /transcribe/{jobId}
- POST 입력(JSON): {"audio": "<base64 녹음>", "mime": "audio/webm", "lang": "ko"}
       출력: 202 {"jobId": "..."}
- GET  출력: {"status": "IN_PROGRESS"}  또는  {"status": "COMPLETED", "text": "기숙사 행정실이 어디에 있나요"}
- 런타임: Python 3.12
- 필요 권한: transcribe:StartTranscriptionJob / GetTranscriptionJob, 녹음 버킷 stt/* 의 s3:PutObject / GetObject

- 사용자가 "내 언어"로 말한 것을 글자로 바꿔 입력칸에 넣는다. 잘못 인식된 부분은 사용자가 직접 고친 뒤 번역한다.
- 따라 말하기(practice-lambda)와 같은 비동기 구조지만, 기능이 다르므로 람다·IAM 역할·S3 폴더를 분리했다 (MSA, 최소 권한).
  공통 코드는 람다끼리 호출하지 않고 각 파일에 작은 헬퍼로 둔다 (콘솔 붙여넣기 배포를 위해 파일 하나로 완결).
- 비용: Transcribe 서울 $0.0001/초, 요청당 최소 15초 과금 → 1회 약 $0.0015. 녹음은 수명주기 규칙으로 1일 후 삭제.
(AI 활용: Claude 작성)
"""
import base64
import json
import os
import re
import urllib.request
import uuid
import boto3

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")

BUCKET = os.environ.get("RECORD_BUCKET", "")       # 콘솔 > 구성 > 환경 변수에서 설정
MAX_AUDIO_BYTES = 1_500_000                         # 15초 녹음이면 충분한 크기, 남용 방지
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")           # GET 경로 값 검증 (임의 작업 조회 방지)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json; charset=utf-8",
}

# Translate 언어 코드 → Transcribe 언어 코드
TRANSCRIBE_LANG = {
    "ko": "ko-KR", "en": "en-US", "ja": "ja-JP", "zh": "zh-CN", "zh-TW": "zh-TW",
    "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "ru": "ru-RU", "it": "it-IT",
    "ar": "ar-SA", "vi": "vi-VN", "th": "th-TH", "id": "id-ID",
    "pl": "pl-PL", "cs": "cs-CZ", "ro": "ro-RO", "hu": "hu-HU",
}

# 브라우저 녹음 형식(MIME) → Transcribe MediaFormat
MEDIA_FORMATS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/wav": "wav"}


def respond(status, body):
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, ensure_ascii=False),
    }


def read_body(event):
    # HTTP API는 본문을 base64로 인코딩해서 넘기는 경우가 있어 두 경우 모두 처리
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


# ---------- POST: 녹음 저장 + 인식 작업 시작 ----------
def start_job(event):
    try:
        data = read_body(event)
    except (ValueError, UnicodeDecodeError):
        return respond(400, {"error": "JSON 형식이 올바르지 않습니다."})

    lang = data.get("lang") or ""
    mime = (data.get("mime") or "").split(";")[0].strip().lower()  # "audio/webm;codecs=opus" → "audio/webm"

    if lang not in TRANSCRIBE_LANG:
        return respond(400, {"error": "이 언어는 음성 입력을 지원하지 않습니다.", "supported": False})
    if mime not in MEDIA_FORMATS:
        return respond(400, {"error": "지원하지 않는 녹음 형식입니다."})
    try:
        audio = base64.b64decode(data.get("audio") or "", validate=True)
    except ValueError:
        return respond(400, {"error": "녹음 데이터가 올바르지 않습니다."})
    if not audio:
        return respond(400, {"error": "녹음이 비어 있습니다."})
    if len(audio) > MAX_AUDIO_BYTES:
        return respond(400, {"error": "녹음이 너무 깁니다. 15초 이내로 말해 주세요."})
    if not BUCKET:
        print("RECORD_BUCKET 환경 변수가 비어 있음")
        return respond(500, {"error": "음성 입력 설정이 완료되지 않았습니다."})

    job_id = uuid.uuid4().hex
    fmt = MEDIA_FORMATS[mime]
    key = f"stt/{job_id}.{fmt}"
    try:
        s3.put_object(Bucket=BUCKET, Key=key, Body=audio, ContentType=mime)
        # 출력 버킷을 지정하지 않으면 Transcribe 관리 저장소의 임시 URL로 결과를 받는다 → 결과 쓰기 권한 불필요
        transcribe.start_transcription_job(
            TranscriptionJobName=f"stt-{job_id}",
            LanguageCode=TRANSCRIBE_LANG[lang],
            MediaFormat=fmt,
            Media={"MediaFileUri": f"s3://{BUCKET}/{key}"},
        )
    except Exception as e:
        print("start error:", repr(e))
        return respond(500, {"error": "음성 인식을 시작하지 못했습니다."})

    return respond(202, {"jobId": job_id})


# ---------- GET: 인식 결과 확인 ----------
def get_result(event):
    job_id = (event.get("pathParameters") or {}).get("jobId", "")
    if not JOB_ID_RE.match(job_id):
        return respond(400, {"error": "작업 ID가 올바르지 않습니다."})

    try:
        job = transcribe.get_transcription_job(TranscriptionJobName=f"stt-{job_id}")["TranscriptionJob"]
    except transcribe.exceptions.BadRequestException:
        return respond(404, {"error": "인식 작업을 찾을 수 없습니다."})
    except Exception as e:
        print("get error:", repr(e))
        return respond(500, {"error": "인식 결과를 확인하지 못했습니다."})

    status = job["TranscriptionJobStatus"]
    if status in ("QUEUED", "IN_PROGRESS"):
        return respond(200, {"status": "IN_PROGRESS"})
    if status == "FAILED":
        print("job failed:", job.get("FailureReason"))
        return respond(200, {"status": "FAILED", "error": "음성을 인식하지 못했어요. 다시 녹음해 주세요."})

    try:
        uri = job["Transcript"]["TranscriptFileUri"]
        if not uri.startswith("https://"):  # Transcribe가 준 임시 URL만 읽음
            raise ValueError("unexpected uri")
        with urllib.request.urlopen(uri, timeout=5) as r:
            result = json.load(r)
        transcripts = result["results"]["transcripts"]
        text = transcripts[0]["transcript"] if transcripts else ""
    except Exception as e:
        print("result error:", repr(e))
        return respond(500, {"error": "인식 결과를 읽지 못했습니다."})

    return respond(200, {"status": "COMPLETED", "text": text})


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method == "OPTIONS":
        return respond(200, {})
    if method == "POST":
        return start_job(event)
    if method == "GET":
        return get_result(event)
    return respond(405, {"error": "지원하지 않는 요청입니다."})
