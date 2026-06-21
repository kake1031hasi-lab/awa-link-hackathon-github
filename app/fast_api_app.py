# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import uuid
import datetime
from urllib.parse import parse_qs

import google.auth
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks, Form
from fastapi.responses import HTMLResponse
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging

from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

# LINE Bot SDK / Elasticsearch / Gemini SDK
from linebot.v3.webhook import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from elasticsearch import Elasticsearch
from google import genai
from google.genai import types

setup_telemetry()
try:
    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client()
    logger = logging_client.logger(__name__)
except Exception:
    project_id = "xauto-489307"
    logger = None
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=True,
    auto_create_session=True,
)
app.title = "awa-link-hackathon"
app.description = "API for interacting with the Agent awa-link-hackathon"

# 環境変数の読み込み
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
ELASTIC_URL = os.getenv("ELASTIC_URL")
ELASTIC_API_KEY = os.getenv("ELASTIC_API_KEY")
ELASTIC_INDEX = os.getenv("ELASTIC_INDEX", "awa-link-sr")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_LINE_USER_ID = os.getenv("ADMIN_LINE_USER_ID")

# Elasticsearchの初期化
es_client = None
if ELASTIC_URL and ELASTIC_API_KEY:
    es_client = Elasticsearch(ELASTIC_URL, api_key=ELASTIC_API_KEY)

# 論文検索関数
def search_elasticsearch(query_text: str) -> str:
    if not es_client:
        print("Elasticsearch config is missing. Skipping search.")
        return "（現在、論文データベースに接続されていません。）"
    try:
        res = es_client.search(
            index=ELASTIC_INDEX,
            size=3,
            query={
                "match": {
                    "text": query_text
                }
            }
        )
        hits = res.get('hits', {}).get('hits', [])
        if not hits:
            return "（関連する論文情報が見つかりませんでした。）"
            
        context = "【参考とするシステマティック・レビュー（SR）の論文知見】\n"
        for i, hit in enumerate(hits):
            source = hit.get('_source', {})
            filename = source.get('filename', '不明')
            page = source.get('page', '不明')
            text = source.get('text', '')
            context += f"[文献{i + 1}] 文献名: {filename} (p.{page})\n内容:\n{text}\n\n"
        return context
    except Exception as e:
        print(f"Elasticsearch search error: {e}")
        return "（論文情報の検索中にエラーが発生しました。）"

# Gemini回答生成関数
def generate_answer_with_gemini(user_message: str, context: str) -> str:
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is not set.")
        return "申し訳ありません。現在Gemini APIキーが設定されていません。"
        
    system_instruction = (
        "あなたはリハビリテーション専門職（理学療法士・作業療法士・言語聴覚士）の臨床推論をサポートする優秀なAIアシスタントです。\n"
        "回答は常に『批判しない、温かい、根拠のある』姿勢で行ってください。\n"
        "提供された参考情報（論文のシステマティック・レビュー等）に基づいて、根拠を明確にして回答してください。\n"
        "可能であれば、提示された[文献X]という形式で、どの文献に記載されているかを引用元として明記してください。\n"
        "もし参考情報に回答の根拠が含まれていない場合は、その旨を正直に伝えつつ、一般的な医学的知見として知られていることを親身にアドバイスしてください。\n"
        "嘘をついたり、ない情報をでっち上げたりしないでください。"
    )
    
    prompt = f"参考情報:\n{context}\n\nユーザーの質問: {user_message}"
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY, vertexai=False)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction
            )
        )
        return response.text or "申し訳ありません。回答を生成できませんでした。"
    except Exception as e:
        print(f"Gemini API invocation error: {e}")
        return "申し訳ありません。Gemini APIの呼び出し中に接続エラーが発生しました。"

# 会話ログ保存関数
def save_chat_log_to_elasticsearch(log_id: str, user_id: str, user_message: str, bot_response: str):
    if not es_client:
        return
    try:
        es_client.index(
            index='awa-link-logs',
            id=log_id,
            document={
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "userId": user_id,
                "userMessage": user_message,
                "botResponse": bot_response,
                "feedback": None
            }
        )
    except Exception as e:
        print(f"Elasticsearch save log error: {e}")

# フィードバック更新関数
def update_chat_log_feedback(log_id: str, feedback_value: str, reason: str | None):
    if not es_client:
        return
    try:
        update_doc = {"feedback": feedback_value}
        if reason is not None:
            update_doc["feedback_reason"] = reason
            
        es_client.update(
            index='awa-link-logs',
            id=log_id,
            doc=update_doc
        )
    except Exception as e:
        print(f"Elasticsearch update feedback error: {e}")

# 会話ログ取得関数
def get_chat_log_from_elasticsearch(log_id: str):
    if not es_client:
        return None
    try:
        res = es_client.get(index='awa-link-logs', id=log_id)
        return res.get('_source')
    except Exception as e:
        print(f"Failed to get chat log: {e}")
        return None

# Elasticsearch集計関数
def get_elasticsearch_stats() -> str:
    if not es_client:
        return "⚠️ Elasticsearch構成が無効です。"
    try:
        total_res = es_client.count(index='awa-link-logs')
        total_count = total_res.get('count', 0)
        
        good_res = es_client.count(
            index='awa-link-logs',
            query={"match": {"feedback": "good"}}
        )
        good_count = good_res.get('count', 0)
        
        bad_res = es_client.count(
            index='awa-link-logs',
            query={"match": {"feedback": "bad"}}
        )
        bad_count = bad_res.get('count', 0)
        
        unrated_count = total_count - (good_count + bad_count)
        
        stats_message = (
            f"📊 【AWA-LINK 利用統計・評価集計】\n\n"
            f"• 総やり取り回数: {total_count} 件\n"
            f"• 評価内訳:\n"
            f"  👍 役に立った: {good_count} 件\n"
            f"  👎 役に立たなかった: {bad_count} 件\n"
            f"  💬 未評価: {unrated_count} 件\n\n"
            f"※この集計は管理者であるあなたにのみ返信されています。"
        )
        return stats_message
    except Exception as e:
        print(f"Failed to generate stats: {e}")
        return "⚠️ 集計データの取得中にエラーが発生しました。"

# LINE イベントハンドラ
async def handle_line_event(event, request: Request, background_tasks: BackgroundTasks):
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    
    if isinstance(event, PostbackEvent):
        data = event.postback.data
        reply_token = event.reply_token
        
        params = parse_qs(data)
        action = params.get('action', [None])[0]
        value = params.get('value', [None])[0]
        log_id = params.get('logId', [None])[0]
        
        if action == 'feedback' and log_id and value:
            thanks_text = '高評価をいただきありがとうございます！励みになります。' if value == 'good' else 'フィードバックありがとうございます。より良い回答ができるよう改善いたします。'
            
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text=thanks_text)]
                    )
                )
            update_chat_log_feedback(log_id, value, None)
            
    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        user_message = event.message.text
        reply_token = event.reply_token
        user_id = event.source.user_id if event.source and event.source.user_id else 'unknown'
        
        if user_message.strip() == '集計' and ADMIN_LINE_USER_ID and user_id == ADMIN_LINE_USER_ID:
            stats_text = get_elasticsearch_stats()
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text=stats_text)]
                    )
                )
            return
            
        search_results = search_elasticsearch(user_message)
        
        session_id = f"line-{user_id}"
        prompt = f"参考情報:\n{search_results}\n\nユーザーの質問: {user_message}"
        
        import httpx
        payload = {
            "app_name": "app",
            "user_id": user_id,
            "session_id": session_id,
            "new_message": {
                "role": "user",
                "parts": [{"text": prompt}]
            }
        }
        
        generated_answer = "申し訳ありません。回答を生成できませんでした。"
        is_data_missing = False
        
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.post("/run", json=payload)
                if res.status_code == 200:
                    events = res.json()
                    output_texts = []
                    for event in events:
                        content = event.get("content")
                        if content and "parts" in content:
                            for part in content["parts"]:
                                if "text" in part:
                                    output_texts.append(part["text"])
                    generated_answer = "".join(output_texts)
                else:
                    print(f"ADK Agent run error: {res.status_code} - {res.text}")
                    generated_answer = "申し訳ありません。エージェントの呼び出し中にエラーが発生しました。"
        except Exception as e:
            print(f"ADK Agent invocation exception: {e}")
            generated_answer = "申し訳ありません。システムエラーにより回答を生成できませんでした。"
            
        print(f"DEBUG: User message: {user_message}")
        print(f"DEBUG: Search results: {search_results}")
        print(f"DEBUG: Generated answer (Raw): {repr(generated_answer)}")
        
        # 特許A（隠しタグ）の検知・処理部分はハッカソン用リポジトリ向けに無効化・ダミー化
        is_data_missing = False
        final_answer = generated_answer
        # 以前は特許アルゴリズムによる欠損判定を行っていたが、ハッカソン用には当たり障りのないダミー判定とする
        if "データがありません" in generated_answer or "情報が不足" in generated_answer:
            is_data_missing = True
            import logging
            logging.getLogger("app").info("Knowledge gap detected")
            
        print(f"DEBUG: Final answer: {repr(final_answer)}")
        log_id = str(uuid.uuid4())
        
        # 特許B（非干渉評価UI）の生リンク付与部分はハッカソン用リポジトリ向けに無効化・ダミー化
        # ハッカソン用ダミー：評価はLINEの標準機能やスタンプ等で行うものと想定
        final_answer_with_link = (
            f"{final_answer}\n\n"
            f"------------------\n"
            f"※この回答への評価は、LINE公式アカウントの標準メニューよりフィードバックをお送りいただけます。"
        )
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=final_answer_with_link)]
                )
            )
            
            save_chat_log_to_elasticsearch(log_id, user_id, user_message, final_answer)
            
            if is_data_missing and ADMIN_LINE_USER_ID:
                try:
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=ADMIN_LINE_USER_ID,
                            messages=[TextMessage(text=f"⚠️ 【データベース情報不足アラート】\n論文データベースに必要な知見が見つかりませんでした。\n\n・ユーザーの質問: {user_message}\n・AIの回答: {final_answer}\n\n※このテーマに関する論文の追加登録を検討してください。")]
                        )
                    )
                except Exception as ex:
                    print(f"Failed to send admin push message: {ex}")

# Webhookエンドポイント
@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(None)
):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing Signature")
        
    body = await request.body()
    body_str = body.decode('utf-8')
    
    if not LINE_CHANNEL_SECRET:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_SECRET is not set")
        
    parser = WebhookParser(LINE_CHANNEL_SECRET)
    try:
        events = parser.parse(body_str, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    for event in events:
        await handle_line_event(event, request, background_tasks)
        
    return "OK"

# フィードバックGETエンドポイント
@app.get("/feedback", response_class=HTMLResponse)
async def feedback_page(
    value: str = "",
    logId: str = ""
):
    if logId and value in ('good', 'bad'):
        update_chat_log_feedback(logId, value, None)
        
    is_good = (value == 'good')
    theme_color = '#06c755' if is_good else '#e15252'
    icon = '👍' if is_good else '👎'
    title = '高評価をありがとうございます！' if is_good else 'ご協力ありがとうございます'
    
    if is_good:
        html_content = f"""<!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>AWA-LINK フィードバック</title>
          <style>
            body {{
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
              text-align: center;
              padding: 50px 20px;
              background-color: #f4f7f6;
              color: #333;
            }}
            .card {{
              background: white;
              padding: 40px 30px;
              border-radius: 16px;
              box-shadow: 0 8px 20px rgba(0,0,0,0.06);
              display: inline-block;
              max-width: 400px;
              width: 100%;
              box-sizing: border-box;
            }}
            .icon {{
              font-size: 48px;
              margin-bottom: 20px;
            }}
            h1 {{
              font-size: 20px;
              margin-bottom: 15px;
              color: {theme_color};
            }}
            p {{
              font-size: 14px;
              line-height: 1.6;
              color: #666;
              margin-bottom: 0;
            }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="icon">{icon}</div>
            <h1>{title}</h1>
            <p>高評価をいただき、誠にありがとうございます。励みになります！</p>
          </div>
        </body>
        </html>"""
        return html_content
    else:
        html_content = f"""<!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>AWA-LINK フィードバック</title>
          <style>
            body {{
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
              text-align: center;
              padding: 50px 20px;
              background-color: #f4f7f6;
              color: #333;
            }}
            .card {{
              background: white;
              padding: 40px 30px;
              border-radius: 16px;
              box-shadow: 0 8px 20px rgba(0,0,0,0.06);
              display: inline-block;
              max-width: 400px;
              width: 100%;
              box-sizing: border-box;
              text-align: left;
            }}
            .icon {{
              font-size: 48px;
              margin-bottom: 20px;
              text-align: center;
            }}
            h1 {{
              font-size: 20px;
              margin-bottom: 15px;
              color: {theme_color};
              text-align: center;
            }}
            p {{
              font-size: 14px;
              line-height: 1.6;
              color: #666;
              margin-bottom: 20px;
              text-align: center;
            }}
            textarea {{
              width: 100%;
              box-sizing: border-box;
              padding: 12px;
              border-radius: 8px;
              border: 1px solid #ddd;
              font-size: 14px;
              resize: none;
              margin-bottom: 20px;
              outline: none;
            }}
            textarea:focus {{
              border-color: {theme_color};
            }}
            button {{
              background-color: {theme_color};
              color: white;
              border: none;
              padding: 12px 24px;
              border-radius: 8px;
              font-size: 14px;
              font-weight: bold;
              cursor: pointer;
              width: 100%;
              transition: background-color 0.2s;
            }}
            button:hover {{
              background-color: #c93d3d;
            }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="icon">{icon}</div>
            <h1>{title}</h1>
            <p>どのような点がいまいちでしたか？<br>改善のため、よろしければ理由を教えてください。</p>
            
            <form action="/feedback/submit" method="POST">
              <input type="hidden" name="logId" value="{logId}">
              <input type="hidden" name="value" value="{value}">
              <textarea name="reason" rows="4" placeholder="例：具体的なリハビリ方法の記載が少なかった、もっと文献名が知りたかった、など" required></textarea>
              <button type="submit">送信する</button>
            </form>
          </div>
        </body>
        </html>"""
        return html_content

# フィードバックPOSTエンドポイント
@app.post("/feedback/submit", response_class=HTMLResponse)
async def feedback_submit(
    logId: str = Form(...),
    value: str = Form(...),
    reason: str = Form(...)
):
    if logId and value:
        update_chat_log_feedback(logId, value, reason)
        
        if ADMIN_LINE_USER_ID:
            log_data = get_chat_log_from_elasticsearch(logId)
            user_msg = log_data.get('userMessage', '不明な質問') if log_data else '不明な質問'
            bot_resp = log_data.get('botResponse', '不明な回答') if log_data else '不明な回答'
            
            configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                try:
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=ADMIN_LINE_USER_ID,
                            messages=[TextMessage(text=f"👎 【回答いまいちフィードバック】\nユーザーから具体的な理由が送信されました。\n\n・いまいちだった理由:\n{reason or '（入力なし）'}\n\n・ユーザーの質問:\n{user_msg}\n\n・AIの回答:\n{bot_resp}")]
                        )
                    )
                except Exception as ex:
                    print(f"Failed to send admin feedback notification: {ex}")
                    
    html_content = """<!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>AWA-LINK フィードバック</title>
      <style>
        body {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
          text-align: center;
          padding: 50px 20px;
          background-color: #f4f7f6;
          color: #333;
        }
        .card {
          background: white;
          padding: 40px 30px;
          border-radius: 16px;
          box-shadow: 0 8px 20px rgba(0,0,0,0.06);
          display: inline-block;
          max-width: 400px;
          width: 100%;
          box-sizing: border-box;
        }
        .icon {
          font-size: 48px;
          margin-bottom: 20px;
        }
        h1 {
          font-size: 20px;
          margin-bottom: 15px;
          color: #06c755;
        }
        p {
          font-size: 14px;
          line-height: 1.6;
          color: #666;
          margin-bottom: 0;
        }
      </style>
    </head>
    <body>
      <div class="card">
        <div class="icon">✉️</div>
        <h1>送信が完了しました</h1>
        <p>貴重なフィードバックをありがとうございました。サービスの改善に役立てさせていただきます。</p>
      </div>
    </body>
    </html>"""
    return html_content

@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    if logger:
        logger.log_struct(feedback.model_dump(), severity="INFO")
    else:
        print(f"Feedback collected: {feedback.model_dump()}")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
