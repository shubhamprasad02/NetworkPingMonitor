import re
import secrets
from datetime import datetime, timedelta

import database

SESSION_TTL_HOURS = database.SESSION_TTL_HOURS
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ensure_data_dir():
    database.DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_sessions():
    return {}


def save_sessions():
    return


sessions = load_sessions()


def load_users():
    return []


def save_users(users):
    return


def hash_password(password, salt=None):
    return database.hash_password(password)


def verify_password(password, salt, password_hash):
    if salt and ":" not in password_hash:
        stored = f"{salt}:{password_hash}"
    else:
        stored = password_hash
    return database.verify_password(password, stored)


def find_user_by_email(email):
    return database.get_user_by_email(email)


def find_user_by_id(user_id):
    return database.get_user_by_id(user_id)


def validate_email(email):
    normalized = email.strip().lower()

    if not normalized or not EMAIL_PATTERN.match(normalized):
        raise ValueError("Enter a valid email address")

    return normalized


def validate_password(password):
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters")

    return password


def create_user(company, email, password):
    company_name = company.strip()
    normalized_email = validate_email(email)
    safe_password = validate_password(password)

    if not company_name:
        raise ValueError("Company name is required")

    if find_user_by_email(normalized_email):
        raise ValueError("An account with this email already exists")

    user = database.create_user_record(
        name=company_name,
        email=normalized_email,
        password=safe_password,
        company_name=company_name,
    )
    database.bootstrap_user_data(user["id"], company_name)
    return database.public_user_web(user)


def authenticate_user(email, password):
    normalized_email = validate_email(email)
    safe_password = validate_password(password)
    user = find_user_by_email(normalized_email)

    if not user or not database.verify_password(safe_password, user["password_hash"]):
        raise ValueError("Invalid email or password")

    return user


def create_session(user_id):
    return database.create_session(user_id, ttl_hours=SESSION_TTL_HOURS)


def get_session(token):
    session = database.get_session(token)
    if not session:
        return None

    return {
        "user_id": session["user_id"],
        "expires_at": datetime.fromisoformat(session["expires_at"]),
    }


def delete_session(token):
    database.delete_session(token)


def change_password(user_id, current_password, new_password):
    user = find_user_by_id(user_id)

    if not user:
        raise ValueError("User not found")

    safe_current = validate_password(current_password)
    safe_new = validate_password(new_password)

    if not database.verify_password(safe_current, user["password_hash"]):
        raise ValueError("Current password is incorrect")

    if safe_current == safe_new:
        raise ValueError("New password must be different from current password")

    database.update_user_password(user_id, database.hash_password(safe_new))
    return True


def public_user(user):
    return database.public_user_web(user)


def update_user_company(user_id, company):
    company_name = company.strip()

    if not company_name:
        raise ValueError("Company name is required")

    user = find_user_by_id(user_id)
    if not user:
        raise ValueError("User not found")

    database.update_user_company_name(user_id, company_name)
    updated = find_user_by_id(user_id)
    return database.public_user_web(updated)


def ensure_demo_user():
    demo_email = "demo@netwatch.local"

    if find_user_by_email(demo_email):
        return

    create_user("Demo Company", demo_email, "password")
