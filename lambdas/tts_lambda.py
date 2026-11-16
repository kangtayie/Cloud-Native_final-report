"""
[음성합성 Lambda]  POST /speak
- 입력(JSON): {"text": "Dzień dobry", "lang": "pl"}
- 출력(JSON): {"audio": "<base64 mp3>", "voice": "Ola", "engine": "neural"}
  * Polly가 지원하지 않는 언어(vi, th, id, hu 등)는 400 + supported:false 반환
- 런타임: Python 3.12
- 필요 권한: polly:SynthesizeSpeech, polly:DescribeVoices

목소리 ID를 코드에 하드코딩하지 않고 DescribeVoices로 해당 언어 목소리를 조회해서 고름
(리전마다 neural 지원 목소리가 달라서, neural이 있으면 neural, 없으면 standard 사용).
(AI 활용: Claude 작성)
"""
import base64
import json
import boto3

polly = boto3.client("polly")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json; charset=utf-8",
}

# Translate 언어 코드 -> Polly 언어 코드
POLLY_LANG = {
    "ko": "ko-KR",
    "en": "en-US",
    "ja": "ja-JP",
    "zh": "cmn-CN",
    "zh-TW": "cmn-CN",   # Polly에는 대만 중국어 목소리가 없어 표준 중국어로 대체
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "ru": "ru-RU",
    "it": "it-IT",
    "ar": "arb",
    "pl": "pl-PL",
    "cs": "cs-CZ",
    "ro": "ro-RO",
}

MAX_CHARS = 500
_voice_cache = {}  # 같은 실행 환경(warm start)에서는 목소리 조회 결과 재사용


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


def pick_voice(polly_lang):
    if polly_lang in _voice_cache:
        return _voice_cache[polly_lang]
    for engine in ("neural", "standard"):
        voices = polly.describe_voices(Engine=engine, LanguageCode=polly_lang)["Voices"]
        if voices:
            _voice_cache[polly_lang] = (voices[0]["Id"], engine)
            return _voice_cache[polly_lang]
    return None


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method == "OPTIONS":
        return respond(200, {})

    try:
        data = read_body(event)
    except (ValueError, UnicodeDecodeError):
        return respond(400, {"error": "JSON 형식이 올바르지 않습니다."})

    text = (data.get("text") or "").strip()
    lang = data.get("lang") or ""

    if not text:
        return respond(400, {"error": "읽어줄 문장이 없습니다."})
    if len(text) > MAX_CHARS:
        return respond(400, {"error": f"{MAX_CHARS}자 이하만 읽을 수 있습니다."})

    polly_lang = POLLY_LANG.get(lang)
    try:
        voice = pick_voice(polly_lang) if polly_lang else None
    except Exception as e:  # 권한 누락 등 (원본 오류는 로그에만)
        print("DescribeVoices error:", repr(e))
        return respond(500, {"error": "음성 목록을 불러오지 못했습니다."})
    if not voice:
        return respond(400, {"error": "이 언어는 원어민 음성을 지원하지 않습니다.", "supported": False})

    voice_id, engine = voice
    try:
        result = polly.synthesize_speech(
            Text=text,
            OutputFormat="mp3",
            VoiceId=voice_id,
            Engine=engine,
            LanguageCode=polly_lang,
        )
        audio_bytes = result["AudioStream"].read()
    except Exception as e:
        # 원본 오류는 CloudWatch Logs에만 남긴다 (오류 메시지에 계정 ID가 들어갈 수 있음)
        print("Polly error:", repr(e))
        return respond(500, {"error": "음성 생성 중 오류가 발생했습니다."})

    return respond(200, {
        "audio": base64.b64encode(audio_bytes).decode("ascii"),
        "voice": voice_id,
        "engine": engine,
    })
