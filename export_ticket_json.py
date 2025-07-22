#!/usr/bin/env python3
"""
Export SupportPal tickets (comments + attachments) to JSON format
with dates formatted as "dd/MMM/yy h:mm a"
"""

import mysql.connector
import json
import os
import sys
import math
import paramiko
import datetime as dt
from tqdm import tqdm
import configparser

# ————————————————— LOAD CONFIG —————————————————
config = configparser.ConfigParser()
config.read('config.ini')
cfg = config['DEFAULT']

# MySQL
mysql_conf = {
    'host':     cfg['MYSQL_HOST'],
    'port':     cfg.getint('MYSQL_PORT'),
    'database': cfg['MYSQL_DB'],
    'user':     cfg['MYSQL_USER'],
    'password': cfg['MYSQL_PASSWORD'],
    'charset':  cfg['MYSQL_CHARSET'],
}

# SSH / SFTP
ssh_conf = {
    'hostname': cfg['SSH_HOST'],
    'port':     cfg.getint('SSH_PORT'),
    'username': cfg['SSH_USER'],
    'password': cfg['SSH_PASSWORD'],
}

# Jira (available if you extend this script later)
JIRA_URL        = cfg['JIRA_URL']
JIRA_USER       = cfg['JIRA_USER']
JIRA_API_TOKEN  = cfg['JIRA_API_TOKEN']
JIRA_PROJECT_KEY= cfg['JIRA_PROJECT']
JIRA_ISSUETYPE  = cfg['JIRA_ISSUETYPE']

# Attachments & folders
remote_attachment_path = cfg['REMOTE_ATTACHMENT_PATH']

# Your config gives a single path for attachments, e.g.
# "SupportPal to Jira/attachments", so we split that into:
BASE_FOLDER        = os.path.dirname(cfg['LOCAL_ATTACHMENTS_DIR'])
ATTACHMENTS_FOLDER = cfg['LOCAL_ATTACHMENTS_DIR']

# Priority mapping (if you need it downstream)
PRIORITY_MAP = {
    1: cfg['PRIORITY_MAP_1'],
    2: cfg['PRIORITY_MAP_2'],
    3: cfg['PRIORITY_MAP_3'],
    4: cfg['PRIORITY_MAP_4'],
}

# Batch size (fallback to 1500 if not set)
MAX_PER_BATCH = cfg.getint('MAX_PER_BATCH', fallback=1500)
# ———————————————————————————————————————————————————

def parse_any_datetime(value):
    """Convert any supported format to datetime object."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value)
    s = str(value).strip()
    if s.isdigit():
        return dt.datetime.fromtimestamp(int(s))
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        raise ValueError(f"Cannot parse datetime: {s}")

def format_custom_datetime(dt_obj):
    """Format as dd/MMM/yy h:mm a"""
    if not isinstance(dt_obj, dt.datetime):
        return ''
    date_part = dt_obj.strftime('%d/%b/%y')
    time_part = dt_obj.strftime('%I:%M %p').lstrip('0')
    return f"{date_part} {time_part}"

# 1. Connect to MySQL
try:
    conn = mysql.connector.connect(**mysql_conf)
except mysql.connector.errors.InterfaceError as e:
    sys.exit(f"❗ MySQL connection error: {e}")
cur = conn.cursor(dictionary=True)

# 2. Fetch tickets & calculate batches
cur.execute("SELECT number, id FROM ticket ORDER BY id")
tickets   = cur.fetchall()
total     = len(tickets)
n_batches = math.ceil(total / MAX_PER_BATCH)
print(f"Found {total} tickets → splitting into {n_batches} JSON file(s)")

# 3. Prepare output folders
os.makedirs(BASE_FOLDER,        exist_ok=True)
os.makedirs(ATTACHMENTS_FOLDER, exist_ok=True)

# 4. Establish SFTP
ssh  = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(**ssh_conf)
sftp = ssh.open_sftp()

# 5. Process tickets in batches
batch_items = [[] for _ in range(n_batches)]
batch_users = [set() for _ in range(n_batches)]

for idx, tk in enumerate(tqdm(tickets, desc='Processing', unit='ticket'), start=1):
    batch_no   = (idx - 1) // MAX_PER_BATCH
    ticket_num = tk['number']
    ticket_id  = tk['id']
    jira_key   = f"{JIRA_PROJECT_KEY}-{ticket_num}"

    # --- Fetch messages ---
    cur.execute("""
        SELECT id AS msg_id, created_at, user_name, text
        FROM ticket_message
        WHERE ticket_id=%s
        ORDER BY created_at
    """, (ticket_id,))
    msgs = cur.fetchall()
    if not msgs:
        continue

    reporter    = msgs[0]['user_name'] or 'Unknown'
    summary     = f"SupportPal Ticket #{ticket_num}"
    description = (msgs[0]['text'] or '').strip()

    comments = []
    for m in msgs:
        created_dt  = parse_any_datetime(m['created_at'])
        created_fmt = format_custom_datetime(created_dt)
        author      = m['user_name'] or 'Unknown'
        body        = (m['text'] or '').replace('
', '
')

        comments.append({
            "body":    body,
            "author":  author,
            "created": created_fmt
        })
        batch_users[batch_no].add(author)

    # --- Fetch & download attachments ---
    cur.execute("""
        SELECT upload_hash, original_name, created_at
        FROM ticket_attachment
        WHERE ticket_id=%s
    """, (ticket_id,))
    attachments = []
    for att in cur.fetchall():
        remote_file = f"{remote_attachment_path}/{att['upload_hash']}"
        local_dir   = os.path.join(ATTACHMENTS_FOLDER, str(ticket_id))
        os.makedirs(local_dir, exist_ok=True)
        local_file  = os.path.join(local_dir, att['original_name'])

        try:
            sftp.stat(remote_file)
            sftp.get(remote_file, local_file)
        except FileNotFoundError:
            tqdm.write(f"⚠️ Missing attachment {remote_file}")
            continue

        att_created = format_custom_datetime(
            parse_any_datetime(att.get('created_at') or dt.datetime.utcnow())
        )
        attachments.append({
            "name":     att['original_name'],
            "attacher": reporter,
            "created":  att_created,
            "uri":      os.path.relpath(local_file, BASE_FOLDER)
        })

    # --- Build work item ---
    created_fmt = comments[0]["created"]
    updated_fmt = comments[-1]["created"]

    work_item = {
        "key":         jira_key,
        "status":      "Open",
        "reporter":    reporter,
        "summary":     summary,
        "description": description,
        "externalId":  str(ticket_id),
        "created":     created_fmt,
        "updated":     updated_fmt,
        "comments":    comments,
    }
    if attachments:
        work_item["attachments"] = attachments

    batch_items[batch_no].append(work_item)

# 6. Write out JSON files
for i in range(n_batches):
    users = [{"name": u} for u in sorted(batch_users[i])]
    data = {
        "users": users,
        "projects": [{
            "name":        PROJECT_NAME,      # still pulled from config if you add it
            "key":         JIRA_PROJECT_KEY,
            "work items":  batch_items[i]
        }]
    }
    fname = os.path.join(BASE_FOLDER, f"tickets_batch_{i+1}.json")
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✓ Wrote {len(batch_items[i])} tickets → {fname}")

# 7. Cleanup
sftp.close()
ssh.close()
cur.close()
conn.close()
print(f"\n✅ Done. JSON files & attachments are in “{BASE_FOLDER}/”.")
