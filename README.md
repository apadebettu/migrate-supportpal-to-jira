
# 🛠️ SupportPal to Jira Migration Script

This Python script automates the migration of support tickets from a **SupportPal** MySQL database to **Jira Cloud**, including messages, attachments, and ticket metadata.

---

## 📦 Features

* Export either a **single ticket** or **all tickets** from SupportPal.
* Creates corresponding Jira issues with:
  * Original ticket subject and creation date
  * Full message history as comments
  * Attachments via SFTP
  * Priority mapping
* Automatically transitions the issue to "Resolve this issue"
* Preserves timestamps and user info from SupportPal

---

## 🔧 Requirements

* Python 3.6+
* SSH access to the SupportPal server (for attachments)
* A Jira Cloud account with API access

### Install Python Dependencies

```bash
pip install mysql-connector-python paramiko python-dotenv sshtunnel jira beautifulsoup4
```

---

## 🔐 Configuration

You’ll be prompted to provide a config file path when the script starts. A sample config looks like this:

```ini
[DEFAULT]

# --- MySQL Database ---
MYSQL_HOST = 127.0.0.1
MYSQL_PORT = 3307
MYSQL_DB = support_pal
MYSQL_USER = spal_dbuser
MYSQL_PASSWORD = your-db-password

# --- SSH for Attachments ---
SSH_HOST = your.server.ip
SSH_PORT = 22
SSH_USER = root
SSH_PASSWORD = your-ssh-password

# --- Jira Credentials ---
JIRA_URL = https://your-domain.atlassian.net
JIRA_USER = your-email@domain.com
JIRA_API_TOKEN = your-api-token
JIRA_PROJECT = PROJECTKEY
JIRA_ISSUETYPE = [System] Service request

# --- Attachments ---
REMOTE_ATTACHMENT_PATH = /var/www/html/storage/app/tickets
LOCAL_ATTACHMENTS_DIR = SupportPal to Jira/attachments

# --- Priority Mapping ---
PRIORITY_MAP_1 = Wishlist
PRIORITY_MAP_2 = Nice To Have
PRIORITY_MAP_3 = Must Have
PRIORITY_MAP_4 = Must Have - Urgent
DEFAULT_PRIORITY = Medium
```

> 💡 Use environment variables or a `.env` file instead of hardcoding credentials in production.

---

## 🚀 Usage

Run the script.

You’ll be prompted to choose:

```
Enter path to config file [/default/path/to/config.ini]:
Migrate single ticket or all tickets?
  1) Single ticket
  2) All tickets
Download attachments over SFTP?
  1) Yes
  2) No
```

---

## 📂 Attachments Handling

Attachments are downloaded from the SupportPal server via **SFTP** and uploaded to the corresponding Jira issue.

They are temporarily stored in:

```
SupportPal to Jira/attachments/
```

Make sure the SSH user has access to:

```
/var/www/html/storage/app/tickets
```

---

## 🗂️ Priority Mapping

| SupportPal ID | Jira Priority        |
|---------------|----------------------|
| 1             | Wishlist             |
| 2             | Nice To Have         |
| 3             | Must Have            |
| 4             | Must Have - Urgent   |

If a priority is missing, it falls back to `Medium`.

---

## 📘 Sample Output

```
✅ Successfully connected to MySQL.
Found 2 ticket(s).
Created Jira issue QSD-154 (prio: Must Have)
Resolved QSD-154
→ Added 4 comments
→ Uploaded attachment: error_log.txt

Created Jira issue QSD-155 (prio: Nice To Have)
Resolved QSD-155
→ Added 2 comments
→ No attachments found

✅ Migration complete.
```

---

## 🧹 Cleanup

At the end of the migration:

* MySQL connections are closed
* SSH/SFTP sessions are properly shut down
* SSH tunnel is terminated

---

## ⚠️ Security Warning

This script requires credentials to access your systems.

**Recommendations:**

* Never commit `.ini` or `.env` files to source control
* Use `.gitignore` to exclude sensitive files
* Use limited-scope SSH and database users
* Consider rotating Jira API tokens regularly
