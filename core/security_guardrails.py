import ipaddress
import re
from enum import Enum
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse


class ActionSafetyCategory(str, Enum):
    READ_ONLY = "READ_ONLY"
    SAFE_INTERACTION = "SAFE_INTERACTION"
    STATE_CHANGE = "STATE_CHANGE"
    DESTRUCTIVE = "DESTRUCTIVE"


# Blocked Cloud Metadata, Link-Local, and Dangerous Endpoints (SSRF Protection)
BLOCKED_METADATA_HOSTS = {
    "169.254.169.254",            # AWS / Azure / GCP Link-Local Metadata
    "metadata.google.internal",   # GCP Internal Metadata Service
    "100.100.100.200",            # Alibaba Cloud Metadata
    "instance-data",              # AWS EC2 Legacy
}

BLOCKED_PROTOCOLS = {"file", "ftp", "gopher", "dict", "ldap", "tftp", "telnet"}

DANGEROUS_PORTS = {22, 23, 25, 110, 135, 137, 138, 139, 445, 1433, 3306, 3389, 5432, 6379, 27017}


def validate_target_url_security(
    url_str: str,
    allow_local_dev: bool = True,
) -> Tuple[bool, str]:
    """
    Validates a target URL against SSRF, cloud metadata exfiltration, and protocol injection.
    Allows local development targets (localhost, 127.0.0.1, private LAN) only when allow_local_dev is True.
    Always blocks cloud metadata endpoints (169.254.169.254) and dangerous infrastructure ports.
    """
    if not url_str or not str(url_str).strip():
        return False, "Target URL cannot be empty."

    clean = str(url_str).strip()

    # Android Native target allowance
    if clean.startswith("android://") or re.match(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$", clean):
        return True, ""

    try:
        parsed = urlparse(clean)
    except Exception as e:
        return False, f"Malformed URL syntax: {e}"

    scheme = (parsed.scheme or "").lower()
    if not scheme:
        if clean.startswith("localhost") or clean.startswith("127.0.0.1"):
            clean = "http://" + clean
            parsed = urlparse(clean)
            scheme = "http"
        else:
            return False, "Target URL must specify 'http://' or 'https://' protocol."

    if scheme in BLOCKED_PROTOCOLS:
        return False, f"Access to '{scheme}://' protocol is prohibited by security policy."

    if scheme not in ["http", "https"]:
        return False, f"Unsupported protocol '{scheme}'. Only 'http' and 'https' are allowed."

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "URL missing valid hostname or domain."

    # 1. Cloud Metadata Exfiltration Block
    if hostname in BLOCKED_METADATA_HOSTS or hostname.endswith(".internal"):
        return False, f"Security Violation: Target destination '{hostname}' is a protected cloud metadata service (SSRF blocked)."

    # 2. Port Restrictions
    port = parsed.port
    if port and port in DANGEROUS_PORTS:
        return False, f"Security Violation: Direct HTTP execution against infrastructure port {port} is prohibited."

    # 3. IP-based checks
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if ip_obj.is_link_local:
            return False, f"Security Violation: Link-local IP address '{hostname}' is prohibited."
        if ip_obj.is_multicast or ip_obj.is_unspecified:
            return False, f"Security Violation: Multicast or unspecified IP '{hostname}' is prohibited."
        if not allow_local_dev and (ip_obj.is_loopback or ip_obj.is_private):
            return False, f"Access to private/local IP '{hostname}' is disabled in this environment."
    except ValueError:
        # Hostname is a domain name, not a raw IP
        if not allow_local_dev and hostname in ["localhost", "127.0.0.1"]:
            return False, "Access to localhost/loopback destinations is disabled in this environment."

    return True, ""


# Action Classifier Keywords
DESTRUCTIVE_KEYWORDS = {
    "delete", "remove", "drop", "purge", "destroy", "truncate", "wipe",
    "clear all", "reset database", "terminate", "kill", "cancel account",
    "buy now", "pay now", "checkout", "transfer funds", "refund",
}

STATE_CHANGE_KEYWORDS = {
    "submit", "save", "update", "create", "add new", "insert", "edit",
    "upload", "import", "post", "send", "apply", "confirm", "register",
}

SAFE_INTERACTION_KEYWORDS = {
    "close", "dismiss", "cancel", "open drawer", "open modal", "expand",
    "collapse", "dropdown", "toggle", "popup", "select",
}


def classify_action_intent(
    action_text: str,
    element_tag: str = "button",
    attributes: Optional[Dict[str, str]] = None,
) -> ActionSafetyCategory:
    """
    Classifies user/crawler interactive element into safety categories:
    - READ_ONLY: Safe navigation, tab switching, search, filtering.
    - SAFE_INTERACTION: Opening/closing drawers, accordions, dialog dismissals.
    - STATE_CHANGE: Form submissions, creating/updating records.
    - DESTRUCTIVE: Irreversible record deletions, database drops, payments.
    """
    txt = (action_text or "").strip().lower()
    attrs = attributes or {}
    attr_str = " ".join(str(v) for v in attrs.values()).lower()
    combined = f"{txt} {attr_str}"

    if any(k in combined for k in DESTRUCTIVE_KEYWORDS):
        return ActionSafetyCategory.DESTRUCTIVE

    if any(k in combined for k in STATE_CHANGE_KEYWORDS):
        return ActionSafetyCategory.STATE_CHANGE

    if any(k in combined for k in SAFE_INTERACTION_KEYWORDS):
        return ActionSafetyCategory.SAFE_INTERACTION

    return ActionSafetyCategory.READ_ONLY
