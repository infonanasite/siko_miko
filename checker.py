"""
Netflix account checker – validates cookies and extracts account details.
Includes country flag mapping and file saving.
"""

import re
import json
import requests
from pathlib import Path
from http.cookiejar import MozillaCookieJar
from tempfile import NamedTemporaryFile
from typing import Dict, Optional, Tuple

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36",
    "Accept": "*/*",
    "Pragma": "no-cache",
}

# Country code to flag emoji (partial list – extend as needed)
COUNTRY_FLAGS = {
    "US": "🇺🇸", "GB": "🇬🇧", "CA": "🇨🇦", "AU": "🇦🇺", "DE": "🇩🇪", "FR": "🇫🇷",
    "IT": "🇮🇹", "ES": "🇪🇸", "BR": "🇧🇷", "MX": "🇲🇽", "IN": "🇮🇳", "JP": "🇯🇵",
    "KR": "🇰🇷", "NL": "🇳🇱", "SE": "🇸🇪", "NO": "🇳🇴", "DK": "🇩🇰", "FI": "🇫🇮",
    "PL": "🇵🇱", "TR": "🇹🇷", "AR": "🇦🇷", "CL": "🇨🇱", "CO": "🇨🇴", "PE": "🇵🇪",
    "ZA": "🇿🇦", "NG": "🇳🇬", "EG": "🇪🇬", "SA": "🇸🇦", "AE": "🇦🇪",
    # Add more as needed
}

def country_to_flag(country_code: str) -> str:
    """Convert two-letter country code to flag emoji."""
    if not country_code:
        return "🌍"
    code = country_code.upper()
    return COUNTRY_FLAGS.get(code, f"🏳️[{code}]")

def _parse_netscape(content: str) -> requests.Session:
    """Convert Netscape cookie file content into a requests Session with cookies."""
    with NamedTemporaryFile(mode='w+', suffix='.txt', delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        cj = MozillaCookieJar(tmp.name)
        cj.load(ignore_discard=True, ignore_expires=True)
    sess = requests.Session()
    sess.cookies = cj
    return sess

def _extract_json_from_html(html: str) -> Optional[Dict]:
    """Find the prefetchedData JSON inside Netflix account page."""
    match = re.search(r'<script[^>]*id="prefetchedData"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

def check_netflix_account(cookie_content: str, save_to_file: bool = True) -> Dict:
    """
    Validate a Netflix cookie and return account details.
    If save_to_file=True and account is valid, creates a file in cookies/netflix/
    with name format: [flag][email:pass][plan].txt
    """
    result = {
        "valid": False,
        "email": "",
        "password": "",  # extracted from content if possible
        "plan": "",
        "country": "",
        "max_streams": "",
        "video_quality": "",
        "payment_method": "",
        "membership_status": "",
        "flag": "🌍",
        "error": None,
    }

    # 1. Build session with cookies
    try:
        if cookie_content.strip().startswith("# Netscape HTTP Cookie File"):
            session = _parse_netscape(cookie_content)
        else:
            # Assume raw cookie header string (name=value; ...)
            session = requests.Session()
            session.headers.update({"Cookie": cookie_content.strip()})
    except Exception as e:
        result["error"] = f"Cookie parsing failed: {e}"
        return result

    # Attempt to extract email:password from common patterns in the content
    # (if present as plain text in the file)
    lines = cookie_content.splitlines()
    for line in lines:
        if ':' in line and ('@' in line or 'email' in line.lower()):
            parts = line.strip().split(':', 1)
            if len(parts) == 2:
                result["email"] = parts[0].strip()
                result["password"] = parts[1].strip()
                break
    # If not found, will be overwritten by website extraction later

    # 2. Get country via OneTrust geolocation
    try:
        resp = session.get(
            "https://geolocation.onetrust.com/cookieconsentpub/v1/geo/location",
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            result["error"] = f"Geolocation request failed (HTTP {resp.status_code})"
            return result
        data = resp.json()
        country = data.get("country", "")
        if not country:
            result["error"] = "Country not found in geolocation response"
            return result
        result["country"] = country
        result["flag"] = country_to_flag(country)
    except Exception as e:
        result["error"] = f"Geolocation error: {e}"
        return result

    # 3. Fetch membership page
    try:
        resp = session.get(
            "https://www.netflix.com/account/membership",
            headers=HEADERS,
            timeout=15,
            allow_redirects=False,
        )
        if resp.status_code != 200:
            result["error"] = f"Membership page not accessible (HTTP {resp.status_code})"
            return result
        html = resp.text
    except Exception as e:
        result["error"] = f"Membership page request failed: {e}"
        return result

    # 4. Extract account details
    prefetched = _extract_json_from_html(html)
    if not prefetched:
        # Fallback regex extraction
        def extract(pattern: str) -> str:
            m = re.search(pattern, html)
            return m.group(1) if m else ""

        email = extract(r'"emailAddress":\s*"\s*([^"]+)\s*"')
        plan = extract(r'"localizedPlanName":\s*\{\s*"fieldType":\s*"String",\s*"value":\s*"\s*([^"]+)\s*"\s*\}')
        max_streams = extract(r'"maxStreams":\s*\{\s*"fieldType":\s*"Numeric",\s*"value":\s*(\d+)\s*\}')
        video_quality = extract(r'"videoQuality":\s*\{\s*"fieldType":\s*"String",\s*"value":\s*"\s*([^"]+)\s*"\s*\}')
        payment_method = extract(r'"paymentMethod":\s*\{\s*"fieldType":\s*"String",\s*"value":\s*"\s*([^"]+)\s*"\s*\}')

        if '"membershipStatus":"CURRENT_MEMBER"' in html or '"CURRENT_MEMBER":true' in html:
            membership = "CURRENT_MEMBER"
        elif '"FORMER_MEMBER":true' in html or '"membershipStatus":"FORMER_MEMBER"' in html:
            membership = "FORMER_MEMBER"
        elif '"NEVER_MEMBER":true' in html or '"membershipStatus":"NEVER_MEMBER"' in html:
            membership = "NEVER_MEMBER"
        else:
            membership = "UNKNOWN"

        result.update({
            "email": email,
            "plan": plan,
            "max_streams": max_streams,
            "video_quality": video_quality,
            "payment_method": payment_method,
            "membership_status": membership,
        })
    else:
        try:
            user_info = prefetched.get("models", {}).get("userInfo", {})
            membership_info = prefetched.get("models", {}).get("membershipInfo", {})
            plan_info = prefetched.get("models", {}).get("planInfo", {})

            result["email"] = user_info.get("email", "")
            result["plan"] = plan_info.get("localizedPlanName", {}).get("value", "")
            result["max_streams"] = str(plan_info.get("maxStreams", {}).get("value", ""))
            result["video_quality"] = plan_info.get("videoQuality", {}).get("value", "")
            result["payment_method"] = membership_info.get("paymentMethod", {}).get("value", "")
            status = user_info.get("membershipStatus")
            result["membership_status"] = status if status else "UNKNOWN"
        except Exception as e:
            result["error"] = f"JSON parsing error: {e}"
            return result

    # If email not found in cookie content earlier, use extracted one
    if not result["email"]:
        result["email"] = result.get("email", "")

    # 5. Final validity check
    if result["membership_status"] == "CURRENT_MEMBER" and result["email"]:
        result["valid"] = True
        # Save to file if requested
        if save_to_file:
            _save_account_file(cookie_content, result)
    else:
        result["valid"] = False
        if not result["error"]:
            result["error"] = f"Membership status: {result['membership_status']}"

    return result

def _save_account_file(cookie_content: str, result: Dict) -> Optional[Path]:
    """Save the Netscape cookie file with descriptive name."""
    try:
        # Sanitize email and plan for filename
        email = result["email"].replace("/", "_").replace("\\", "_")
        plan = result["plan"].replace("/", "_").replace("\\", "_").replace(" ", "_")
        flag = result["flag"]
        filename = f"{flag}[{email}][{plan}].txt"
        # Ensure directory exists
        dir_path = Path("cookies/netflix")
        dir_path.mkdir(parents=True, exist_ok=True)
        filepath = dir_path / filename
        filepath.write_text(cookie_content, encoding="utf-8")
        return filepath
    except Exception as e:
        # Log error but don't fail validation
        print(f"Failed to save cookie file: {e}")
        return None