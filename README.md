# 🛠️ SupportPal to Jira Cloud Migration Guide

This guide walks you through migrating tickets from a **SupportPal MySQL** server to **Jira Cloud**. You have to gather database info, configure the script, and run the migration, including messages and attachments.

---

## ✅ 1. Prepare Your Environment

### 🔽 Step 1.1: Clone or Download the Script

```bash
git clone https://github.com/your-repo/supportpal-to-jira.git
cd supportpal-to-jira
````

---

### 💡 Step 1.2: Install Python Requirements

Install these on **your local machine**:

```bash
pip install mysql-connector-python paramiko python-dotenv sshtunnel jira beautifulsoup4 tqdm requests pytz PyJWT
```
---

## 🔍 2. Get Database & SSH Access Details

SSH into your **SupportPal server** to collect DB credentials and the attachment path.

### 🔐 Step 2.1: SSH Into Server

```bash
ssh root@your.server.ip
```

### 🧾 Step 2.2: Find Database Credentials

Check the SupportPal config file (commonly located here):

```bash
cat /var/www/html/config/database.php
```

Look for values like:

```php
'mysql' => [
    'host' => '127.0.0.1',
    'database' => 'supportpal',
    'username' => 'spal_dbuser',
    'password' => 'your-db-password',
    ...
]
```

> 📌 **Copy these values down.**

---

### 🧱 Step 2.3: Find Attachments Directory

The default is usually:

```
/var/www/html/storage/app/tickets
```

You can confirm with:

```bash
ls -l /var/www/html/storage/app/tickets
```

---

## 🗄️ 3. Create the Config File

Back on **your local machine**, create a file called `config.ini`:

```ini
[DEFAULT]

# --- MySQL Database ---
MYSQL_HOST = 127.0.0.1
MYSQL_PORT = 3307
MYSQL_DB = supportpal
MYSQL_USER = spal_dbuser
MYSQL_PASSWORD = your-db-password
MYSQL_CHARSET = utf8mb4

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
DONE_TRANSITION_ID = 761

# --- Attachments ---
REMOTE_ATTACHMENT_PATH = /var/www/html/storage/app/tickets
LOCAL_ATTACHMENTS_DIR = SupportPal to Jira/attachments
OLD_SUPPORTPAL_URL = https://support.example.com
NEW_SUPPORTPAL_URL = https://internal.example.com

# --- Priority Mapping ---
PRIORITY_MAP_1 = Wishlist
PRIORITY_MAP_2 = Nice To Have
PRIORITY_MAP_3 = Must Have
PRIORITY_MAP_4 = Must Have - Urgent
DEFAULT_PRIORITY = Medium
```

> 🔒 **Important**: Add `config.ini` to `.gitignore` to avoid leaking credentials.

---

## 🔐 4. SSH Tunnel Setup (Handled by Script)

The script will automatically:

* Open an SSH tunnel (if configured)
* Forward local port `3307` → remote `3306` (MySQL)
* Connect to the DB through `127.0.0.1:3307`

No extra setup is needed, just ensure the credentials and ports are correct.

---

## 🚀 5. Run the Migration Script

```bash
python supportpal_to_jira.py
```

You’ll be prompted:

```
Enter path to config file [/default/path/to/config.ini]:
> config.ini

Migrate single ticket or all tickets?
  1) Single ticket
  2) All tickets
> 2

Download attachments over SFTP?
  1) Yes
  2) No
> 1
```

---

## 📥 6. What the Script Does

For each ticket:

1. Creates a Jira issue with:

   * Subject
   * Created date
   * Comments for each message
   * Internal notes as comments (in Jira panel formatting)
2. Downloads attachments (inline via HTTP and optionally regular files via SFTP)
3. Uploads files to the Jira issue concurrently
4. Sets mapped priority
5. Transitions the issue to the “Done” status

---

## 📂 7. Where Attachments Go

Attachments are temporarily saved to:

```bash
SupportPal to Jira/attachments/
```

> ✅ You can safely delete this folder after migration.

---

## 🧪 8. Sample Output

```
✅ MySQL pool ready @ 127.0.0.1:3307/supportpal
Found 3 ticket(s) to migrate.
📥 Downloaded inline image screenshot.png
✅ Uploaded screenshot.png
Created and transitioned ITA-101 for ticket 12345
✅ Migration complete.
```

---

## 🧹 9. After Migration

The script handles cleanup:

* MySQL connection pool closed
* SSH/SFTP sessions closed
* SSH tunnel shut down

---

## 🛡️ 10. Security Checklist

✔ Do NOT commit `config.ini` or `.env`  

✔ Use `.gitignore`  

✔ Use **limited-scope DB and SSH users**  

✔ Rotate Jira API token regularly 
