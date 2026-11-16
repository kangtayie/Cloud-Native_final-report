"""
[번역 Lambda]  POST /translate
- 입력(JSON): {"text": "안녕하세요", "source": "ko", "target": "pl"}
  * source를 "auto"로 보내면 Translate가 언어를 자동 감지 (Comprehend 권한 필요)
- 출력(JSON): {"translatedText": "...", "source": "ko", "target": "pl"}
- 런타임: Python 3.12 (boto3 기본 포함)
- 필요 권한: translate:TranslateText, (auto 사용 시) comprehend:DetectDominantLanguage

※ 교재 8.6(Node 8.10 + aws-sdk v2) 코드를 현재 Lambda 런타임에 맞게 Python으로 재작성하고,
  출발어/도착어를 요청에서 받도록 확장한 버전. (AI 활용: Claude 작성)
"""
import base64
import json
import boto3

translate = boto3.client("translate")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json; charset=utf-8",
}

MAX_CHARS = 1000  # 과금·남용 방지를 위한 입력 길이 제한 (Translate는 글자 수로 과금)


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


def lambda_handler(event, context):
    # API Gateway(HTTP API)가 보낸 CORS 사전 요청 처리
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method == "OPTIONS":
        return respond(200, {})

    try:
        data = read_body(event)
    except (ValueError, UnicodeDecodeError):
        return respond(400, {"error": "JSON 형식이 올바르지 않습니다."})

    text = (data.get("text") or "").strip()
    source = data.get("source") or "auto"
    target = data.get("target") or "en"

    if not text:
        return respond(400, {"error": "번역할 문장을 입력해주세요."})
    if len(text) > MAX_CHARS:
        return respond(400, {"error": f"{MAX_CHARS}자 이하로 입력해주세요."})
    if source == target:
        return respond(400, {"error": "내 언어와 현지 언어가 같습니다."})

    try:
        result = translate.translate_text(
            Text=text,
            SourceLanguageCode=source,
            TargetLanguageCode=target,
        )
    except translate.exceptions.UnsupportedLanguagePairException:
        return respond(400, {"error": "지원하지 않는 언어 조합입니다."})
    except Exception as e:
        # 원본 오류는 CloudWatch Logs에만 남긴다.
        # 권한 오류 메시지에는 역할 ARN(계정 ID 포함)이 들어 있어 응답으로 내보내지 않는다.
        print("Translate error:", repr(e))
        return respond(500, {"error": "번역 중 오류가 발생했습니다."})

    return respond(200, {
        "translatedText": result["TranslatedText"],
        "source": result["SourceLanguageCode"],
        "target": result["TargetLanguageCode"],
    })
