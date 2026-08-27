import os
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic
from notion_client import Client as NotionClient

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# 任意（未設定ならNotion登録をスキップし、Slack投稿のみ行う）。web-engagement-toolの
# notionSync.tsが読んでいるのと同じ「【営業部】お問合せリード管理」DBへ書き込む前提の値
# （同じNotion integration・同じDB IDを共有する）。
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_INQUIRY_DATABASE_ID = os.environ.get("NOTION_INQUIRY_DATABASE_ID")
# 任意（未設定ならCRM連絡先への同期をスキップ）。crm-sfa-integration側の
# POST /api/webhooks/lead-inquiry（連絡先DBへのfind-or-create、db_key=contact）。
CRM_SFA_LEAD_INQUIRY_WEBHOOK_URL = os.environ.get("CRM_SFA_LEAD_INQUIRY_WEBHOOK_URL")
CRM_SFA_LEAD_INQUIRY_WEBHOOK_SECRET = os.environ.get("CRM_SFA_LEAD_INQUIRY_WEBHOOK_SECRET")

app = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_API_KEY) if NOTION_API_KEY else None

PROCESSED_REACTION = "white_check_mark"
OWN_BOT_ID = app.client.auth_test()["bot_id"]

# web-engagement-tool/src/lib/notionSync.tsのPROPと同じプロパティ名（あちら側が
# pullFromNotion()でこのDBのプロパティだけを読む——ページ本文は読まないため、リサーチ結果は
# 「補足」プロパティに入れる必要がある）。
NOTION_PROP = {
    "name": "名前",
    "company": "会社名",
    "email": "メールアドレス",
    "phone": "電話番号",
    "supplement": "補足",
    "received_at": "受信日時",
}


def get_email_file(event):
    """メールファイルがあれば本文テキストを返す。なければNone。"""
    for f in event.get("files", []):
        if f.get("filetype") == "email" or f.get("mimetype", "").startswith("text"):
            raw = requests.get(
                f["url_private_download"],
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                timeout=10,
            ).text
            if f.get("mimetype") == "text/html":
                return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
            return raw
    return None


def _response_text(msg):
    """Claudeのレスポンスからテキストだけを連結して返す。

    thinkingが有効なモデル（Sonnet 5等はthinking省略時にadaptiveでONになる）では
    content[0]がThinkingBlockになるため、content[0].text決め打ちにはしない。
    """
    return "".join(b.text for b in msg.content if b.type == "text")


def extract_lead_with_claude(email_text):
    """Claudeでリード情報を抽出する。テスト・無効な場合はNoneを返す。"""
    msg = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""以下のメール本文からリード情報を抽出してください。

{email_text[:3000]}

以下のJSON形式で返してください（該当情報がない場合は空文字）:
{{
  "company": "会社名・施設名",
  "name": "担当者名",
  "email": "メールアドレス",
  "phone": "電話番号",
  "doc": "資料名またはお問い合わせ種別"
}}

会社名が「テスト」や空の場合、またはリード情報が含まれていない場合は null を返してください。"""
        }]
    )
    import json
    try:
        text = _response_text(msg).strip()
        if text == "null":
            return None
        # コードブロックを除去
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception:
        return None


def fetch_site(domain):
    for url in [f"https://{domain}", f"https://www.{domain}"]:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                texts = [t.get_text(" ", strip=True)
                         for t in soup.find_all(["title", "h1", "h2", "h3", "p", "address", "footer"])]
                return "\n".join(texts)[:5000]
        except Exception:
            continue
    return ""


def search_address(company_name):
    """Google検索で住所・所在地を取得する"""
    try:
        r = requests.get(
            "https://www.google.com/search",
            params={"q": f"{company_name} 住所 所在地", "num": 3},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        # 検索結果のスニペットを抽出
        snippets = [s.get_text(" ", strip=True) for s in soup.select(".VwiC3b, .yDYNvb, span")]
        return "\n".join(snippets)[:1000]
    except Exception:
        return ""


def search_urls(company_name):
    targets = {
        "instagram": ("instagram.com",       f"{company_name} site:instagram.com"),
        "rakuten":   ("travel.rakuten.co.jp", f"{company_name} site:travel.rakuten.co.jp"),
        "jalan":     ("jalan.net",            f"{company_name} site:jalan.net"),
        "booking":   ("booking.com",          f"{company_name} site:booking.com"),
    }
    results = {}
    for key, (domain, query) in targets.items():
        try:
            r = requests.get(
                "https://www.google.com/search",
                params={"q": query, "num": 3},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href]"):
                href = a["href"]
                if "/url?q=" in href:
                    url = href.split("/url?q=")[1].split("&")[0]
                    if domain in url:
                        results[key] = url
                        break
        except Exception:
            pass
    return results


def build_summary(lead, site_content, urls, address_hints=""):
    msg = claude.messages.create(
        model="claude-sonnet-5",
        # Sonnet 5はthinking省略時にadaptiveでONになる。決まった書式へ整形するだけの
        # 工程で思考は不要なうえ、max_tokensを思考トークンが食うため明示的に切る。
        thinking={"type": "disabled"},
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""ホテル・旅館の営業担当向けに企業調査サマリーを作成してください。

リード情報:
- 会社名: {lead.get('company', '不明')}
- 担当者: {lead.get('name', '不明')}
- メール: {lead.get('email', '')}
- 電話: {lead.get('phone', '')}
- 問い合わせ種別: {lead.get('doc', '')}

企業サイト内容:
{site_content or '（取得できませんでした）'}

住所検索結果:
{address_hints or '（取得できませんでした）'}

以下の形式で出力してください（情報がない場合は「なし」）:

• **所在地**:
• **業種・事業内容**:
• **規模感**:
• **最近のニュースやトピック**:
• **公式HP URL**: {urls.get('hp', 'なし')}
• **Instagram URL**: {urls.get('instagram', 'なし')}
• **公式LINE URL**: {urls.get('line', 'なし')}
• **楽天トラベルURL**: {urls.get('rakuten', 'なし')}
• **じゃらんネットURL**: {urls.get('jalan', 'なし')}
• **booking.com URL**: {urls.get('booking', 'なし')}"""
        }]
    )
    return _response_text(msg)


# Notion rich_textプロパティの1テキストオブジェクトあたりの上限（Notion API仕様）。
_NOTION_RICH_TEXT_MAX_LEN = 2000


def create_notion_inquiry_page(lead, summary):
    """企業調査サマリーを「補足」プロパティとして【営業部】お問合せリード管理DBへ登録する。

    web-engagement-tool側(`src/lib/notionSync.ts`)が同じDBを毎日プルしてLead/LeadInquiryへ
    取り込む（プロパティのみ参照、ページ本文は見ない）ため、リサーチ結果は本文ではなく
    「補足」プロパティに入れる必要がある。NOTION_API_KEY/NOTION_INQUIRY_DATABASE_ID未設定、
    またはNotion API呼び出し失敗時は、既存のSlack投稿フローを止めないよう例外を送出せず
    ログのみ出力してスキップする。
    """
    if notion is None or not NOTION_INQUIRY_DATABASE_ID:
        print("Notion登録スキップ: NOTION_API_KEY/NOTION_INQUIRY_DATABASE_ID未設定", flush=True)
        return

    name = lead.get("name") or lead.get("company") or "(名前不明)"
    properties = {
        NOTION_PROP["name"]: {"title": [{"text": {"content": name}}]},
        NOTION_PROP["supplement"]: {
            "rich_text": [{"text": {"content": summary[:_NOTION_RICH_TEXT_MAX_LEN]}}]
        },
        NOTION_PROP["received_at"]: {"date": {"start": datetime.now(timezone.utc).isoformat()}},
    }
    if lead.get("company"):
        properties[NOTION_PROP["company"]] = {
            "rich_text": [{"text": {"content": lead["company"]}}]
        }
    if lead.get("email"):
        properties[NOTION_PROP["email"]] = {"email": lead["email"]}
    if lead.get("phone"):
        properties[NOTION_PROP["phone"]] = {"phone_number": lead["phone"]}

    try:
        notion.pages.create(
            parent={"database_id": NOTION_INQUIRY_DATABASE_ID},
            properties=properties,
        )
        print(f"Notion登録完了: {name}", flush=True)
    except Exception as e:
        print(f"Notion登録失敗: {name} ({e})", flush=True)


def sync_lead_to_crm(lead):
    """リード情報をcrm-sfa-integrationの連絡先DBへfind-or-createで同期する。

    未設定・失敗時は例外を送出せずログのみ出力し、既存のSlack投稿フローを止めない
    （`create_notion_inquiry_page`と同じフェイルセーフ方針）。
    """
    if not CRM_SFA_LEAD_INQUIRY_WEBHOOK_URL or not CRM_SFA_LEAD_INQUIRY_WEBHOOK_SECRET:
        print("CRM連絡先同期スキップ: CRM_SFA_LEAD_INQUIRY_WEBHOOK_URL/SECRET未設定", flush=True)
        return

    try:
        r = requests.post(
            CRM_SFA_LEAD_INQUIRY_WEBHOOK_URL,
            json={
                "company": lead.get("company", ""),
                "name": lead.get("name", ""),
                "email": lead.get("email", ""),
                "phone": lead.get("phone", ""),
            },
            headers={"X-Webhook-Secret": CRM_SFA_LEAD_INQUIRY_WEBHOOK_SECRET},
            timeout=10,
        )
        r.raise_for_status()
        print(f"CRM連絡先同期完了: {r.json()}", flush=True)
    except Exception as e:
        print(f"CRM連絡先同期失敗: {e}", flush=True)


@app.event("message")
def handle_lead(event, client, logger):
    # 自分自身の投稿はスキップ
    if event.get("bot_id") == OWN_BOT_ID:
        return

    # メールファイルがない場合はスキップ
    email_text = get_email_file(event)
    if email_text is None:
        return

    print(f"EMAIL RECEIVED: {email_text[:200]}", flush=True)

    ts = event["ts"]
    channel = event["channel"]

    # Claudeでリード情報を抽出
    lead = extract_lead_with_claude(email_text)
    if not lead:
        print("Skipped: not a lead or test", flush=True)
        return

    company = lead.get("company", "")
    print(f"Processing: {company}", flush=True)

    domain = lead.get("email", "").split("@")[-1] if "@" in lead.get("email", "") else ""
    site_content = fetch_site(domain) if domain else ""
    urls = search_urls(company)
    address_hints = search_address(company)
    summary = build_summary(lead, site_content, urls, address_hints)
    create_notion_inquiry_page(lead, summary)
    sync_lead_to_crm(lead)

    client.chat_postMessage(
        channel=channel,
        thread_ts=ts,
        text=(
            f"*{company}* 調査\n\n"
            f"*【リード情報】*\n```{email_text[:1000]}```\n\n"
            f"*【企業調査】*\n{summary}"
        ),
    )
    client.reactions_add(channel=channel, name=PROCESSED_REACTION, timestamp=ts)
    print(f"Done: {company}", flush=True)


if __name__ == "__main__":
    print("Starting Slack Lead Bot...", flush=True)
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
