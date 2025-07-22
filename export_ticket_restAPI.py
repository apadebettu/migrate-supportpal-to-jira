#!/usr/bin/env python3
import os
import sys
import re
import mysql.connector
import paramiko
from bs4 import BeautifulSoup
from jira import JIRA
from datetime import datetime
import configparser

# —————————————
# 1) Load CONFIG from config.ini
# —————————————
config = configparser.ConfigParser()
config.read('config.ini')
cfg = config['DEFAULT']

# MySQL Database
mysql_conf = {
    'host':     cfg['MYSQL_HOST'],
    'port':     cfg.getint('MYSQL_PORT'),
    'database': cfg['MYSQL_DB'],
    'user':     cfg['MYSQL_USER'],
    'password': cfg['MYSQL_PASSWORD'],
    'charset':  cfg['MYSQL_CHARSET'],
}

# SSH / SFTP (for attachment download)
ssh_conf = {
    'hostname': cfg['SSH_HOST'],
    'port':     cfg.getint('SSH_PORT'),
    'username': cfg['SSH_USER'],
    'password': cfg['SSH_PASSWORD'],
}

# Jira Configuration
JIRA_URL       = cfg['JIRA_URL']
JIRA_USER      = cfg['JIRA_USER']
JIRA_API_TOKEN = cfg['JIRA_API_TOKEN']
JIRA_PROJECT   = cfg['JIRA_PROJECT']
JIRA_ISSUETYPE = cfg['JIRA_ISSUETYPE']

# Attachment Paths
remote_attachment_path = cfg['REMOTE_ATTACHMENT_PATH']
local_attachments_dir  = cfg['LOCAL_ATTACHMENTS_DIR']

# Priority Mapping
PRIORITY_MAP = {
    1: cfg['PRIORITY_MAP_1'],
    2: cfg['PRIORITY_MAP_2'],
    3: cfg['PRIORITY_MAP_3'],
    4: cfg['PRIORITY_MAP_4'],
}

# —————————————
# 2) Verify MySQL connection (check SSH tunnel)
# —————————————
try:
    conn = mysql.connector.connect(**mysql_conf)
except mysql.connector.errors.InterfaceError as e:
    print("\n🚨  MySQL Connection Error  🚨")
    print(f"""
    Could not connect to the MySQL database.

    Error: {e}

💡 Tip: Did you forget to open the SSH tunnel?

    Try running:
      ssh -L {mysql_conf['port']}:127.0.0.1:3306 {ssh_conf['username']}@{ssh_conf['hostname']}

    Password: {ssh_conf['password']}
""")
    sys.exit(1)

cursor = conn.cursor(dictionary=True)

# —————————————
# 3) Prompt: single ticket or all tickets
# —————————————
choice = input("Export single ticket or all tickets? Enter 1 for single, 2 for all: ").strip()
if choice == "1":
    single_ticket = True
    ticket_number_input = input("Enter the ticket number to export: ").strip()
else:
    single_ticket = False

# —————————————
# 4) Connect to Jira
# —————————————
jira = JIRA(
    server=JIRA_URL,
    basic_auth=(JIRA_USER, JIRA_API_TOKEN)
)

# —————————————
# 5) (Optional) open SFTP for attachments
# —————————————
use_sftp = True
if use_sftp:
    os.makedirs(local_attachments_dir, exist_ok=True)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(**ssh_conf)
    sftp = ssh.open_sftp()

# —————————————
# 6) Fetch tickets
# —————————————
if single_ticket:
    cursor.execute("""
        SELECT number, id, subject, priority_id, status_id, created_at
        FROM ticket
        WHERE number = %s
    """, (ticket_number_input,))
    tickets = cursor.fetchall()
    if not tickets:
        print(f"No ticket found with number {ticket_number_input}.")
        sys.exit(0)
else:
    cursor.execute("""
        SELECT number, id, subject, priority_id, status_id, created_at
        FROM ticket
    """)
    tickets = cursor.fetchall()
    if not tickets:
        print("No tickets found in the database.")
        sys.exit(0)

print(f"Found {len(tickets)} ticket(s) to export.")

# —————————————
# 7) Migrate each ticket
# —————————————
for t in tickets:
    ticket_id     = t['id']
    ticket_number = t['number']
    subject       = t.get('subject') or f"Ticket {ticket_number}"
    priority_id   = t.get('priority_id')
    created_at    = t.get('created_at')
    created_date  = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d")

    jira_priority = PRIORITY_MAP.get(priority_id, PRIORITY_MAP[2])
    jira_status   = 'Resolve this issue'

    summary = f"[{ticket_number}] {subject} (Created: {created_date})"

    cursor.execute("""
        SELECT tm.id AS message_id,
               tm.created_at AS ts,
               COALESCE(CONCAT(u.firstname, ' ', u.lastname), tm.user_name) AS user_name,
               tm.text AS body
        FROM ticket_message tm
        LEFT JOIN user u ON tm.user_id = u.id
        WHERE tm.ticket_id = %s
        ORDER BY tm.created_at
    """, (ticket_id,))
    messages = cursor.fetchall()
    if not messages:
        print(f"Ticket {ticket_number} has no messages. Skipping.")
        continue

    first = messages[0]
    creator = first['user_name'] or "Unknown"
    raw_body = first['body'] or ""
    body_text = BeautifulSoup(raw_body, "html.parser") \
                   .get_text(separator="\n").strip()

    desc = f"**Originally created by {creator}**\n\n{body_text}"

    issue_dict = {
        'project':     {'key': JIRA_PROJECT},
        'summary':     summary,
        'description': desc,
        'issuetype':   {'name': JIRA_ISSUETYPE},
        'priority':    {'name': jira_priority},
        'labels':      ['supportpal-migration']
    }

    print(f"Creating issue for SupportPal #{ticket_number}...")
    issue = jira.create_issue(fields=issue_dict)
    print(f"✅ Created {issue.key} with priority '{jira_priority}'")

    transitions = jira.transitions(issue)
    resolve_tr = next(
        (tr for tr in transitions
         if tr['name'].lower() in ('resolve issue','done')),
        None
    )
    if resolve_tr:
        jira.transition_issue(issue, resolve_tr['id'])
        print(f"  → Forced transition to '{resolve_tr['name']}'")
    else:
        print("  ⚠️ No 'Resolve issue' transition; available:",
              [tr['name'] for tr in transitions])

    created_iso = datetime.fromtimestamp(created_at).isoformat()
    issue.update(fields={'created': created_iso,
                         'updated': created_iso})
    print(f"  → Set Jira created/updated to {created_iso}")

    # Add comments
    for m in messages[1:]:
        raw = BeautifulSoup(m['body'] or "", "html.parser") \
                  .get_text(separator="\n").strip()
        text = re.sub(r"\n{3,}", "\n\n", raw)
        author = m['user_name'] or "Unknown user"
        orig_ts = datetime.fromtimestamp(m['ts']) \
                         .strftime("%Y-%m-%d %H:%M:%S")
        comment_body = (
            f"**Originally posted by {author} on {orig_ts}**\n\n" +
            text
        )
        jira.add_comment(issue.key, comment_body)
    print(f"  → Added {len(messages)-1} comments.")

    # Download & attach files
    if use_sftp:
        local_dir = os.path.join(local_attachments_dir, str(ticket_id))
        os.makedirs(local_dir, exist_ok=True)
        cursor.execute("""
            SELECT upload_hash, original_name
            FROM ticket_attachment
            WHERE ticket_id = %s
        """, (ticket_id,))
        for att in cursor.fetchall():
            remote = f"{remote_attachment_path}/{att['upload_hash']}"
            local  = os.path.join(local_dir, att['original_name'])
            try:
                sftp.get(remote, local)
                jira.add_attachment(issue, local)
                print(f"  → Uploaded attachment: {att['original_name']}")
            except FileNotFoundError:
                print(f"  ⚠️ Missing on server: {remote}")
            except Exception as e:
                print(f"  ⚠️ Attachment error: {e}")

print("✅ Done importing tickets.")

# —————————————
# 8) Cleanup
# —————————————
cursor.close()
conn.close()
if use_sftp:
    sftp.close()
    ssh.close()
