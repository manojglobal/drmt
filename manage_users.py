#!/usr/bin/env python3
"""
DRMT User Management CLI Tool
==============================
Safely manage dashboard operator accounts in DRMT without running or restarting the server.

Usage:
  Interactive Menu:
    python3 manage_users.py

  Direct Commands:
    python3 manage_users.py list
    python3 manage_users.py create <username> <password>
    python3 manage_users.py reset <username> <new_password>
    python3 manage_users.py delete <username>
"""

import sys
import os
import sqlite3
from datetime import datetime

# Add project root to sys.path so we can import DRMT modules
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from credentials.crypto import hash_password, verify_password

DB_PATH = os.path.join(PROJECT_DIR, "data", "drmt.db")
AUDIT_LOG_DIR = os.path.join(PROJECT_DIR, "data", "logs", "audit")


def get_db_connection():
    """Connects to SQLite database."""
    if not os.path.exists(DB_PATH):
        print(f"[-] Error: Database not found at {DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def log_audit_event(conn, username: str, action: str, details: str):
    """Records user management action in auth_audit_logs and file log."""
    try:
        conn.execute(
            """
            INSERT INTO auth_audit_logs (username, action, status, ip_address, user_agent, details, timestamp)
            VALUES (?, ?, 'SUCCESS', '127.0.0.1', 'CLI-Script', ?, datetime('now', 'localtime'))
            """,
            (username, action, details)
        )
    except Exception:
        pass  # If table doesn't exist, proceed safely

    # Mirror to file log
    try:
        os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(AUDIT_LOG_DIR, f"app_{today}.log")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [AUDIT] user='{username}' | action={action} | status=SUCCESS | ip=127.0.0.1 | {details}\n")
    except Exception:
        pass


def list_users():
    """Lists all registered users in DRMT."""
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT id, username, created_at FROM users ORDER BY id ASC")
        users = cursor.fetchall()

        # Count active sessions
        try:
            s_cursor = conn.execute(
                "SELECT user_id, COUNT(*) FROM sessions WHERE expires_at > datetime('now', 'localtime') GROUP BY user_id"
            )
            session_counts = dict(s_cursor.fetchall())
        except Exception:
            session_counts = {}

        print("\n" + "=" * 65)
        print(f"{'ID':<6} {'Username':<18} {'Role':<12} {'Active Sessions':<16} {'Created At'}")
        print("=" * 65)
        for u in users:
            uid = u["id"]
            uname = u["username"]
            role = "Admin" if uname.lower() == "admin" else "Operator"
            created = str(u["created_at"]) if u["created_at"] else "--"
            active_s = session_counts.get(uid, 0)
            print(f"{uid:<6} {uname:<18} {role:<12} {active_s:<16} {created}")
        print("=" * 65)
        print(f"Total Users: {len(users)}\n")
    finally:
        conn.close()


def create_user(username: str, password: str):
    """Creates a new operator with bcrypt-hashed password."""
    username = username.strip()
    password = password.strip()

    if len(username) < 3:
        print("[-] Error: Username must be at least 3 characters long.")
        return False
    if len(password) < 4:
        print("[-] Error: Password must be at least 4 characters long.")
        return False

    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
        if cursor.fetchone():
            print(f"[-] Error: User '{username}' already exists.")
            return False

        pwd_hash = hash_password(password)
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, pwd_hash, now_ts)
        )
        log_audit_event(conn, username, "USER_CREATE", f"Created user '{username}' via CLI script")
        conn.commit()

        print("\n" + "─" * 55)
        print(" [✓] USER CREATED SUCCESSFULLY")
        print("─" * 55)
        print(f" • Username : {username}")
        print(f" • Password : {password}")
        print(f" • Role     : Operator")
        print(f" • Created  : {now_ts}")
        print("─" * 55)
        print(" You can now share these credentials with the operator to log in.\n")
        return True
    finally:
        conn.close()


def reset_password(username: str, new_password: str):
    """Resets the password for an existing user."""
    username = username.strip()
    new_password = new_password.strip()

    if len(new_password) < 4:
        print("[-] Error: Password must be at least 4 characters long.")
        return False

    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if not row:
            print(f"[-] Error: User '{username}' does not exist.")
            return False

        user_id = row["id"]
        pwd_hash = hash_password(new_password)

        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pwd_hash, user_id))
        log_audit_event(conn, username, "PASSWORD_CHANGE", f"Password reset for '{username}' via CLI script")
        conn.commit()

        print("\n" + "─" * 55)
        print(" [✓] PASSWORD RESET SUCCESSFULLY")
        print("─" * 55)
        print(f" • Username     : {username}")
        print(f" • New Password : {new_password}")
        print("─" * 55)
        print(" The user can now log into DRMT with their new password.\n")
        return True
    finally:
        conn.close()


def delete_user(username: str):
    """Deletes a user and their sessions (admin protected)."""
    username = username.strip()

    if username.lower() == "admin":
        print("[-] Error: Cannot delete primary 'admin' account.")
        return False

    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if not row:
            print(f"[-] Error: User '{username}' does not exist.")
            return False

        user_id = row["id"]

        # Delete active sessions first
        try:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        except Exception:
            pass

        # Delete user
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        log_audit_event(conn, username, "USER_DELETE", f"Deleted user '{username}' (id={user_id}) via CLI script")
        conn.commit()

        print(f"\n[✓] User '{username}' (id={user_id}) and active sessions deleted successfully.\n")
        return True
    finally:
        conn.close()


def interactive_menu():
    """Runs interactive menu in the terminal."""
    while True:
        print("=" * 45)
        print("         DRMT User Management Tool")
        print("=" * 45)
        print("  1) List all users")
        print("  2) Create new user")
        print("  3) Reset user password")
        print("  4) Delete user")
        print("  5) Exit")
        print("=" * 45)

        try:
            choice = input("Enter choice [1-5]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if choice == "1":
            list_users()
        elif choice == "2":
            uname = input("Enter new username: ").strip()
            pwd = input("Enter password: ").strip()
            if uname and pwd:
                create_user(uname, pwd)
        elif choice == "3":
            uname = input("Enter username to reset password: ").strip()
            pwd = input("Enter new password: ").strip()
            if uname and pwd:
                reset_password(uname, pwd)
        elif choice == "4":
            uname = input("Enter username to delete: ").strip()
            if uname:
                confirm = input(f"Are you sure you want to delete '{uname}'? (y/N): ").strip().lower()
                if confirm == "y":
                    delete_user(uname)
                else:
                    print("Deletion cancelled.")
        elif choice == "5" or choice.lower() == "q":
            print("Goodbye.")
            break
        else:
            print("[-] Invalid choice. Please select 1, 2, 3, 4, or 5.\n")


def print_help():
    print("""
DRMT User Management CLI Tool
==============================
Usage:
  Interactive Menu:
    python3 manage_users.py

  Direct Commands:
    python3 manage_users.py list
    python3 manage_users.py create <username> <password>
    python3 manage_users.py reset <username> <new_password>
    python3 manage_users.py delete <username>
""")


def main():
    if len(sys.argv) == 1:
        interactive_menu()
        return

    cmd = sys.argv[1].lower()
    if cmd in ("list", "ls"):
        list_users()
    elif cmd in ("create", "add"):
        if len(sys.argv) < 4:
            print("[-] Usage: python3 manage_users.py create <username> <password>")
            sys.exit(1)
        create_user(sys.argv[2], sys.argv[3])
    elif cmd in ("reset", "passwd", "password"):
        if len(sys.argv) < 4:
            print("[-] Usage: python3 manage_users.py reset <username> <new_password>")
            sys.exit(1)
        reset_password(sys.argv[2], sys.argv[3])
    elif cmd in ("delete", "del", "remove", "rm"):
        if len(sys.argv) < 3:
            print("[-] Usage: python3 manage_users.py delete <username>")
            sys.exit(1)
        delete_user(sys.argv[2])
    elif cmd in ("help", "-h", "--help"):
        print_help()
    else:
        print(f"[-] Unknown command '{cmd}'.")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
