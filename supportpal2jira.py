#!/usr/bin/env python3
"""
Notes
-----
• Do NOT hardcode credentials. Use a config.ini and/or .env file.
• Jira Cloud can rate-limit; retries/backoff are included on common network exceptions.

Dependencies
------------
python -m pip install \
  mysql-connector-python sshtunnel paramiko beautifulsoup4 jira PyJWT \
  python-dotenv tqdm requests pytz

"""
from __future__ import annotations

import argparse
import configparser
import contextlib
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pytz
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

import mysql.connector
import mysql.connector.pooling
from dotenv import load_dotenv
from jira import JIRA
from jira.exceptions import JIRAError
from requests.exceptions import HTTPError, RequestException, ConnectionError, Timeout
from sshtunnel import SSHTunnelForwarder
import paramiko
from tqdm import tqdm

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --------------------------------------------------------------------------------------
# Globals & constants
# --------------------------------------------------------------------------------------
EASTERN = pytz.timezone("America/New_York")
MAX_DESC = 32767  # Jira server/Cloud description field storage maximum
DEFAULT_MYSQL_PORT = 3307
DEFAULT_SSH_PORT = 22
DEFAULT_POOL_SIZE = 10
ATTACH_UPLOAD_WORKERS = 5
TICKET_MIGRATION_WORKERS = 10

# Keep a global flag for cooperative shutdown on SIGINT/SIGTERM
_SHUTTING_DOWN = False

# --------------------------------------------------------------------------------------
# Logging setup
# --------------------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    log = logging.getLogger("supportpal_to_jira")
    log.setLevel(logging.INFO)

    # console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))

    # rotating file
    log_dir = Path("logs"); log_dir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(log_dir / "migration.log", maxBytes=2_000_000, backupCount=5)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))

    # avoid double handlers on reruns
    if not log.handlers:
        log.addHandler(ch)
        log.addHandler(fh)

    # quiet noisy libs if desired
    logging.getLogger('mysql.connector').setLevel(logging.WARNING)
    logging.getLogger('paramiko').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('jira').setLevel(logging.WARNING)

    return log

logger = _setup_logging()

# --------------------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------------------

def prompt_default(prompt: str, default: str) -> str:
    resp = input(f"{prompt} [{default}]: ").strip()
    return resp.strip('"').strip("'") if resp else default


def prompt_choice(prompt: str, choices: Dict[str, str]) -> str:
    while True:
        for key, desc in choices.items():
            print(f"  {key}) {desc}")
        sel = input(f"{prompt} ").strip()
        if sel in choices:
            return sel
        print("Invalid choice, try again.")


def redact(value: Optional[str]) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 6:
        return "***"
    return value[:2] + "***" + value[-2:]


def to_eastern(dt) -> datetime:
    if isinstance(dt, (int, float)):
        dt_utc = datetime.fromtimestamp(dt, pytz.UTC)
    elif isinstance(dt, datetime) and dt.tzinfo is None:
        dt_utc = pytz.UTC.localize(dt)
    else:
        dt_utc = dt
    return dt_utc.astimezone(EASTERN)

# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

@dataclass
class AppConfig:
    # MySQL
    MYSQL_HOST: str
    MYSQL_PORT: int
    MYSQL_DB: str
    MYSQL_USER: str
    MYSQL_PASSWORD: str
    MYSQL_CHARSET: str = "utf8mb4"

    # SSH/SFTP
    SSH_HOST: str = ""
    SSH_PORT: int = DEFAULT_SSH_PORT
    SSH_USER: str = ""
    SSH_PASSWORD: str = ""

    # Jira
    JIRA_URL: str = ""
    JIRA_USER: str = ""
    JIRA_API_TOKEN: str = ""
    JIRA_PROJECT: str = ""
    JIRA_ISSUETYPE: str = ""
    DONE_TRANSITION_ID: Optional[str] = None

    # Attachments & URLs
    REMOTE_ATTACHMENT_PATH: str = ""
    LOCAL_ATTACHMENTS_DIR: str = "attachments"
    OLD_SUPPORTPAL_URL: Optional[str] = None
    NEW_SUPPORTPAL_URL: Optional[str] = None

    # Priority mapping e.g. PRIORITY_MAP_1=Low
    PRIORITY_MAP: Dict[int, str] = None

    @staticmethod
    def from_ini_or_env(path: Path) -> "AppConfig":
        load_dotenv()  # allow .env to supplement
        config = configparser.ConfigParser()
        config.optionxform = str
        env = os.environ

        section: Dict[str, str]
        if path.exists():
            config.read(path)
            logger.info(f"Loaded configuration from {path}")
            section = dict(config['DEFAULT'])
        else:
            logger.warning("Config file not found, using environment variables only")
            section = {}

        def get(name: str, default: Optional[str] = None) -> Optional[str]:
            return section.get(name) or env.get(name) or default

        # Gather priority map keys
        prio_map: Dict[int, str] = {}
        for k, v in {**section, **env}.items():
            if str(k).startswith('PRIORITY_MAP_') and v:
                try:
                    prio = int(str(k).split('_')[-1])
                    prio_map[prio] = v
                except ValueError:
                    continue

        required = {
            'MYSQL_HOST': get('MYSQL_HOST'),
            'MYSQL_DB': get('MYSQL_DB'),
            'MYSQL_USER': get('MYSQL_USER'),
            'MYSQL_PASSWORD': get('MYSQL_PASSWORD'),
            'JIRA_URL': get('JIRA_URL'),
            'JIRA_USER': get('JIRA_USER'),
            'JIRA_API_TOKEN': get('JIRA_API_TOKEN'),
            'JIRA_PROJECT': get('JIRA_PROJECT'),
            'JIRA_ISSUETYPE': get('JIRA_ISSUETYPE'),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            for k in missing:
                logger.error(f"Missing required config value: {k}")
            raise SystemExit(1)

        cfg = AppConfig(
            MYSQL_HOST=required['MYSQL_HOST'],
            MYSQL_PORT=int(get('MYSQL_PORT', str(DEFAULT_MYSQL_PORT))),
            MYSQL_DB=required['MYSQL_DB'],
            MYSQL_USER=required['MYSQL_USER'],
            MYSQL_PASSWORD=required['MYSQL_PASSWORD'],
            MYSQL_CHARSET=get('MYSQL_CHARSET', 'utf8mb4') or 'utf8mb4',
            SSH_HOST=get('SSH_HOST', ''),
            SSH_PORT=int(get('SSH_PORT', str(DEFAULT_SSH_PORT))),
            SSH_USER=get('SSH_USER', ''),
            SSH_PASSWORD=get('SSH_PASSWORD', ''),
            JIRA_URL=required['JIRA_URL'],
            JIRA_USER=required['JIRA_USER'],
            JIRA_API_TOKEN=required['JIRA_API_TOKEN'],
            JIRA_PROJECT=required['JIRA_PROJECT'],
            JIRA_ISSUETYPE=required['JIRA_ISSUETYPE'],
            DONE_TRANSITION_ID=get('DONE_TRANSITION_ID'),
            REMOTE_ATTACHMENT_PATH=get('REMOTE_ATTACHMENT_PATH', ''),
            LOCAL_ATTACHMENTS_DIR=get('LOCAL_ATTACHMENTS_DIR', 'attachments'),
            OLD_SUPPORTPAL_URL=get('OLD_SUPPORTPAL_URL'),
            NEW_SUPPORTPAL_URL=get('NEW_SUPPORTPAL_URL'),
            PRIORITY_MAP=prio_map or {},
        )

        logger.info(
            "Config summary: MYSQL_HOST=%s MYSQL_PORT=%s JIRA_URL=%s JIRA_PROJECT=%s JIRA_ISSUETYPE=%s",
            cfg.MYSQL_HOST, cfg.MYSQL_PORT, cfg.JIRA_URL, cfg.JIRA_PROJECT, cfg.JIRA_ISSUETYPE,
        )
        return cfg

# --------------------------------------------------------------------------------------
# Context managers for external resources
# --------------------------------------------------------------------------------------

@contextlib.contextmanager
def ssh_tunnel(cfg: AppConfig):
    """Forward local port to remote MySQL via SSH if SSH creds are provided.
    Always binds 127.0.0.1:<local_port> → <SSH_HOST>:3306
    """
    if not cfg.SSH_HOST:
        # no SSH tunneling
        logger.info("SSH tunnel not configured; connecting directly to MySQL")
        yield None
        return

    local_port = cfg.MYSQL_PORT or DEFAULT_MYSQL_PORT
    server = SSHTunnelForwarder(
        (cfg.SSH_HOST, cfg.SSH_PORT),
        ssh_username=cfg.SSH_USER,
        ssh_password=cfg.SSH_PASSWORD,
        remote_bind_address=('127.0.0.1', 3306),
        local_bind_address=('127.0.0.1', local_port),
    )
    try:
        server.start()
        logger.info("🛡️  SSH tunnel established: 127.0.0.1:%s → %s:3306", server.local_bind_port, cfg.SSH_HOST)
        yield server
    finally:
        with contextlib.suppress(Exception):
            server.stop()
            logger.info("SSH tunnel closed.")


@contextlib.contextmanager
def ssh_client(cfg: AppConfig):
    if not cfg.SSH_HOST:
        yield None
        return
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=cfg.SSH_HOST, port=cfg.SSH_PORT, username=cfg.SSH_USER, password=cfg.SSH_PASSWORD)
    try:
        yield ssh
    finally:
        with contextlib.suppress(Exception):
            ssh.close()
            logger.info("SSH client closed.")


@contextlib.contextmanager
def sftp_from_ssh(ssh: Optional[paramiko.SSHClient]):
    sftp = None
    try:
        if ssh:
            sftp = ssh.open_sftp()
        yield sftp
    finally:
        if sftp:
            sftp.close()

# --------------------------------------------------------------------------------------
# MySQL
# --------------------------------------------------------------------------------------

def mysql_pool(cfg: AppConfig) -> mysql.connector.pooling.MySQLConnectionPool:
    try:
        pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="migration_pool",
            pool_size=DEFAULT_POOL_SIZE,
            host=cfg.MYSQL_HOST,
            port=cfg.MYSQL_PORT,
            database=cfg.MYSQL_DB,
            user=cfg.MYSQL_USER,
            password=cfg.MYSQL_PASSWORD,
            charset=cfg.MYSQL_CHARSET,
            autocommit=True,
        )
        logger.info("✅ MySQL pool ready @ %s:%s/%s", cfg.MYSQL_HOST, cfg.MYSQL_PORT, cfg.MYSQL_DB)
        return pool
    except mysql.connector.Error as e:
        logger.error("MySQL Connection Error: %s", e)
        raise SystemExit(1)


# --------------------------------------------------------------------------------------
# Jira
# --------------------------------------------------------------------------------------

def jira_client(cfg: AppConfig) -> JIRA:
    try:
        session = requests.Session()
        session.verify = False  # honor original behavior; consider making configurable
        jira = JIRA(options={'server': cfg.JIRA_URL, 'session': session},
                    basic_auth=(cfg.JIRA_USER, cfg.JIRA_API_TOKEN))
        return jira
    except Exception as e:
        logger.error("Failed to connect to Jira: %s", e)
        raise SystemExit(1)


def discover_done_transition_id(jira: JIRA, cfg: AppConfig) -> str:
    if cfg.DONE_TRANSITION_ID:
        logger.info("🔑 Using provided Done transition ID %s", cfg.DONE_TRANSITION_ID)
        return cfg.DONE_TRANSITION_ID

    # Fallback: inspect create meta; if not available, we can probe transitions from a temp issue
    try:
        meta = jira.createmeta(projectKeys=[cfg.JIRA_PROJECT],
                               issuetypeNames=[cfg.JIRA_ISSUETYPE],
                               expand='projects.issuetypes.transitions')
        projects = meta.get('projects', [])
        issuetypes = (projects or [{}])[0].get('issuetypes', [])
        transitions = (issuetypes or [{}])[0].get('transitions', [])
        for tr in transitions:
            to = tr.get('to', {})
            cat = (to.get('statusCategory') or {}).get('key')
            if cat == 'done':
                logger.info("🔑 Discovered Done transition ID %s", tr['id'])
                return tr['id']
    except Exception:
        pass

    logger.error("Unable to determine a 'done' transition ID. Provide DONE_TRANSITION_ID in config.")
    raise SystemExit(1)

# --------------------------------------------------------------------------------------
# Data fetch & processing
# --------------------------------------------------------------------------------------

def fetch_all_ticket_data(db_pool: mysql.connector.pooling.MySQLConnectionPool,
                          single_number: Optional[str] = None) -> List[dict]:
    """Fetch tickets and their messages efficiently (avoids N+1)."""
    conn = db_pool.get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        sql_tickets = (
            "SELECT t.number, t.id, t.subject, t.priority_id, t.status_id, t.created_at, "
            "COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.firstname, u.lastname)), ''), u.email) AS submitter_name "
            "FROM ticket AS t LEFT JOIN `user` AS u ON t.user_id = u.id"
        )
        params: Tuple = ()
        if single_number:
            sql_tickets += " WHERE t.number = %s"
            params = (single_number,)
        cursor.execute(sql_tickets, params)
        tickets = cursor.fetchall()
        if not tickets:
            return []

        # Messages
        ticket_ids = [t['id'] for t in tickets]
        placeholders = ', '.join(['%s'] * len(ticket_ids))
        sql_messages = (
            f"SELECT tm.ticket_id, tm.created_at AS ts, "
            f"COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.firstname, u.lastname)), ''), "
            f"        NULLIF(TRIM(tm.user_name), ''), NULLIF(TRIM(u.email), ''), 'Unknown') AS user_name, "
            f"tm.text AS body, tm.type AS msg_type "
            f"FROM ticket_message AS tm LEFT JOIN `user` AS u ON tm.user_id = u.id "
            f"WHERE tm.ticket_id IN ({placeholders}) ORDER BY tm.ticket_id, tm.created_at"
        )
        cursor.execute(sql_messages, tuple(ticket_ids))
        messages = cursor.fetchall()

        by_tid: Dict[int, List[dict]] = {}
        for m in messages:
            by_tid.setdefault(m['ticket_id'], []).append(m)
        for t in tickets:
            t['messages'] = by_tid.get(t['id'], [])
        return tickets
    finally:
        cursor.close(); conn.close()


def _requests_with_retries(session: requests.Session,
                           method: str,
                           url: str,
                           *,
                           retries: int = 3,
                           backoff: float = 1.5,
                           timeout: int = 15,
                           **kwargs) -> requests.Response:
    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except (HTTPError, ConnectionError, Timeout, RequestException) as e:
            if attempt == retries:
                raise
            sleep_s = backoff ** attempt
            logger.warning("Network error on %s %s (%s). Retrying in %.1fs...", method, url, e, sleep_s)
            time.sleep(sleep_s)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------------------
# Attachments & Jira issue migration
# --------------------------------------------------------------------------------------

def upload_attachments_concurrently(jira: JIRA, issue_key: str, paths: Sequence[Path], max_workers: int = ATTACH_UPLOAD_WORKERS) -> List[Path]:
    errors: List[Path] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(jira.add_attachment, issue=issue_key, attachment=str(p)): p for p in paths}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                fut.result()
                logger.info("✅ Uploaded %s", p.name)
            except Exception as e:
                logger.error("❌ Upload failed for %s: %s", p.name, e)
                errors.append(p)
    return errors


def _html_to_jira_markup(html: str,
                         hash_to_name: Dict[str, str],
                         old_url: Optional[str],
                         new_url: Optional[str],
                         inline_attachments: Set[str],
                         inline_image_urls: Dict[str, str]) -> str:
    soup = BeautifulSoup(html or '', 'html.parser')

    # Images
    for img in soup.find_all('img'):
        src = img.get('src', '')
        if old_url and old_url in src:
            corrected = src.replace(old_url, new_url or old_url)
            raw_hash = os.path.basename(corrected).split('?')[0]
            orig_name = hash_to_name.get(raw_hash, raw_hash)
            inline_attachments.add(orig_name)
            if new_url:
                inline_image_urls[orig_name] = corrected
            img.replace_with(f"!{orig_name}!")

    # Links
    for a in soup.find_all('a'):
        text = a.get_text(strip=True)
        href = a.get('href', '').strip()
        if href:
            a.replace_with(f"[{text}|{href}]")
        else:
            a.replace_with(text)

    # Preserve line breaks
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(['p', 'div']):
        tag.append("\n")

    body = soup.get_text(separator='')
    body = re.sub(r'\n\s*\n', '\n\n', body).strip()
    return body


def migrate_ticket(jira: JIRA,
                   db_pool: mysql.connector.pooling.MySQLConnectionPool,
                   t: dict,
                   cfg: AppConfig,
                   priority_map: Dict[int, str],
                   use_sftp: bool = False,
                   done_id: Optional[str] = None,
                   ssh: Optional[paramiko.SSHClient] = None,
                   download_session: Optional[requests.Session] = None) -> None:
    """Migrate a single SupportPal ticket to Jira."""
    if _SHUTTING_DOWN:
        return

    conn = db_pool.get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        number = t['number']
        subject = t.get('subject') or f"Ticket {number}"
        created_dt = to_eastern(t['created_at'])
        created_str = created_dt.strftime("%Y-%m-%d")
        jira_prio = priority_map.get(int(t.get('priority_id') or 2), 'Medium')
        submitter_name = t.get('submitter_name') or "Unknown User"

        # Attachment metadata for inline image name resolution
        cursor.execute("SELECT upload_hash, original_name FROM ticket_attachment WHERE ticket_id=%s", (t['id'],))
        attachments_meta = cursor.fetchall()
        hash_to_name = {att['upload_hash']: att['original_name'] for att in attachments_meta}

        parts: List[str] = [
            f"{{panel:title=Submitter|bgColor=#EAE6FF}}\nSubmitted by: *{submitter_name}*\n{{panel}}"
        ]
        inline_attachments: Set[str] = set()
        inline_image_urls: Dict[str, str] = {}

        for i, msg in enumerate(t['messages']):
            author = msg.get('user_name') or "Unknown"
            ts = to_eastern(msg['ts']).strftime("%Y-%m-%d %H:%M:%S %Z")
            body = _html_to_jira_markup(
                html=msg.get('body', ''),
                hash_to_name=hash_to_name,
                old_url=cfg.OLD_SUPPORTPAL_URL,
                new_url=cfg.NEW_SUPPORTPAL_URL,
                inline_attachments=inline_attachments,
                inline_image_urls=inline_image_urls,
            )
            verb = "Originally created" if i == 0 else "Commented"
            header = f"*{verb} by {author} on {ts}*"
            if msg.get('msg_type') == 1:
                parts.append(f"{{panel:title=Internal Note|bgColor=#DEEBFF}}\n{header}\n\n{body}\n{{panel}}")
            else:
                parts.append(f"{header}\n\n{body}")

        full_description = "\n\n----\n\n".join(parts)

        # 1) Create Jira issue with placeholder
        fields = {
            'project': {'key': cfg.JIRA_PROJECT},
            'summary': f"[{number}] {subject} (Created: {created_str})",
            'issuetype': {'name': cfg.JIRA_ISSUETYPE},
            'priority': {'name': jira_prio},
            'labels': ['supportpal-migration'],
            'description': f"Migration placeholder for ticket {number}",
        }
        issue = jira.create_issue(fields=fields)
        issue_key = getattr(issue, 'key', issue)

        # Attachment staging directory
        local_base = Path(cfg.LOCAL_ATTACHMENTS_DIR) / str(t['id'])
        local_base.mkdir(parents=True, exist_ok=True)

        # Prepare sessions
        dl_session = download_session or requests.Session()
        dl_session.verify = False

        attachments_to_upload: List[Path] = []
        expected_filenames: Set[str] = set()

        # Inline images fetched over HTTP(S)
        for name, url in inline_image_urls.items():
            try:
                resp = _requests_with_retries(dl_session, 'GET', url, stream=True)
                local_file = local_base / name
                with open(local_file, 'wb') as f:
                    for chunk in resp.iter_content(8192):
                        if not chunk:
                            continue
                        f.write(chunk)
                attachments_to_upload.append(local_file)
                expected_filenames.add(name)
                logger.info("📥 Downloaded inline image %s", name)
            except Exception as e:
                logger.warning("⚠️ Could not download inline image %s from %s: %s", name, url, e)

        # SFTP downloads for regular attachments
        if use_sftp and ssh:
            with sftp_from_ssh(ssh) as sftp:
                if sftp is not None:
                    cursor.execute("SELECT upload_hash, original_name FROM ticket_attachment WHERE ticket_id=%s", (t['id'],))
                    for att in cursor.fetchall():
                        remote = f"{cfg.REMOTE_ATTACHMENT_PATH}/{att['upload_hash']}"
                        dest = local_base / att['original_name']
                        try:
                            sftp.get(remote, str(dest))
                            attachments_to_upload.append(dest)
                            expected_filenames.add(dest.name)
                            logger.info("Downloaded attachment %s", att['original_name'])
                        except FileNotFoundError:
                            logger.warning("Missing attachment on server: %s", remote)
                        except Exception as e:
                            logger.error("Error downloading %s: %s", att['original_name'], e)

        # 2) If no attachments, finalize now
        if not attachments_to_upload:
            issue.update(fields={'description': full_description})
            jira.transition_issue(issue, done_id or cfg.DONE_TRANSITION_ID)
            logger.info("Created (no attachments) and transitioned %s for ticket %s", issue_key, number)
            return

        # 3) Upload attachments
        failed = upload_attachments_concurrently(jira, issue_key, attachments_to_upload)
        expected_filenames -= {p.name for p in failed}

        # 4) Update description (chunk if needed)
        if len(full_description) <= MAX_DESC:
            issue.update(fields={'description': full_description})
        else:
            head = full_description[:MAX_DESC]
            issue.update(fields={'description': head})
            tail = full_description[MAX_DESC:]
            chunks = [tail[i:i+MAX_DESC] for i in range(0, len(tail), MAX_DESC)]
            for chunk in chunks:
                jira.add_comment(issue_key, chunk)
            logger.warning("Description too long (%s chars); split into %s comment chunk(s).", len(full_description), len(chunks))

        # 5) Transition to Done
        jira.transition_issue(issue, done_id or cfg.DONE_TRANSITION_ID)
        logger.info("Created and transitioned %s for ticket %s", issue_key, number)

    finally:
        cursor.close(); conn.close()


# --------------------------------------------------------------------------------------
# CLI / Main
# --------------------------------------------------------------------------------------

def _install_signal_handlers():
    def _handler(signum, frame):
        global _SHUTTING_DOWN
        _SHUTTING_DOWN = True
        logger.warning("Received signal %s. Finishing in-flight work and shutting down…", signum)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    _install_signal_handlers()

    # Keep interactive prompting per original behavior
    cfg_path = Path(prompt_default("Enter path to config file", r"config.ini"))
    cfg = AppConfig.from_ini_or_env(cfg_path)

    # Open SSH tunnel if configured
    with ssh_tunnel(cfg) as tunnel, ssh_client(cfg) as ssh:
        # If an SSH tunnel was opened, we already bound cfg.MYSQL_PORT; keep as-is.
        pool = mysql_pool(cfg)

        jira = jira_client(cfg)
        done_id = discover_done_transition_id(jira, cfg)

        # Prompts (keep original UX)
        choice = prompt_choice("Migrate single ticket or all tickets?", {'1': 'Single ticket', '2': 'All tickets'})
        single_ticket = (choice == '1')
        ticket_number = input("Enter ticket number: ").strip() if single_ticket else None
        use_sftp = (prompt_choice("Download attachments over SFTP?", {'1': 'Yes', '2': 'No'}) == '1')

        # Fetch tickets
        tickets = fetch_all_ticket_data(pool, ticket_number)
        if not tickets:
            print("No tickets found. Exiting.")
            return
        print(f"Found {len(tickets)} ticket(s) to migrate.")

        # Priority map
        prio_map = cfg.PRIORITY_MAP or {}

        # Global download session for efficiency
        dl_session = requests.Session(); dl_session.verify = False

        # Migrate concurrently
        with ThreadPoolExecutor(max_workers=TICKET_MIGRATION_WORKERS) as executor:
            futures = {
                executor.submit(migrate_ticket, jira, pool, t, cfg, prio_map, use_sftp, done_id, ssh, dl_session): t
                for t in tickets
            }
            with tqdm(total=len(tickets), desc="Migrating tickets", position=0, leave=True) as pbar:
                for fut in as_completed(futures):
                    t = futures[fut]
                    try:
                        fut.result()
                    except Exception as e:
                        num = t.get('number', 'N/A')
                        logger.error("Thread failed while processing ticket %s: %s", num, e)
                        # Also append to a time‑stamped skipped file
                        skipped = Path(f"skipped_tickets_{datetime.now(EASTERN).strftime('%Y%m%d_%H%M%S')}.txt")
                        with skipped.open('a', encoding='utf-8') as f:
                            f.write(f"{num}\t{e}\n")
                    finally:
                        pbar.update(1)

        print("✅ Migration complete.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # Already logged – just honor exit code
        raise
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        raise SystemExit(1)
