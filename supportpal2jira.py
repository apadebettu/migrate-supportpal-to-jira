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
    logger.info("🛡️  SSH tunnel established on 127.0.0.1:%d → %s:3306", local_port, ssh_host)
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
            print(f"  {key}) {desc}")
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
            basic_auth=(conf['JIRA_USER'], conf['JIRA_API_TOKEN'])
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
      LEFT JOIN `user` AS u ON tm.user_id = u.id
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
    number    = t['number']
    subject   = t.get('subject') or f"Ticket {number}"
    created_at_ts = t['created_at']
    created_dt    = datetime.fromtimestamp(created_at_ts)
    created_str   = created_dt.strftime("%Y-%m-%d")

    pm = {int(k.split('_')[-1]):v for k,v in conf.items() if k.startswith('PRIORITY_MAP_')}
    logger.debug("Loaded PRIORITY_MAP: %r", pm)
    prio_id = int(t.get('priority_id') or 2)
    jira_prio = pm.get(prio_id, conf.get('DEFAULT_PRIORITY','Medium'))
    logger.info("Ticket %s priority_id=%s → Jira priority=%s", t['number'], prio_id, jira_prio)

    summary = f"[{number}] {subject} (Created: {created_str})"
    messages = fetch_messages(cursor, ticket_id)
    if not messages:
        logger.warning("No messages for ticket %s, skipping", number)
        return

    first = messages[0]
    creator = first['user_name'] or "Unknown"
    created_ts = datetime.fromtimestamp(first['ts'], tz=est).strftime("%Y-%m-%d %H:%M:%S EST")
    body = BeautifulSoup(first['body'] or "", 'html.parser').get_text(separator="\n").strip()
    desc = f"**Originally created by {creator} on {created_ts}**\n\n{body}"

    fields = {
        'project':     {'key': conf['JIRA_PROJECT']},
        'summary':     summary,
        'description': desc,
        'issuetype':   {'name': conf['JIRA_ISSUETYPE']},
        'priority':    {'name': jira_prio},
        'labels':      ['supportpal-migration'],
    }

    issue = jira.create_issue(fields=fields)
    logger.info("Created Jira issue %s (prio: %s)", issue.key, jira_prio)

    trans = jira.transitions(issue)
    res = next((t for t in trans if 'resolve' in t['name'].lower()), None)
    if res:
        jira.transition_issue(issue, res['id'])
        logger.info("Resolved %s", issue.key)

    for msg in messages[1:]:
        txt = BeautifulSoup(msg['body'] or "", 'html.parser').get_text(separator="\n").strip()
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        auth = msg['user_name'] or "Unknown"
        ts = datetime.fromtimestamp(msg['ts'], tz=est).strftime("%Y-%m-%d %H:%M:%S EST")
        comment = f"**Originally posted by {auth} on {ts}**\n\n{txt}"

        if msg.get('msg_type') == 1:
            jira.add_comment(issue.key, comment, visibility={'type': 'role', 'value': 'Service Desk Team'})
            logger.info("Added internal note to %s", issue.key)
        else:
            jira.add_comment(issue.key, comment)
            logger.info("Added public comment to %s", issue.key)

    if use_sftp and sftp:
        paths = download_attachments(cursor, sftp, ticket_id,
                                     conf['LOCAL_ATTACHMENTS_DIR'],
                                     conf['REMOTE_ATTACHMENT_PATH'])
        for p in paths:
            jira.add_attachment(issue, str(p))
            logger.info("Uploaded %s to %s", p.name, issue.key)

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
        print("  ssh -L 3307:127.0.0.1:3306 root@192.168.50.124\n")
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
                for t in tickets:
                    try:
                        migrate_ticket(jira, cursor, t, conf, use_sftp, ftp)
                    except Exception as e:
                        logger.exception("Error migrating %s: %s", t['number'], e)
        else:
            for t in tickets:
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
