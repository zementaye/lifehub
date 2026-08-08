"""One-off script: promote an existing account to admin.

Run this once, from the project root, wherever your real database lives
(same environment/env vars as the live app — e.g. locally with DATA_DIR
pointed at your data volume, or via `render shell` / `railway run` / SSH
into wherever it's actually deployed):

    python make_admin.py zemexasma@gmail.com

If the account doesn't exist yet, register it normally first (via /register)
and then run this.
"""

import sys

import db


def main():
    if len(sys.argv) != 2:
        print("Usage: python make_admin.py <email>")
        sys.exit(1)

    email = sys.argv[1].strip().lower()
    user = db.get_user_by_email(email)
    if not user:
        print(f"No account found for {email} — register it first, then re-run this.")
        sys.exit(1)

    db.admin_set_admin(user["id"], True)
    print(f"{email} is now an admin.")
    print("Log in as usual, or use \"{}/admin\" as the email at login to jump straight to the admin panel.".format(email))


if __name__ == "__main__":
    main()
