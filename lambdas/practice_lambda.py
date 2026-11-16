"""
[따라 말하기 평가 Lambda]  POST /practice,  GET /practice/{jobId}
- POST 입력(JSON): {"audio": "<base64 녹음>", "mime": "audio/webm", "text": "Gdzie jest biuro akademika?", "lang": "pl"}
       출력: 202 {"jobId": "..."}   ← 인식 작업만 시작하고 바로 응답
- GET  출력: {"status": "IN_PROGRESS"}  또는
             {"status": "COMPLETED", "heard": "인식된 문장", "score": 87, "words": [{"w": "Gdzie", "ok": true}, ...]}
- 런타임: Python 3.12
- 필요 권한: transcribe:StartTranscriptionJob / GetTranscriptionJob, 녹음 버킷 practice/* 의 s3:PutObject / GetObject

- 왜 비동기(작업 시작 → 폴링)인가: Transcribe 일괄 작업은 짧은 녹음도 10~30초가 걸려서
  API Gateway 통합 제한 시간(30초)을 넘길 수 있다. 그래서 POST는 jobId만 돌려주고 프론트가 GET으로 결과를 확인한다.
- 왜 녹음을 base64 본문으로 받는가: 녹음은 최대 15초(수십~수백 KB)라 람다 요청 한도(6MB)에 충분히 들어가고,
  presigned URL 방식보다 버킷 CORS 설정과 요청 왕복이 줄어 구조가 단순하다.
- 비용: Transcribe 서울 $0.0001/초, 요청당 최소 15초 과금 → 1회 약 $0.0015. 녹음 버킷은 수명주기 1일 삭제.
- 채점은 "원어민 음성 인식기가 정답 문장으로 알아들었는가"를 측정한다 (억양·강세 채점이 아님).
(AI 활용: Claude 작성)
"""
import base64
import difflib
import json
import os
import re
import unicodedata
import urllib.request
import uuid
import boto3

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")

BUCKET = os.environ.get("RECORD_BUCKET", "")       # 콘솔 > 구성 > 환경 변수에서 설정
MAX_TEXT = 500                                      # TTS와 같은 한도 (따라 말할 문장)
MAX_AUDIO_BYTES = 1_500_000                         # 15초 녹음이면 충분한 크기, 남용 방지
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")           # GET 경로 값 검증 (임의 작업 조회 방지)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json; charset=utf-8",
}

# Translate 언어 코드 → Transcribe 언어 코드 (현지어로 인식해야 원어민 기준 평가가 됨)
TRANSCRIBE_LANG = {
    "ko": "ko-KR", "en": "en-US", "ja": "ja-JP", "zh": "zh-CN", "zh-TW": "zh-TW",
    "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "ru": "ru-RU", "it": "it-IT",
    "ar": "ar-SA", "vi": "vi-VN", "th": "th-TH", "id": "id-ID",
    "pl": "pl-PL", "cs": "cs-CZ", "ro": "ro-RO", "hu": "hu-HU",
}
# 띄어쓰기로 단어를 나누지 않는 언어는 글자 단위로 비교
CHAR_LANGS = {"ja", "zh", "zh-TW", "th"}

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


# ---------- 채점 ----------
def normalize(s):
    # 소문자 + 구두점 제거 + 공백 정리 (인식기는 구두점·대소문자를 제멋대로 붙이므로)
    s = "".join(" " if unicodedata.category(c).startswith("P") else c for c in s.lower())
    return " ".join(s.split())


def tokens(text, lang):
    # 화면 표시용 원문 토큰과 비교용 정규화 토큰을 짝지어 돌려준다
    if lang in CHAR_LANGS:
        raw = [c for c in text if not c.isspace()]
    else:
        raw = text.split()
    pairs = [(r, normalize(r).replace(" ", "")) for r in raw]
    return [(r, n) for r, n in pairs if n]


def edit_distance(a, b):
    # 문자 단위 편집 거리 (Levenshtein). 외부 라이브러리를 못 쓰므로 직접 구현
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def score(expected, heard, lang):
    exp_tokens = tokens(expected, lang)
    heard_norm = [n for _, n in tokens(heard, lang)]
    exp_norm = [n for _, n in exp_tokens]

    # 일치율: 띄어쓰기를 뺀 글자열의 편집 거리 기반 유사도(%)
    a, b = "".join(exp_norm), "".join(heard_norm)
    pct = 0 if not a else round(max(0.0, 1 - edit_distance(a, b) / max(len(a), len(b))) * 100)

    # 단어별 맞음/틀림: 정답 단어 중 인식 결과와 순서대로 일치한 것만 ok
    ok = [False] * len(exp_tokens)
    matcher = difflib.SequenceMatcher(a=exp_norm, b=heard_norm, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            ok[block.a + k] = True
    return pct, [{"w": r, "ok": o} for (r, _), o in zip(exp_tokens, ok)]


# ---------- POST: 녹음 저장 + 인식 작업 시작 ----------
def start_practice(event):
    try:
        data = read_body(event)
    except (ValueError, UnicodeDecodeError):
        return respond(400, {"error": "JSON 형식이 올바르지 않습니다."})

    text = (data.get("text") or "").strip()
    lang = data.get("lang") or ""
    mime = (data.get("mime") or "").split(";")[0].strip().lower()  # "audio/webm;codecs=opus" → "audio/webm"

    if not text:
        return respond(400, {"error": "따라 말할 문장이 없습니다."})
    if len(text) > MAX_TEXT:
        return respond(400, {"error": f"{MAX_TEXT}자 이하 문장만 연습할 수 있습니다."})
    if lang not in TRANSCRIBE_LANG:
        return respond(400, {"error": "이 언어는 따라 말하기를 지원하지 않습니다.", "supported": False})
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
        return respond(500, {"error": "따라 말하기 설정이 완료되지 않았습니다."})

    job_id = uuid.uuid4().hex
    fmt = MEDIA_FORMATS[mime]
    audio_key = f"practice/{job_id}.{fmt}"
    try:
        s3.put_object(Bucket=BUCKET, Key=audio_key, Body=audio, ContentType=mime)
        # 정답 문장은 같은 버킷에 저장해 두고 GET 때 읽는다 (DB 없이 상태 보관, 1일 후 함께 삭제됨)
        s3.put_object(
            Bucket=BUCKET, Key=f"practice/{job_id}.json",
            Body=json.dumps({"text": text, "lang": lang}, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        # 출력 버킷을 지정하지 않으면 Transcribe가 관리하는 저장소에 결과를 두고 임시 URL을 준다
        # → 람다에 결과 쓰기 권한이 필요 없어 권한이 더 작아진다
        transcribe.start_transcription_job(
            TranscriptionJobName=f"practice-{job_id}",
            LanguageCode=TRANSCRIBE_LANG[lang],
            MediaFormat=fmt,
            Media={"MediaFileUri": f"s3://{BUCKET}/{audio_key}"},
        )
    except Exception as e:
        print("start error:", repr(e))
        return respond(500, {"error": "음성 인식을 시작하지 못했습니다."})

    return respond(202, {"jobId": job_id})


# ---------- GET: 인식 결과 확인 + 채점 ----------
def get_result(event):
    job_id = (event.get("pathParameters") or {}).get("jobId", "")
    if not JOB_ID_RE.match(job_id):
        return respond(400, {"error": "작업 ID가 올바르지 않습니다."})

    try:
        job = transcribe.get_transcription_job(
            TranscriptionJobName=f"practice-{job_id}"
        )["TranscriptionJob"]
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
        heard = result["results"]["transcripts"][0]["transcript"] if result["results"]["transcripts"] else ""
        meta = json.loads(s3.get_object(Bucket=BUCKET, Key=f"practice/{job_id}.json")["Body"].read())
    except Exception as e:
        print("result error:", repr(e))
        return respond(500, {"error": "인식 결과를 읽지 못했습니다."})

    pct, words = score(meta["text"], heard, meta["lang"])
    return respond(200, {"status": "COMPLETED", "heard": heard, "score": pct, "words": words})


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method == "OPTIONS":
        return respond(200, {})
    if method == "POST":
        return start_practice(event)
    if method == "GET":
        return get_result(event)
    return respond(405, {"error": "지원하지 않는 요청입니다."})
