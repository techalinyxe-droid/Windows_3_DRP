import os
import json
import re
import threading
import time
from datetime import datetime
import requests
import boto3
import telebot
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
AUTHORIZED_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", 0))
NOTIFY_TOKEN = os.environ.get("NOTIFY_TOKEN", "super-secret-token")

GITHUB_OWNER = os.environ.get("GITHUB_OWNER")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GH_TOKEN = os.environ.get("GH_TOKEN")

# Paramètres Tailscale
TS_TAILNET = os.environ.get("TS_TAILNET")
TS_AUTHKEY = os.environ.get("TS_AUTHKEY")
TS_API_KEY = os.environ.get("TS_API_KEY")

B2_APPLICATION_KEY_ID = os.environ.get("B2_APPLICATION_KEY_ID")
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY")
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME")
B2_ENDPOINT = os.environ.get("B2_ENDPOINT")

COOLDOWN_MINUTES = 10
APPS_KEY = "apps.txt"

# Clients
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
llm_client = OpenAI(base_url="https://router.huggingface.co/v1", api_key=HF_TOKEN)
b2_client = boto3.client('s3', endpoint_url=f"https://{B2_ENDPOINT}",
                         aws_access_key_id=B2_APPLICATION_KEY_ID,
                         aws_secret_access_key=B2_APPLICATION_KEY)

# ==========================================
# OUTILS (B2 / GITHUB / COOLDOWN)
# ==========================================

def get_b2_file_content(key):
    try:
        obj = b2_client.get_object(Bucket=B2_BUCKET_NAME, Key=key)
        return obj['Body'].read().decode('utf-8')
    except: return ""

def check_cooldown():
    try:
        response = b2_client.head_object(Bucket=B2_BUCKET_NAME, Key='cooldown.txt')
        diff = (datetime.utcnow() - response['LastModified'].replace(tzinfo=None)).total_seconds() / 60
        return (False, int(COOLDOWN_MINUTES - diff)) if diff < COOLDOWN_MINUTES else (True, 0)
    except: return True, 0

def get_workflow_status(workflow_id):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{workflow_id}/runs?per_page=1"
    headers = {"Authorization": f"Bearer {GH_TOKEN}"}
    try:
        res = requests.get(url, headers=headers).json()
        if "workflow_runs" in res and res["workflow_runs"]:
            run = res["workflow_runs"][0]
            return f"{run['status']} ({run['conclusion'] or 'en cours'})"
    except: pass
    return "inconnu"

def trigger_workflow(workflow_id):
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    url_check = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{workflow_id}/runs?status=in_progress"
    
    if requests.get(url_check, headers=headers).json().get("total_count", 0) > 0:
        return "⚠️ Ce workflow est déjà en cours."
    
    ok, mins = check_cooldown()
    if not ok: return f"⏳ Cooldown actif : attends encore {mins} min."

    url_dispatch = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{workflow_id}/dispatches"
    inputs = {
        "session_id": str(int(time.time())),
        "runtime_minutes": "355",
        "ts_tailnet": TS_TAILNET,
        "ts_authkey": TS_AUTHKEY,
        "ts_api_key": TS_API_KEY
    }
    res = requests.post(url_dispatch, headers=headers, json={"ref": "main", "inputs": inputs})
    if res.status_code == 204:
        b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='cooldown.txt', Body='updated')
        return f"🚀 Démarrage réussi de {workflow_id} !"
    return f"❌ Erreur GitHub : {res.status_code}"

# ==========================================
# COMMANDES TELEGRAM (HANDLERS)
# ==========================================

@bot.message_handler(func=lambda m: m.chat.id != AUTHORIZED_CHAT_ID)
def block_unauthorized(m): bot.reply_to(m, "⛔ Accès refusé.")

@bot.message_handler(commands=['help'])
def h_help(m):
    text = ("🤖 **Aide Bullet**\n\n"
            "/start - Session complète\n"
            "/restart - Redémarrage rapide\n"
            "/stop - Arrêt propre (B2 lock)\n"
            "/status - État des workflows\n"
            "/files [dossier] - Liste B2\n"
            "/storage - Taille B2\n"
            "/apps - Liste des apps\n"
            "/addapp [nom] - Ajouter une app\n"
            "/removeapp [nom] - Retirer une app")
    bot.reply_to(m, text, parse_mode="Markdown")

@bot.message_handler(commands=['start'])
def h_start(m): bot.reply_to(m, trigger_workflow('rdp-tailscale-rustdesk-A.yml'))

@bot.message_handler(commands=['restart'])
def h_restart(m): bot.reply_to(m, trigger_workflow('restart.yml'))

@bot.message_handler(commands=['stop'])
def h_stop(m):
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='stop-now.lock', Body='stop')
    bot.reply_to(m, "🛑 Signal d'arrêt envoyé (stop-now.lock).")

@bot.message_handler(commands=['status'])
def h_status(m):
    s_full = get_workflow_status('rdp-tailscale-rustdesk-A.yml')
    s_rest = get_workflow_status('restart.yml')
    bot.reply_to(m, f"📊 **Statut**\nFull: {s_full}\nRestart: {s_rest}", parse_mode="Markdown")

@bot.message_handler(commands=['files'])
def h_files(m):
    prefix = m.text.replace('/files', '').strip() or 'userdata/'
    res = b2_client.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=prefix)
    files = [f"📄 {obj['Key']} ({round(obj['Size']/1024, 1)} KB)" for obj in res.get('Contents', [])[:15]]
    bot.reply_to(m, "\n".join(files) if files else "📭 Aucun fichier.")

@bot.message_handler(commands=['storage'])
def h_storage(m):
    res = b2_client.list_objects_v2(Bucket=B2_BUCKET_NAME)
    size = sum(obj['Size'] for obj in res.get('Contents', [])) / (1024*1024)
    bot.reply_to(m, f"💾 Stockage B2 : {round(size, 2)} MB utilisé.")

@bot.message_handler(commands=['apps'])
def h_apps(m):
    content = get_b2_file_content(APPS_KEY)
    bot.reply_to(m, f"📋 Apps :\n{content or '(vide)'}")

@bot.message_handler(commands=['addapp'])
def h_addapp(m):
    app_name = m.text.replace('/addapp', '').strip()
    if not app_name: return bot.reply_to(m, "Précise le nom.")
    curr = get_b2_file_content(APPS_KEY)
    new_content = f"{curr}\n{app_name}".strip()
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key=APPS_KEY, Body=new_content)
    bot.reply_to(m, f"✅ Ajouté : {app_name}")

@bot.message_handler(commands=['removeapp'])
def h_removeapp(m):
    app_name = m.text.replace('/removeapp', '').strip()
    if not app_name: return bot.reply_to(m, "Précise le nom.")
    curr = get_b2_file_content(APPS_KEY)
    lines = [line.strip() for line in curr.split('\n') if line.strip() and line.strip() != app_name]
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key=APPS_KEY, Body='\n'.join(lines))
    bot.reply_to(m, f"🗑️ Retiré : {app_name}")

# ==========================================
# IA CONVERSATIONNELLE
# ==========================================

@bot.message_handler(func=lambda message: True)
def handle_ai(message):
    bot.send_chat_action(message.chat.id, 'typing')
    status_info = f"Full: {get_workflow_status('rdp-tailscale-rustdesk-A.yml')}, Restart: {get_workflow_status('restart.yml')}"
    
    system_prompt = f"""Tu es l'IA du serveur "Bullet". 
    Statut actuel: {status_info}.
    Actions possibles via JSON en fin de message:
    {{"action": "start_full" | "start_restart" | "stop_session" | "get_status"}}
    Réponds brièvement en français."""

    try:
        response = llm_client.chat.completions.create(
            model="deepseek-ai/DeepSeek-V4-Flash:novita",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": message.text}]
        )
        content = response.choices[0].message.content
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            action_data = json.loads(match.group())
            act = action_data.get("action")
            if act == "start_full": bot.send_message(message.chat.id, trigger_workflow('rdp-tailscale-rustdesk-A.yml'))
            elif act == "start_restart": bot.send_message(message.chat.id, trigger_workflow('restart.yml'))
            elif act == "stop_session": h_stop(message)
            elif act == "get_status": h_status(message)
            content = content[:match.start()].strip()
        if content: bot.send_message(message.chat.id, content)
    except: bot.reply_to(message, "L'IA ne répond pas. Utilise les commandes /.")

# ==========================================
# FLASK (NOTIFY & HEALTH)
# ==========================================

@app.route('/notify', methods=['POST'])
def notify():
    token = request.headers.get('Authorization')
    if token != f"Bearer {NOTIFY_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json
    event = data.get("event", "Notification")
    info = data.get("data", "")
    
    try:
        ai_res = llm_client.chat.completions.create(
            model="deepseek-ai/DeepSeek-V4-Flash:novita",
            messages=[{"role": "user", "content": f"Reformule gentiment : {event} - {info}"}]
        )
        msg = f"🔔 {ai_res.choices[0].message.content}"
    except: msg = f"🔔 {event}: {info}"
    
    bot.send_message(AUTHORIZED_CHAT_ID, msg)
    return "OK", 200

@app.route('/health')
def health(): return "OK", 200

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000))), daemon=True).start()
    bot.infinity_polling()
