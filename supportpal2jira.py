
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
from datetime import datetime
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

import mysql.connector
import mysql.connector.pooling
import paramiko
from bs4 import BeautifulSoup
from jira import JIRA
from jira.exceptions import JIRAError
from requests.exceptions import HTTPError
from dotenv import load_dotenv
from sshtunnel import SSHTunnelForwarder
from tqdm import tqdm
import requests

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

eastern = pytz.timezone('America/New_York')

SKIPPED_LOGFILE = f"skipped_tickets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

# Reusable HTTP sessions
jira_session = requests.Session()
jira_session.auth = None  # JIRA client sets auth itself
jira_session.verify = False

download_session = requests.Session()
download_session.verify = False

# —————————————
# Setup logging
# —————————————
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)
# logging.getLogger('mysql.connector').setLevel(logging.WARNING)

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
    logger.info(f"🛡️  SSH tunnel established on 127.0.0.1:{local_port} → {ssh_host}:3306")
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
        logger.info(f"Loaded configuration from {cfg_file}")
        return config['DEFAULT']
    else:
        logger.warning("Config file not found, falling back to environment variables")
        return os.environ

# —————————————
# Context managers
# —————————————
@contextmanager
def sftp_client(conf):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp = None
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
        logger.error(f"SFTP connection error: {e}")
        raise
    finally:
        if sftp:
            sftp.close()
        ssh.close()

# —————————————
# Concurrency
# —————————————

def upload_attachments_concurrently(jira, issue_key, paths, max_workers=5):
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(jira.add_attachment, issue=issue_key, attachment=str(path)): path
            for path in paths
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                future.result()
                logger.info(f"✅ Uploaded {path.name}")
            except Exception as e:
                logger.error(f"❌ Upload failed for {path.name}: {e}")
                errors.append(path)
    return errors

# —————————————
# Jira client
# —————————————
def get_jira_client(conf):
    try:
        jira = JIRA(
            options={'server': conf['JIRA_URL'], 'session': jira_session},
            basic_auth=(conf['JIRA_USER'], conf['JIRA_API_TOKEN']),
            async_=True,
            async_workers=10
        )

        return jira
    except Exception as e:
        logger.error(f"Failed to connect to Jira: {e}")
        sys.exit(1)

# —————————————
# Data fetchers & migrator
# —————————————
def fetch_all_ticket_data(db_pool, single_number=None):
    """
    Efficiently fetches tickets and all their messages, avoiding the N+1 query problem.
    """
    conn = db_pool.get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 1. Fetch tickets, now including the submitter's name
        sql_tickets = """
            SELECT
                t.number, t.id, t.subject, t.priority_id, t.status_id, t.created_at,
                COALESCE(
                    NULLIF(TRIM(CONCAT_WS(' ', u.firstname, u.lastname)), ''),
                    u.email
                ) AS submitter_name
            FROM ticket AS t
            LEFT JOIN `user` AS u ON t.user_id = u.id
        """
        params = ()
        if single_number:
            sql_tickets += " WHERE t.number = %s"
            params = (single_number,)
        
        cursor.execute(sql_tickets, params)
        tickets = cursor.fetchall()

        if not tickets:
            return []

        # 2. Fetch all messages for those tickets in a single query
        ticket_ids = [t['id'] for t in tickets]
        id_placeholders = ', '.join(['%s'] * len(ticket_ids))
        sql_messages = f"""
            SELECT
                tm.ticket_id, tm.created_at AS ts,
                COALESCE(
                    NULLIF(TRIM(CONCAT_WS(' ', u.firstname, u.lastname)), ''),
                    NULLIF(TRIM(tm.user_name), ''),
                    NULLIF(TRIM(u.email), ''),
                    'Unknown'
                ) AS user_name,
                tm.text AS body, tm.type AS msg_type
            FROM ticket_message AS tm
            LEFT JOIN `user` AS u ON tm.user_id = u.id
            WHERE tm.ticket_id IN ({id_placeholders})
            ORDER BY tm.ticket_id, tm.created_at
        """
        cursor.execute(sql_messages, tuple(ticket_ids))
        messages = cursor.fetchall()
        
        # 3. Group messages by ticket_id for easy lookup
        messages_by_ticket = {}
        for msg in messages:
            tid = msg['ticket_id']
            if tid not in messages_by_ticket:
                messages_by_ticket[tid] = []
            messages_by_ticket[tid].append(msg)

        # 4. Attach messages to their parent ticket
        for t in tickets:
            t['messages'] = messages_by_ticket.get(t['id'], [])
            
        return tickets
    finally:
        cursor.close()
        conn.close()

def download_attachments(cursor, sftp, ticket_id, local_base, remote_base):
    local_dir = Path(local_base) / str(ticket_id)
    local_dir.mkdir(parents=True, exist_ok=True)
    cursor.execute(
        "SELECT upload_hash, original_name FROM ticket_attachment WHERE ticket_id=%s",
        (ticket_id,)
    )
    paths = []
    for att in cursor.fetchall():
        remote = f"{remote_base}/{att['upload_hash']}"
        dest = local_dir / att['original_name']
        try:
            sftp.get(remote, str(dest))
            logger.info(f"Downloaded attachment {att['original_name']}")
            paths.append(dest)
        except FileNotFoundError:
            logger.warning(f"Missing attachment on server: {remote}")
        except Exception as e:
            logger.error(f"Error downloading {att['original_name']}: {e}")
    return paths

def migrate_ticket(jira, db_pool, t, conf, priority_map, use_sftp=False, done_id=None):
    """
    Migrate a single SupportPal ticket to Jira:
      1) Build the full description with inline-image markup
      2) Create the Jira issue with a placeholder description
      3) Download attachments via SFTP (if enabled)
      4) Upload all attachments (inline and regular)
      5) Poll Jira until all attachments are verified or timeout occurs
      6) Update the issue description with the final markup
      7) Transition the issue to Done
    """
    conn = db_pool.get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # --- (Existing code for extracting ticket fields and preparing description - no changes here) ---
        number      = t['number']
        subject     = t.get('subject') or f"Ticket {number}"
        created_str = datetime.fromtimestamp(t['created_at']).strftime("%Y-%m-%d")
        jira_prio   = priority_map.get(int(t.get('priority_id') or 2),
                                       conf.get('DEFAULT_PRIORITY', 'Medium'))
        submitter_name = t.get('submitter_name') or "Unknown User"
        submitter_panel = (
            f"{{panel:title=Submitter|bgColor=#EAE6FF}}\n"
            f"Submitted by: *{submitter_name}*\n"
            f"{{panel}}"
        )
        cursor.execute(
            "SELECT upload_hash, original_name FROM ticket_attachment WHERE ticket_id=%s",
            (t['id'],)
        )
        attachments_meta = cursor.fetchall()
        hash_to_name = {att['upload_hash']: att['original_name'] for att in attachments_meta}
        parts = [submitter_panel]
        inline_attachments = set()
        inline_image_urls = {}
        for i, msg in enumerate(t['messages']):
            author = msg.get('user_name') or "Unknown"
            ts     = datetime.fromtimestamp(msg['ts'], tz=pytz.utc) \
                           .astimezone(eastern) \
                           .strftime("%Y-%m-%d %H:%M:%S %Z")
            soup   = BeautifulSoup(msg.get('body', ''), 'html.parser')
            old_url = conf.get('OLD_SUPPORTPAL_URL')
            new_url = conf.get('NEW_SUPPORTPAL_URL')
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if old_url and old_url in src:
                    corrected = src.replace(old_url, new_url)
                    raw_hash  = os.path.basename(corrected).split('?')[0]
                    orig_name = hash_to_name.get(raw_hash, raw_hash)
                    inline_attachments.add(orig_name)
                    inline_image_urls[orig_name] = corrected
                    img.replace_with(f"!{orig_name}!")
            body = soup.get_text(separator="\n").strip()
            body = re.sub(r"\n{3,}", "\n\n", body)
            verb = "Originally created" if i == 0 else "Commented"
            header = f"*{verb} by {author} on {ts}*"
            if msg.get('msg_type') == 1:
                panel = (
                    f"{{panel:title=Internal Note|bgColor=#DEEBFF}}\n"
                    f"{header}\n\n{body}\n"
                    f"{{panel}}"
                )
                parts.append(panel)
            else:
                parts.append(f"{header}\n\n{body}")
        full_description = "\n\n----\n\n".join(parts)
        # --- (End of unchanged section) ---

        # 1) Create the Jira issue with a placeholder description
        fields = {
            'project':     {'key': conf['JIRA_PROJECT']},
            'summary':     f"[{number}] {subject} (Created: {created_str})",
            'issuetype':   {'name': conf['JIRA_ISSUETYPE']},
            'priority':    {'name': jira_prio},
            'labels':      ['supportpal-migration'],
            'description': f"Migration placeholder for ticket {number}"
        }
        issue     = jira.create_issue(fields=fields)
        issue_key = getattr(issue, 'key', issue)

        # 2) Download attachments via SFTP if requested
        attachments_to_upload = []
        expected_filenames = set()

        local_base = Path(conf['LOCAL_ATTACHMENTS_DIR']) / str(t['id'])
        local_base.mkdir(parents=True, exist_ok=True)

        if inline_image_urls:
            for name, url in inline_image_urls.items():
                try:
                    resp = download_session.get(url, stream=True, timeout=10)
                    resp.raise_for_status()
                    local_file = local_base / name
                    with open(local_file, 'wb') as f:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk)
                    attachments_to_upload.append(local_file)
                    expected_filenames.add(name)
                    logger.info(f"📥 Downloaded inline image {name}")
                except Exception as e:
                    logger.warning(f"⚠️ Could not download inline image {name} from {url}: {e}")

        if use_sftp:
            with sftp_client(conf) as sftp:
                for att in attachments_meta:
                    orig_name = att['original_name']
                    remote    = f"{conf['REMOTE_ATTACHMENT_PATH']}/{att['upload_hash']}"
                    local     = local_base / orig_name
                    try:
                        sftp.get(remote, str(local))
                        attachments_to_upload.append(local)
                        expected_filenames.add(orig_name)
                        logger.info(f"📥 Downloaded attachment {orig_name}")
                    except FileNotFoundError:
                        logger.warning(f"Missing attachment on server: {remote}")
                    except Exception as e:
                        logger.error(f"Error downloading {orig_name}: {e}")

        # If there are no attachments, we can skip the upload/verification steps
        if not attachments_to_upload:
            issue.update(fields={'description': full_description})
            jira.transition_issue(issue, done_id or conf['DONE_TRANSITION_ID'])
            logger.info(f"Successfully created (no attachments) and transitioned {issue_key} for ticket {number}")
            return # Exit function for this ticket

        # 3) Upload all attachments
        failed_uploads = upload_attachments_concurrently(jira, issue_key, attachments_to_upload)
        expected_filenames -= {p.name for p in failed_uploads}


        # 4) ✨ NEW: Poll Jira to verify all successful uploads are present
        # all_attachments_verified = False
        # timeout = 60  # seconds
        # start_time = time.time()
        # logger.info(f"Verifying {len(expected_filenames)} attachments on {issue_key}...")
        # while time.time() - start_time < timeout:
        #     try:
        #         refreshed = jira.issue(issue_key, fields='attachment')
        #         existing_on_jira = {att.filename for att in refreshed.fields.attachment}
                
        #         if expected_filenames.issubset(existing_on_jira):
        #             all_attachments_verified = True
        #             logger.info("✅ All attachments verified successfully.")
        #             break
                
        #         missing_files = expected_filenames - existing_on_jira
        #         logger.info(f"Waiting for {len(missing_files)} attachments to be indexed... ({int(time.time() - start_time)}s elapsed)")
        #         time.sleep(5) # Wait 5 seconds before checking again

        #     except JIRAError as e:
        #         logger.warning(f"Could not verify attachments due to Jira API error: {e}")
        #         time.sleep(5)

        # if not all_attachments_verified:
        #     final_attachments = jira.issue(issue_key, fields='attachment').fields.attachment
        #     final_filenames = {att.filename for att in final_attachments}
        #     missing = expected_filenames - final_filenames
        #     logger.error(f"❌ TIMEOUT: Failed to verify all attachments for {issue_key} after {timeout}s. Missing: {missing}")
        #     # Continue anyway, but the description might have broken images

        # 5) Update the description now that attachments exist, handling length limit
        MAX_DESC = 32767

        if len(full_description) <= MAX_DESC:
            issue.update(fields={'description': full_description})
        else:
            head = full_description[:MAX_DESC]
            issue.update(fields={'description': head})

            tail = full_description[MAX_DESC:]
            chunks = [tail[i:i+MAX_DESC] for i in range(0, len(tail), MAX_DESC)]
            for chunk in chunks:
                jira.add_comment(issue_key, chunk)

            logger.warning(
                f"Description too long ({len(full_description)} chars); "
                f"split and posted {len(chunks)} comment(s)."
            )

        # 6) Transition the issue to Done
        jira.transition_issue(issue, done_id or conf['DONE_TRANSITION_ID'])
        logger.info(f"Successfully created and transitioned {issue_key} for ticket {number}")

    finally:
        cursor.close()
        conn.close()

# —————————————
# Main
# —————————————
def main():
    # Load configuration (from file or environment)
    conf   = load_config_interactive()
    # Open SSH tunnel to MySQL
    tunnel = open_ssh_tunnel(conf)
    # Initialize Jira client
    jira   = get_jira_client(conf)

    # —————————————
    # DETERMINE DONE_TRANSITION_ID
    # —————————————
    done_id = conf.get('DONE_TRANSITION_ID')
    if done_id:
        logger.info(f"🔑 Using hard‑coded Done transition ID {done_id}")
    else:
        # Fallback to autodiscovery if not configured
        meta = jira.createmeta(
            projectKeys=[conf['JIRA_PROJECT']],
            issuetypeNames=[conf['JIRA_ISSUETYPE']],
            expand='projects.issuetypes.transitions'
        )
        projects = meta.get('projects', [])
        if not projects:
            logger.error(
                f"No projects in createmeta response for "
                f"project={conf['JIRA_PROJECT']}, issuetype={conf['JIRA_ISSUETYPE']}. "
                f"Full response: {meta}"
            )
            sys.exit(1)

        issuetypes = projects[0].get('issuetypes', [])
        if not issuetypes:
            logger.error(
                f"No issuetypes in createmeta response for project. "
                f"Response: {projects[0]}"
            )
            sys.exit(1)

        transitions = issuetypes[0].get('transitions', [])
        done_trans = next(
            (tr for tr in transitions
             if tr['to']['statusCategory']['key'] == 'done'),
            None
        )
        if not done_trans:
            logger.error("No transition to a ‘done’ statusCategory found in Jira metadata.")
            sys.exit(1)

        done_id = done_trans['id']
        logger.info(f"🔑 Fallback: using discovered Done transition ID {done_id}")

    # —————————————
    # Setup MySQL connection pool
    # —————————————
    try:
        db_pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="migration_pool",
            pool_size=10,
            host=conf['MYSQL_HOST'],
            port=int(conf.get('MYSQL_PORT', 3307)),
            database=conf['MYSQL_DB'],
            user=conf['MYSQL_USER'],
            password=conf['MYSQL_PASSWORD'],
            charset=conf.get('MYSQL_CHARSET', 'utf8mb4'),
            autocommit=True
        )
        logger.info("✅ Successfully connected to MySQL and created connection pool.")
    except mysql.connector.Error as e:
        logger.error(f"MySQL Connection Error: {e}")
        print("\n❌ Failed to connect to MySQL. Please check your credentials or config.")
        sys.exit(1)

    # —————————————
    # Interactive prompts: single vs all tickets, SFTP attachments
    # —————————————
    choice        = prompt_choice("Migrate single ticket or all tickets?", {'1':'Single ticket','2':'All tickets'})
    single_ticket = (choice == '1')
    ticket_number = input("Enter ticket number: ").strip() if single_ticket else None
    use_sftp      = (prompt_choice("Download attachments over SFTP?", {'1':'Yes','2':'No'}) == '1')

    # —————————————
    # Fetch tickets from SupportPal
    # —————————————
    tickets = fetch_all_ticket_data(db_pool, ticket_number)
    if not tickets:
        print("No tickets found. Exiting.")
        sys.exit(0)
    print(f"Found {len(tickets)} ticket(s) to migrate.")

    # —————————————
    # Build priority map from config
    # —————————————
    priority_map = {
        int(k.split('_')[-1]): v
        for k, v in conf.items()
        if k.startswith('PRIORITY_MAP_')
    }

    # —————————————
    # Migrate tickets concurrently
    # —————————————
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ticket = {
            executor.submit(
                migrate_ticket,
                jira, db_pool, t, conf, priority_map, use_sftp, done_id
            ): t
            for t in tickets
        }

        with tqdm(total=len(tickets), desc="Migrating tickets", position=0, leave=True) as pbar:
            for future in as_completed(future_to_ticket):
                t = future_to_ticket[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"A thread failed while processing ticket {t.get('number', 'N/A')}: {e}")
                finally:
                    pbar.update(1)

    # —————————————
    # Cleanup
    # —————————————
    tunnel.stop()
    logger.info("SSH tunnel closed.")
    print("✅ Migration complete.")

if __name__ == "__main__":
    main()
