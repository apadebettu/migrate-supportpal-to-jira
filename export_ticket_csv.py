#!/usr/bin/env python3
import configparser
import mysql.connector
import csv
import os
import sys
import datetime
from sshtunnel import SSHTunnelForwarder
from bs4 import BeautifulSoup
import paramiko
from tqdm import tqdm

# ——————————————
# 0) Load config.ini
# ——————————————
config = configparser.ConfigParser()
config.read('config.ini')
cfg = config['DEFAULT']

# ——————————————
# 1) Config from config.ini
# ——————————————
mysql_conf = {
    'host':    cfg['MYSQL_HOST'],
    'port':    cfg.getint('MYSQL_PORT'),
    'database':cfg['MYSQL_DB'],
    'user':    cfg['MYSQL_USER'],
    'password':cfg['MYSQL_PASSWORD'],
    'charset': cfg['MYSQL_CHARSET'],
}
ssh_conf = {
    'hostname': cfg['SSH_HOST'],
    'port':     cfg.getint('SSH_PORT'),
    'username': cfg['SSH_USER'],
    'password': cfg['SSH_PASSWORD'],
}
JIRA_PROJECT_KEY = cfg['JIRA_PROJECT']
REMOTE_ATTACHMENT_PATH = cfg['REMOTE_ATTACHMENT_PATH']
LOCAL_ATTACHMENTS_DIR  = cfg['LOCAL_ATTACHMENTS_DIR']

def strip_html(html):
    return BeautifulSoup(html or "", "html.parser").get_text()

# ——————————————
# 2) Start SSH tunnel (to MySQL)
# ——————————————
tunnel = SSHTunnelForwarder(
    (ssh_conf['hostname'], ssh_conf['port']),
    ssh_username=ssh_conf['username'],
    ssh_password=ssh_conf['password'],
    remote_bind_address=('127.0.0.1', 3306),
    local_bind_address = ('127.0.0.1', mysql_conf['port'])
)
tunnel.start()

# ——————————————
# 3) Connect to MySQL
# ——————————————
try:
    conn = mysql.connector.connect(**mysql_conf)
except mysql.connector.errors.InterfaceError as e:
    tunnel.stop()
    sys.exit(f"❗ MySQL connection error: {e}")
cursor = conn.cursor(dictionary=True)

# ——————————————
# 4) Fetch all tickets (or just one for testing)
# ——————————————
print("\nExport mode selection:")
print("  [1] Export ALL tickets")
print("  [2] Export a SINGLE ticket (for testing)")
choice = input("Choose 1 or 2: ").strip()

if choice == "2":
    single = input("Enter the ticket number to export: ").strip()
    cursor.execute("SELECT number, id FROM ticket WHERE number = %s", (single,))
else:
    cursor.execute("SELECT number, id FROM ticket ORDER BY id")

tickets = cursor.fetchall()
total   = len(tickets)
print(f"Found {total} tickets to export.")

# ——————————————
# 5) Download attachments via SFTP
# ——————————————
os.makedirs(LOCAL_ATTACHMENTS_DIR, exist_ok=True)
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(**ssh_conf)
sftp = ssh.open_sftp()

for t in tqdm(tickets, desc="Downloading attachments", unit="ticket"):
    tid = t['id']
    folder = os.path.join(LOCAL_ATTACHMENTS_DIR, str(tid))
    os.makedirs(folder, exist_ok=True)
    cursor.execute(
        "SELECT upload_hash, original_name FROM ticket_attachment WHERE ticket_id = %s",
        (tid,)
    )
    for att in cursor.fetchall():
        remote = f"{REMOTE_ATTACHMENT_PATH}/{att['upload_hash']}"
        local  = os.path.join(folder, att['original_name'])
        try:
            sftp.stat(remote)
            sftp.get(remote, local)
        except FileNotFoundError:
            tqdm.write(f"⚠️ Missing: {remote}")
        except Exception as e:
            tqdm.write(f"⚠️ Error: {e}")

sftp.close()
ssh.close()

# ——————————————
# 6) Build in‑memory structure and find max comments
# ——————————————
all_data = []
max_comments = 0

for t in tickets:
    num, tid = t['number'], t['id']
    key = f"{JIRA_PROJECT_KEY}-{num}"

    # fetch messages
    cursor.execute("""
      SELECT id, created_at, user_name, text
      FROM ticket_message
      WHERE ticket_id = %s
      ORDER BY created_at
    """, (tid,))
    msgs = cursor.fetchall()
    if len(msgs) > max_comments:
        max_comments = len(msgs)

    # list local attachment paths
    att_folder = os.path.join(LOCAL_ATTACHMENTS_DIR, str(tid))
    paths = []
    if os.path.isdir(att_folder):
        for fn in os.listdir(att_folder):
            abs_path = os.path.abspath(os.path.join(att_folder, fn))
            paths.append(f"file://{abs_path}")

    all_data.append({
        'key': key,
        'messages': msgs,
        'attachments': paths
    })

# ——————————————
# 7) Write a single CSV for Jira import
# ——————————————
csv_path = os.path.join(os.path.dirname(LOCAL_ATTACHMENTS_DIR), "jira_import.csv")
fieldnames = ['Issue Key']
for i in range(1, max_comments + 1):
    fieldnames += [
      f'Comment Created {i}',
      f'Comment Author {i}',
      f'Comment Body {i}',
    ]
fieldnames.append('Attachments')

with open(csv_path, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(
        f,
        fieldnames=fieldnames,
        delimiter=',',
        quotechar='"',
        quoting=csv.QUOTE_ALL
    )
    writer.writeheader()

    for item in all_data:
        row = {'Issue Key': item['key']}
        # populate each comment slot
        for idx, m in enumerate(item['messages'], start=1):
            # normalize timestamp
            ts = m['created_at']
            if isinstance(ts, str):
                dt = datetime.datetime.fromisoformat(ts)
            else:
                dt = ts if isinstance(ts, datetime.datetime) else datetime.datetime.fromtimestamp(ts)
            ts_str = dt.strftime('%Y-%m-%d %H:%M')
            body = strip_html(m['text']).replace('\r\n', '\n')

            row[f'Comment Created {idx}'] = ts_str
            row[f'Comment Author {idx}']  = m['user_name'] or 'Unknown'
            row[f'Comment Body {idx}']    = body

        # attachments semicolon‑joined
        row['Attachments'] = ';'.join(item['attachments'])
        writer.writerow(row)

print(f"\n✅ Generated CSV ready for Jira import: {csv_path}")

# ——————————————
# 8) Tear down MySQL & SSH tunnel
# ——————————————
cursor.close()
conn.close()
tunnel.stop()
