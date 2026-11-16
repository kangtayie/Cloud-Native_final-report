"""
[발음 표기 Lambda]  POST /pronounce
- 입력(JSON): {"text": "Gdzie jest biuro akademika?", "lang": "pl"}
- 출력(JSON): {"romanization": "gjeh yest byoo-ro ...", "hangul": "그제 예스트 비우로 ..."}
- 런타임: Python 3.12
- 필요 권한: bedrock:InvokeModel (환경 변수 MODEL_ID로 지정한 모델로만 제한)

- Bedrock Converse API를 사용해서 모델이 바뀌어도 코드는 그대로 두고 MODEL_ID만 바꾸면 되게 했다.
- 비용 관리: 입력 300자 제한, 출력 토큰(MAX_TOKENS) 제한, temperature 0(같은 입력엔 같은 답 → 프론트 캐시와 궁합).
- 장애 격리: 이 람다가 실패해도 번역·음성 람다는 영향이 없다. 프론트도 실패 시 발음 칸만 숨긴다.
(AI 활용: Claude 작성)
"""
import base64
import json
import os
import boto3

bedrock = boto3.client("bedrock-runtime")

MODEL_ID = os.environ.get("MODEL_ID", "")          # 콘솔 > 구성 > 환경 변수에서 설정
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "400"))
MAX_CHARS = 300  # Bedrock은 토큰당 과금이라 번역(1000자)보다 더 짧게 제한

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json; charset=utf-8",
}

# 프롬프트에 넣을 언어 이름 (Translate 언어 코드 기준)
LANG_NAMES = {
    "en": "English", "ja": "Japanese", "zh": "Chinese (Simplified, Mandarin)",
    "zh-TW": "Chinese (Traditional, Mandarin)", "es": "Spanish", "fr": "French",
    "de": "German", "ru": "Russian", "it": "Italian", "ar": "Arabic",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian",
    "pl": "Polish", "cs": "Czech", "ro": "Romanian", "hu": "Hungarian",
}

SYSTEM_PROMPT = (
    "You write pronunciation guides for Korean speakers. "
    "Given a sentence in {lang}, return ONLY a JSON object with two string fields:\n"
    '- "romanization": an easy English-style phonetic spelling of how the sentence sounds '
    "(not the official romanization; split syllables with hyphens).\n"
    '- "hangul": the same pronunciation written in Korean Hangul, as a Korean would read it aloud.\n'
    "Keep the original word boundaries and punctuation. No explanations, no markdown."
)


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


def parse_model_json(text):
    # 모델이 ```json ... ``` 같은 장식을 붙여도 첫 { ~ 마지막 } 만 잘라서 읽는다
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON not found")
    data = json.loads(text[start:end + 1])
    roman, hangul = data.get("romanization"), data.get("hangul")
    if not isinstance(roman, str) or not isinstance(hangul, str) or not roman or not hangul:
        raise ValueError("missing fields")
    return roman.strip(), hangul.strip()


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
        return respond(400, {"error": "발음을 표기할 문장이 없습니다."})
    if len(text) > MAX_CHARS:
        return respond(400, {"error": f"{MAX_CHARS}자 이하만 발음 표기를 지원합니다."})
    if lang not in LANG_NAMES:
        # 한국어(ko)는 한국인에게 발음 표기가 필요 없어서 제외
        return respond(400, {"error": "이 언어는 발음 표기를 지원하지 않습니다.", "supported": False})
    if not MODEL_ID:
        print("MODEL_ID 환경 변수가 비어 있음")
        return respond(500, {"error": "발음 표기 설정이 완료되지 않았습니다."})

    try:
        result = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT.format(lang=LANG_NAMES[lang])}],
            messages=[{"role": "user", "content": [{"text": text}]}],
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
        )
        # 토큰 사용량을 로그에 남겨 비용을 확인할 수 있게 함 (CloudWatch Logs)
        print("usage:", json.dumps(result.get("usage", {})))
        output = "".join(
            part.get("text", "") for part in result["output"]["message"]["content"]
        )
    except Exception as e:
        # 원본 오류는 로그에만 (오류 메시지에 계정 ID·모델 ARN이 들어갈 수 있음)
        print("Bedrock error:", repr(e))
        return respond(500, {"error": "발음 표기 생성 중 오류가 발생했습니다."})

    try:
        romanization, hangul = parse_model_json(output)
    except (ValueError, json.JSONDecodeError):
        print("모델 응답 파싱 실패:", output[:500])
        return respond(502, {"error": "발음 표기 결과를 읽지 못했습니다."})

    return respond(200, {"romanization": romanization, "hangul": hangul})
