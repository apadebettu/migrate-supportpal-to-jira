#!/usr/bin/env python3
"""
Interactive SupportPal to Jira migration script.

On launch, prompts you for:
  • Config file path
  • Single vs. all-ticket export
  • (Optional) Attachments via SFTP

Everything else proceeds automatically.
"""

import os
import sys
import re
import logging
import configparser
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
import mysql.connector
import paramiko
from bs4 import BeautifulSoup
from jira import JIRA
from dotenv import load_dotenv
from sshtunnel import SSHTunnelForwarder
from tqdm import tqdm

est = timezone(timedelta(hours=-5))

# —————————————
# Setup logging
# —————————————
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# —————————————
# SSH Tunnel
# —————————————
def open_ssh_tunnel(conf):
    """
    Establish an SSH tunnel to the remote MySQL server.
    Returns the tunnel object so it can be kept alive.
    """
    ssh_host = conf['SSH_HOST']
    ssh_port = int(conf.get('SSH_PORT', 22))
    ssh_user = conf['SSH_USER']
    ssh_pass = conf['SSH_PASSWORD']
    local_port = int(conf.get('MYSQL_PORT', 3307))

    server = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_password=ssh_pass,
        remote_bind_address=('127.0.0.1', 3306),
        local_bind_address=('127.0.0.1', local_port)
    )

    server.start()
    time.sleep(2)
    logger.info("🛡️  SSH tunnel established on 127.0.0.1:%d → %s:3306", local_port, ssh_host)
    return server

# —————————————
# Helpers for prompts
# —————————————
def prompt_default(prompt, default):
    resp = input(f"{prompt} [{default}]: ").strip()
    return resp.strip('"').strip("'") if resp else default

def prompt_choice(prompt, choices):
    while True:
        for key, desc in choices.items():
            print(f"  {key}) {desc}")
        sel = input(f"{prompt} ").strip()
        if sel in choices:
            return sel
        print("Invalid choice, try again.")

# —————————————
# Load config
# —————————————
def load_config_interactive():
    load_dotenv()
    cfg_path = prompt_default(
        "Enter path to config file",
        r"C:\Users\apadebettu\Desktop\SupportPal to Jira\config.ini"
    )
    cfg_file = Path(cfg_path)
    config = configparser.ConfigParser()
    config.optionxform = str
    if cfg_file.exists():
        config.read(cfg_file)
        logger.info("Loaded configuration from %s", cfg_file)
        return config['DEFAULT']
    else:
        logger.warning("Config file not found, falling back to environment variables")
        return os.environ

# —————————————
# Context managers
# —————————————
from contextlib import contextmanager

@contextmanager
def mysql_connection(conf):
    conn = None
    try:
        conn = mysql.connector.connect(
            host=conf['MYSQL_HOST'],
            port=int(conf.get('MYSQL_PORT', 3306)),
            database=conf['MYSQL_DB'],
            user=conf['MYSQL_USER'],
            password=conf['MYSQL_PASSWORD'],
            charset=conf.get('MYSQL_CHARSET', 'utf8mb4'),
            autocommit=True
        )
        yield conn
    except mysql.connector.Error as e:
        logger.error("MySQL Connection Error: %s", e)
        sys.exit(1)
    finally:
        if conn:
            conn.close()

@contextmanager
def sftp_client(conf):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=conf['SSH_HOST'],
            port=int(conf.get('SSH_PORT', 22)),
            username=conf['SSH_USER'],
            password=conf['SSH_PASSWORD']
        )
        sftp = ssh.open_sftp()
        yield sftp
    except Exception as e:
        logger.error("SFTP connection error: %s", e)
        raise
    finally:
        try:
            sftp.close()
        except:
            pass
        ssh.close()

# —————————————
# Jira client
# —————————————
def get_jira_client(conf):
    try:
        jira = JIRA(
            server=conf['JIRA_URL'],
            basic_auth=(conf['JIRA_USER'], conf['JIRA_API_TOKEN']),
            options={'resilient': False},
            async_=True,
            async_workers=10
        )
        return jira
    except Exception as e:
        logger.error("Failed to connect to Jira: %s", e)
        sys.exit(1)

# —————————————
# Data fetchers & migrator
# —————————————
def fetch_tickets(cursor, single_number=None):
    sql = "SELECT number,id,subject,priority_id,status_id,created_at FROM ticket"
    params = ()
    if single_number:
        sql += " WHERE number=%s"
        params = (single_number,)
    cursor.execute(sql, params)
    return cursor.fetchall()

def fetch_messages(cursor, ticket_id):
    sql = """
      SELECT
        tm.id AS message_id,
        tm.created_at AS ts,
        COALESCE(
          NULLIF(TRIM(CONCAT_WS(' ', u.firstname, u.lastname)), ''),
          NULLIF(TRIM(tm.user_name), ''),
          NULLIF(TRIM(u.email), ''),
          'Unknown'
        ) AS user_name,
        tm.text AS body,
        tm.type AS msg_type
      FROM ticket_message AS tm
      LEFT JOIN user AS u ON tm.user_id = u.id
      WHERE tm.ticket_id = %s
      ORDER BY tm.created_at
    """
    cursor.execute(sql, (ticket_id,))
    return cursor.fetchall()


def download_attachments(cursor, sftp, ticket_id, local_base, remote_base):
    local_dir = Path(local_base)/str(ticket_id)
    local_dir.mkdir(parents=True, exist_ok=True)
    cursor.execute(
      "SELECT upload_hash,original_name FROM ticket_attachment WHERE ticket_id=%s",
      (ticket_id,)
    )
    paths = []
    for att in cursor.fetchall():
        remote = f"{remote_base}/{att['upload_hash']}"
        dest = local_dir/att['original_name']
        try:
            sftp.get(remote, str(dest))
            logger.info("Downloaded attachment %s", att['original_name'])
            paths.append(dest)
        except FileNotFoundError:
            logger.warning("Missing attachment on server: %s", remote)
        except Exception as e:
            logger.error("Error downloading %s: %s", att['original_name'], e)
    return paths

def migrate_ticket(jira, cursor, t, conf, use_sftp=False, sftp=None):
    ticket_id = t['id']
    number    = t['number']
    subject   = t.get('subject') or f"Ticket {number}"
    created_at_ts = t['created_at']
    created_dt    = datetime.fromtimestamp(created_at_ts)
    created_str   = created_dt.strftime("%Y-%m-%d")

    # --- Priority Mapping ---
    pm = {int(k.split('_')[-1]):v for k,v in conf.items() if k.startswith('PRIORITY_MAP_')}
    prio_id = int(t.get('priority_id') or 2)
    jira_prio = pm.get(prio_id, conf.get('DEFAULT_PRIORITY', 'Medium'))
    logger.info("Ticket %s priority_id=%s → Jira priority=%s", number, prio_id, jira_prio)

    # --- Fetch and Combine Messages ---
    messages = fetch_messages(cursor, ticket_id)
    if not messages:
        logger.warning("No messages for ticket %s, skipping", number)
        return

    description_parts = []
    for i, msg in enumerate(messages):
        author = msg['user_name'] or "Unknown"
        timestamp = datetime.fromtimestamp(msg['ts'], tz=est).strftime("%Y-%m-%d %H:%M:%S EST")
        body = BeautifulSoup(msg['body'] or "", 'html.parser').get_text(separator="\n").strip()
        body = re.sub(r"\n{3,}", "\n\n", body) # Clean up extra newlines

        # Format header based on whether it's the original post or a comment
        header_verb = "Originally created" if i == 0 else "Commented"
        header = f"*{header_verb} by {author} on {timestamp}*"

        # Check for internal notes and format them differently
        is_internal = msg.get('msg_type') == 1
        if is_internal:
            # Use Jira's panel macro to highlight internal notes
            message_content = (
                f"{{panel:title=Internal Note|bgColor=#DEEBFF}}\n"
                f"{header}\n\n{body}\n"
                f"{{panel}}"
            )
        else:
            message_content = f"{header}\n\n{body}"

        description_parts.append(message_content)

    # Join all parts with a horizontal rule for readability
    full_description = "\n\n----\n\n".join(description_parts)

    # --- Create Jira Issue ---
    fields = {
        'project':     {'key': conf['JIRA_PROJECT']},
        'summary':     f"[{number}] {subject} (Created: {created_str})",
        'description': full_description,
        'issuetype':   {'name': conf['JIRA_ISSUETYPE']},
        'priority':    {'name': jira_prio},
        'labels':      ['supportpal-migration'],
    }

    issue = jira.create_issue(fields=fields)
    logger.info("✅ Created Jira issue %s for SupportPal ticket %s", issue.key, number)

    # --- Transition Issue ---
    try:
        trans = jira.transitions(issue)
        # Find a transition named "Resolve" or "Done", case-insensitive
        resolve_transition = next(
            (t for t in trans if t['name'].lower() in ['resolve', 'done', 'resolve issue']), None
        )
        if resolve_transition:
            jira.transition_issue(issue, resolve_transition['id'])
            logger.info("Resolved %s", issue.key)
    except Exception as e:
        logger.warning("Could not resolve issue %s: %s", issue.key, e)


    # --- Handle Attachments ---
    if use_sftp and sftp:
        paths = download_attachments(cursor, sftp, ticket_id,
                                     conf['LOCAL_ATTACHMENTS_DIR'],
                                     conf['REMOTE_ATTACHMENT_PATH'])
        if paths:
            for p in paths:
                try:
                    jira.add_attachment(issue=issue, attachment=str(p))
                    logger.info("📎 Uploaded %s to %s", p.name, issue.key)
                except Exception as e:
                    logger.error("Failed to upload attachment %s: %s", p.name, e)

# —————————————
# Main
# —————————————
def main():
    conf = load_config_interactive()

    # Establish SSH tunnel to access remote MySQL
    tunnel = open_ssh_tunnel(conf)

    try:
        with mysql_connection(conf) as test_conn:
            pass
        logger.info("✅ Successfully connected to MySQL.")
    except SystemExit:
        print("\n❌ Failed to connect to MySQL. Please check your credentials or config.\n")
        print("🔐 If the database is only accessible via SSH, try using port forwarding:\n")
        print("  ssh -L 3307:127.0.0.1:3306 root@192.168.50.124\n")
        print("Then update your config to use host=127.0.0.1 and port=3307.\n")
        sys.exit(1)

    choice = prompt_choice("Migrate single ticket or all tickets?", {
        '1': 'Single ticket',
        '2': 'All tickets'
    })
    single_ticket = (choice == '1')
    ticket_number = input("Enter ticket number: ").strip() if single_ticket else None

    use_sftp = prompt_choice("Download attachments over SFTP?", {
        '1': 'Yes',
        '2': 'No'
    }) == '1'

    jira = get_jira_client(conf)

    with mysql_connection(conf) as conn:
        cursor = conn.cursor(dictionary=True)
        tickets = fetch_tickets(cursor, ticket_number if single_ticket else None)
        if not tickets:
            print("No tickets found. Exiting.")
            sys.exit(0)
        print(f"Found {len(tickets)} ticket(s).")

        if use_sftp:
            with sftp_client(conf) as ftp:
                for t in tqdm(tickets, desc="Migrating tickets with attachments"):
                    try:
                        migrate_ticket(jira, cursor, t, conf, use_sftp, ftp)
                    except Exception as e:
                        logger.exception("Error migrating %s: %s", t['number'], e)
        else:
            for t in tqdm(tickets, desc="Migrating tickets"):
                try:
                    migrate_ticket(jira, cursor, t, conf, use_sftp, None)
                except Exception as e:
                    logger.exception("Error migrating %s: %s", t['number'], e)

    # Stop the SSH tunnel after work is done
    tunnel.stop()
    logger.info("SSH tunnel closed.")

    print("✅ Migration complete.")

if __name__ == "__main__":
    main()
