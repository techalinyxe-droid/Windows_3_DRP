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

# Modèles IA disponibles
AVAILABLE_MODELS = [
    "deepseek-ai/DeepSeek-V4-Flash:novita",
    "mistralai/Mixtral-8x7B-Instruct-v0.1",
    "mistralai/Mistral-7B-Instruct-v0.2",
    "meta-llama/Meta-Llama-3.1-8B-Instruct",
    "Qwen/Qwen2.5-7B-Instruct"
]
current_model = os.environ.get("HF_MODEL", AVAILABLE_MODELS[0])

# ------------------------------------------------------------------
# AJOUT : Bibliothèque des 16 scripts maîtres (templates PowerShell)
# ------------------------------------------------------------------
SCRIPTS_LIBRARY = {
    # Analyse
    "s_anal_disk": "Get-ChildItem -Path 'C:/{arg}' -Recurse -ErrorAction SilentlyContinue | Sort-Object Length -Descending | Select-Object Name, @{{Name='Size(MB)';Expression={{[Math]::Round($_.Length / 1MB, 2)}}}} -First 15",
    "s_anal_perf": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 | Select-Object ProcessName, @{{Name='CPU(%)';Expression={{[Math]::Round($_.CPU, 1)}}}}, @{{Name='RAM(MB)';Expression={{[Math]::Round($_.WorkingSet / 1MB, 1)}}}}",
    "s_anal_net": "Test-NetConnection -ComputerName 8.8.8.8; Get-NetTCPConnection | Where-Object {{$_.State -eq 'Established'}} | Select-Object LocalAddress, LocalPort, RemoteAddress, RemotePort",
    "s_anal_ia": "Select-String -Path 'C:/Users/$env:RDP_USER/Desktop/*.log' -Pattern '{arg}' -Context 2,2 | Select-Object -Last 10",

    # Fichiers
    "s_get_file": "& $env:RCLONE_EXE copy 'C:/Users/$env:RDP_USER/{arg}' 'myb2:$env:B2_BUCKET/outputs/' --progress",
    "s_dl_b2": "& $env:RCLONE_EXE copy 'myb2:$env:B2_BUCKET/{arg}' 'C:/Users/$env:RDP_USER/Downloads/' --force",
    "s_zip_folder": "Compress-Archive -Path 'C:/Users/$env:RDP_USER/{arg}' -DestinationPath 'C:/temp/archive.zip' -Force; & $env:RCLONE_EXE move 'C:/temp/archive.zip' 'myb2:$env:B2_BUCKET/outputs/'",
    "s_search": "Get-ChildItem -Path 'C:/Users/$env:RDP_USER/' -Filter '*{arg}*' -Recurse -ErrorAction SilentlyContinue | Select-Object FullName",

    # Système & Réparation
    "s_fix_pip": "python -m pip install --upgrade pip; pip install {arg} --force-reinstall",
    "s_kill_task": "Stop-Process -Name '{arg}' -Force -ErrorAction SilentlyContinue",
    "s_sys_info": "Get-ComputerInfo | Select-Object OsName, OsVersion, CsProcessors, @{{Name='Uptime';Expression={{(Get-Date) - (Get-Uptime)}}}}",
    "s_clean_tmp": "Remove-Item -Path 'C:/Users/$env:RDP_USER/AppData/Local/Temp/*' -Recurse -Force; Write-Host 'Nettoyage Temp terminé.'",

    # Automatisation
    "s_auto_run": "Start-Process python.exe -ArgumentList 'C:/Users/$env:RDP_USER/Desktop/{arg}'",
    "s_firewall": "netsh advfirewall firewall add rule name='Open_{arg}' dir=in action=allow protocol=TCP localport={arg}",
    "s_screenshot": "python -c 'import pyautogui; pyautogui.screenshot(\"C:/temp/screen.png\")'; & $env:RCLONE_EXE move 'C:/temp/screen.png' 'myb2:$env:B2_BUCKET/outputs/'",
    "s_health": "Write-Host '--- DISQUE ---'; Get-PSDrive C | Select-Object Used,Free; Write-Host '--- RAM ---'; Get-WmiObject Win32_OperatingSystem | Select-Object FreePhysicalMemory; Write-Host '--- SERVICES ---'; Get-Service | Where-Object {{$_.Status -eq 'Running'}} | Select-Object -First 5"
}

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
        "runtime_minutes": "355",
        "ts_tailnet": TS_TAILNET,
        "ts_authkey": TS_AUTHKEY,
        "ts_api_key": TS_API_KEY
    }
    
    res = requests.post(url_dispatch, headers=headers, json={"ref": "main", "inputs": inputs})
    if res.status_code == 204:
        b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='cooldown.txt', Body='updated')
        return f"🚀 Démarrage réussi de {workflow_id} !"
    
    try:
        detail = res.json().get("message", res.text)
    except:
        detail = res.text
    return f"❌ Erreur GitHub {res.status_code} : {detail}"

# ==========================================
# COMMANDES TELEGRAM (HANDLERS)
# ==========================================

@bot.message_handler(func=lambda m: m.chat.id != AUTHORIZED_CHAT_ID)
def block_unauthorized(m): bot.reply_to(m, "⛔ Accès refusé.")

@bot.message_handler(commands=['help'])
def h_help(m):
    text = ("🤖 **Aide Bullet**\n\n"
            "*Contrôle Session*\n"
            "/start - Session complète\n"
            "/restart - Redémarrage rapide\n"
            "/stop - Arrêt propre (B2 lock)\n"
            "/status - État des workflows\n\n"
            "*Pilotage à distance (Core-Engine)*\n"
            "/cmd <commande> - Exécute du PowerShell\n"
            "/stats - CPU/RAM en direct\n"
            "/get <fichier> - Récupère un fichier\n"
            "/fix - Répare dépendances Python\n"
            "/screen - Capture d'écran\n\n"
            "*Scripts prédéfinis*\n"
            "/s_list - Liste des scripts\n"
            "/s_nom_script [arg] - Lancer un script\n\n"
            "*Stockage & Apps*\n"
            "/files [dossier] - Liste B2\n"
            "/storage - Taille B2\n"
            "/apps - Liste des apps\n"
            "/addapp [nom] - Ajouter une app\n"
            "/removeapp [nom] - Retirer une app\n\n"
            "*Modèles IA*\n"
            "/models - Lister les modèles disponibles\n"
            "/model [nom/num] - Changer de modèle\n\n"
            "*Système*\n"
            "/settings - Configuration et état")
    bot.reply_to(m, text, parse_mode="Markdown")

@bot.message_handler(commands=['models'])
def h_models(m):
    models_list = []
    for i, model_name in enumerate(AVAILABLE_MODELS, 1):
        prefix = "✅" if model_name == current_model else "  "
        models_list.append(f"{prefix} {i}. `{model_name}`")
    
    msg = "*Modèles disponibles :*\n" + "\n".join(models_list)
    msg += "\n\n_Utilisez /model <numéro> ou /model <nom> pour changer._"
    bot.reply_to(m, msg, parse_mode="Markdown")

@bot.message_handler(commands=['model'])
def h_model(m):
    global current_model
    arg = m.text.replace('/model', '').strip()
    
    if not arg:
        h_models(m)
        return
    
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(AVAILABLE_MODELS):
            current_model = AVAILABLE_MODELS[idx]
            os.environ["HF_MODEL"] = current_model
            bot.reply_to(m, f"✅ Modèle changé pour : `{current_model}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, f"❌ Numéro invalide. Choisis entre 1 et {len(AVAILABLE_MODELS)}.")
        return
    
    for model_name in AVAILABLE_MODELS:
        if arg.lower() in model_name.lower():
            current_model = model_name
            os.environ["HF_MODEL"] = current_model
            bot.reply_to(m, f"✅ Modèle changé pour : `{current_model}`", parse_mode="Markdown")
            return
            
    bot.reply_to(m, f"❌ Modèle non trouvé. Utilise /models pour voir la liste.")

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

# ------------------------------------------------------------------
# NOUVELLES COMMANDES DE PILOTAGE DU CORE-ENGINE (CMD, STATS, GET, FIX, SCREEN)
# ------------------------------------------------------------------
@bot.message_handler(commands=['cmd'])
def h_cmd(m):
    ps_command = m.text.replace('/cmd', '').strip()
    if not ps_command:
        bot.reply_to(m, "❌ Utilisation : `/cmd [votre commande]`")
        return
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='cmd.ps1', Body=ps_command)
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='cmd.lock', Body='lock')
    bot.reply_to(m, f"💻 Commande PowerShell envoyée au serveur.")

@bot.message_handler(commands=['stats'])
def h_stats(m):
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='stats.lock', Body='lock')
    bot.reply_to(m, "📊 Demande de stats (CPU/RAM) envoyée.")

@bot.message_handler(commands=['get'])
def h_get(m):
    filename = m.text.replace('/get', '').strip()
    if not filename:
        bot.reply_to(m, "❌ Utilisation : `/get nom_du_fichier.ext`")
        return
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='get.txt', Body=filename)
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='get.lock', Body='lock')
    bot.reply_to(m, f"📂 Ordre de récupération pour `{filename}` envoyé.")

@bot.message_handler(commands=['fix'])
def h_fix(m):
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='fix.lock', Body='lock')
    bot.reply_to(m, "🔧 Commande de réparation Python/Pip envoyée.")

@bot.message_handler(commands=['screen'])
def h_screen(m):
    b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='screen.lock', Body='lock')
    bot.reply_to(m, "📸 Capture d'écran demandée.")

# ------------------------------------------------------------------
# COMMANDES DE SCRIPTS (BIBLIOTHÈQUE /s_...)
# ------------------------------------------------------------------
@bot.message_handler(commands=['s_list'])
def h_s_list(m):
    scripts = "\n".join([f"• `/{name}`" for name in SCRIPTS_LIBRARY.keys()])
    bot.reply_to(m, f"📜 **Bibliothèque de Scripts :**\n\n{scripts}\n\n_Usage: /s_nom_du_script [ton_argument]_", parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text.startswith('/s_'))
def execute_script_engine(m):
    parts = m.text.split(' ', 1)
    cmd_name = parts[0].replace('/', '')
    argument = parts[1] if len(parts) > 1 else ""

    if cmd_name in SCRIPTS_LIBRARY:
        # Si le script demande un argument et qu'il est vide
        if "{arg}" in SCRIPTS_LIBRARY[cmd_name] and not argument:
            bot.reply_to(m, f"⚠️ Ce script demande un argument.\nExemple : `/{cmd_name} mon_texte_ou_chemin`")
            return

        # Préparation du PowerShell final
        ps_code = SCRIPTS_LIBRARY[cmd_name].replace("{arg}", argument)
        
        try:
            b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='cmd.ps1', Body=ps_code)
            b2_client.put_object(Bucket=B2_BUCKET_NAME, Key='cmd.lock', Body='lock')
            bot.reply_to(m, f"✅ **Ordre envoyé au Core-Engine**\nScript : `{cmd_name}`\nArg : `{argument}`\n\n_Attends la notification du résultat..._")
        except Exception as e:
            bot.reply_to(m, f"❌ Erreur B2 : {e}")
    else:
        bot.reply_to(m, "❌ Ce script n'existe pas dans la bibliothèque.")

# ------------------------------------------------------------------
# COMMANDE /settings (vision globale)
# ------------------------------------------------------------------
@bot.message_handler(commands=['settings'])
def h_settings(m):
    status_engine = "🟢 Actif" if "in_progress" in get_workflow_status('core-engine.yml') else "🔴 Hors-ligne"
    text = (
        "⚙️ **CONFIGURATION BULLET ULTIMA**\n\n"
        f"🚀 **Statut Core Engine :** {status_engine}\n"
        f"📂 **Bucket B2 :** `{B2_BUCKET_NAME}`\n"
        f"⏱️ **Runtime :** `355 min`\n"
        f"⏳ **Cooldown :** `{COOLDOWN_MINUTES} min`\n\n"
        "📜 **Scripts de Contrôle Prêts :**\n"
        "└ _Analyse, Fichiers, Système, Auto_\n\n"
        "💡 _Utilisez /s_list pour voir toutes les commandes de scripts._"
    )
    bot.reply_to(m, text, parse_mode="Markdown")

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
            model=current_model,
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
    except Exception as e: 
        bot.reply_to(message, f"L'IA ({current_model}) ne répond pas ou a rencontré une erreur. Utilise les commandes /.")

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
            model=current_model,
            messages=[{"role": "user", "content": f"Reformule gentiment en une courte phrase pour Telegram: {event} - {info}"}]
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
