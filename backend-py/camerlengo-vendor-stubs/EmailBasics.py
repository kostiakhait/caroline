##################################################################################
#### Camerlengo Universal Swiss Toolkit and Web Server                        ####
#### Copyright (C) Konstantin Khait @ Partners Solutions, 2015-2025           ####
#### All rights reserved                                                      ####
#### Must be licensed before use                                              ####
##################################################################################

import requests
import json
import imaplib
import base64
import datetime
import time
import Config
from email.header import decode_header
from email.mime.base import MIMEBase
from email import encoders
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.header import Header
from email.utils import make_msgid, formatdate, parseaddr
import email
from datetime import datetime, timezone
from time import struct_time, strptime
import re
import html
import hashlib, smtplib

# Bug fix (2026-09-26), confirmed live: send() below had no timeout at all on its SMTP
# connection, so a wrong port or a dead/unreachable relay hung the calling request (and,
# for callers that block an outer session on it, that whole session) forever instead of
# failing with a clear error. Applies to every smtplib.SMTP()/SMTP_SSL() call in this
# module.
SMTP_CONNECT_TIMEOUT_S = 20

# Function to convert an email to a hashed ID
def email_to_hash(email):
    # Calculate SHA-256 hash of the email
    sha256_hash = hashlib.sha256(email.encode()).hexdigest()
    
    # Take the first eight characters of the hash
    short_hash = sha256_hash[:8]
    print(sha256_hash)  # Print full SHA-256 hash for debugging
    
    # Format the result as two groups of four characters separated by a dash
    formatted_hash = f"{short_hash[:4]}-{short_hash[4:]}"
    
    return formatted_hash


def _decode_header_text(value):
    """Robustly decodes a raw email header value into proper Unicode text --
    handles BOTH RFC 2047 encoded-words (=?charset?Q/B?...?=, the common
    case, via decode_header) AND raw 8-bit non-ASCII bytes sent directly in
    a header without RFC 2047 wrapping (technically invalid per RFC 5322
    but common from older/misconfigured mail agents, confirmed live
    2026-09-09 on a real message's From header).

    Why this is needed: email.message_from_bytes() (the plain/Compat32
    policy this whole file uses) decodes EVERY header line via ASCII with
    errors='surrogateescape' at parse time, regardless of its real charset
    -- so a raw 8-bit header survives as a string of lone surrogates
    encoding the ORIGINAL bytes. decode_header() alone only unwraps genuine
    =?...?= tokens; it passes a non-RFC2047 string through UNCHANGED,
    surrogates and all. Those surrogates then leak into the v2 API's JSON
    response and get mangled into U+FFFD replacement characters wherever
    the transport eventually tries to UTF-8-encode them -- found live via
    Caroline's email:listMessages/getMessage commands (a message with a
    Cyrillic display name straight in the From header, no RFC2047
    wrapping). Recovers the original bytes via surrogateescape and
    re-decodes as UTF-8 first (the common case even for "raw" headers
    today), falling back to windows-1251 (this deployment's most likely
    non-UTF-8 sender charset) with errors='replace' as a last resort so
    this never raises."""
    if not value:
        return value
    try:
        decoded_parts = decode_header(value)
    except Exception:
        decoded_parts = [(value, None)]
    out = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            try:
                out.append(part.decode(encoding or "utf-8"))
            except (LookupError, UnicodeDecodeError):
                out.append(part.decode("utf-8", "replace"))
            continue
        if any(0xDC80 <= ord(ch) <= 0xDCFF for ch in part):
            raw = part.encode("utf-8", "surrogateescape")
            try:
                part = raw.decode("utf-8")
            except UnicodeDecodeError:
                part = raw.decode("windows-1251", "replace")
        out.append(part)
    return "".join(out)


# Per explicit instruction (2026-10-16), confirmed live: Caroline sent a
# plain-text email (html_format=False) whose body literally contained "<br>"
# tags -- the caller/model meant them as line breaks, but with html_format
# False this function has always attached _msg byte-for-byte as text/plain,
# so the recipient saw the literal tag text instead of a line break. This is
# the general, recurring shape of the bug ("старый баг с html-тегами"), not
# one caller's mistake: fixed once, here, for every caller of send() across
# this whole codebase (Caroline's own email:send AND Anvar's direct
# EmailBasics.send calls both go through this same function), rather than
# relying on each caller/model to remember never to do this.
_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r"</\s*(p|div|li|h[1-6])\s*>", re.IGNORECASE)


def _sanitize_plain_text(text):
    """When html_format is False, _msg is supposed to be plain text -- if it
    contains stray HTML markup (almost always the model intending `<br>` as
    a line break, never a deliberate choice for a plain-text body), convert
    what can be converted to its plain-text equivalent and strip the rest,
    instead of sending literal tag text to the recipient. A cheap no-op for
    the overwhelming common case (no '<'/'>' at all, or a true plain-text
    comparison like "x < 5" -- the tag regex requires a letter or '/'
    immediately after '<', which an ordinary less-than never has)."""
    if not text or "<" not in text or ">" not in text or not _HTML_TAG_RE.search(text):
        return text
    cleaned = _BR_RE.sub("\n", text)
    cleaned = _BLOCK_CLOSE_RE.sub("\n\n", cleaned)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    cleaned = html.unescape(cleaned).replace("\xa0", " ")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def send(_from, _to, _msg, _subject="",
         server=None, port=None,
         login=None, password=None, html_format=False,
         cc=None, bcc=None,
         attachments=None,
         inline_images=None,
         plain_text=None,
         save_to_sent=False,
         imap_server=None, imap_port=993,
         in_reply_to=None, references=None):
    """
    Sends an email with support for CC/BCC and OPTIONAL attachments and OPTIONAL inline images.

    attachments format (optional):
      - list of dicts like Mailbox.downloadEmailAttachments():
          {
            "filename": "file.pdf",
            "content": "<base64>",
            "content_type": "application/pdf"
          }

    inline_images format (optional; used only when html_format=True):
      - list of dicts:
          {
            "cid": "logo",
            "filename": "logo.png",              # optional but recommended
            "content": "<base64>",
            "content_type": "image/png"          # recommended
          }
      In HTML use:
          <img src="cid:logo">

    plain_text (optional; used only when html_format=True):
      - if provided, will be sent as text/plain alongside text/html (multipart/alternative)

    save_to_sent (optional; default False):
      - if True, IMAP-APPENDs a copy of the sent message into the "Sent" folder
        of the account given by login/password (imap_server/imap_port, default
        Config.EMAIL_SERVER:993). Opt-in and off by default: smtplib alone has
        no notion of a Sent folder, and most callers of this shared function
        (used across every product on this backend) send through the generic
        EMAIL_USER relay account, which either has no meaningful personal Sent
        folder or isn't the account the message logically belongs to. Only
        pass True when login/password identify a real mailbox the sender
        should visibly own a Sent copy in (e.g. TunnelCat's cat@navlink.net --
        see cmdSendEmail in Commands.py). A failure here is logged but never
        raised -- the SMTP send already succeeded and recipients already have
        the email; a missing Sent-folder copy is a cosmetic gap, not a
        delivery failure, so it must not make send() report an error it
        didn't have.
    """

    # Default values from Config if not provided
    if not server:
        server = Config.EMAIL_SERVER
    if not port:
        port = Config.EMAIL_PORT
    if not login:
        login = Config.EMAIL_USER
    if not password:
        password = Config.EMAIL_PASSWORD

    # Set up SMTP server connection.
    #
    # Bug fix (2026-09-26), confirmed live: this always did the explicit-TLS handshake
    # (plain connect -> ehlo -> starttls), which is only valid for the submission port
    # (587/25). Port 465 is implicit-TLS (SMTPS): the server expects a TLS ClientHello
    # immediately, not a plaintext EHLO, so a caller who passed port 465 (a real,
    # live incident: Caroline's own email plugin did, for a mailbox whose saved
    # credentials simply didn't record a port) got no readable response and, combined
    # with the missing timeout above, hung indefinitely. Reproduced directly against
    # mail.partners.solutions: port 465 in this old STARTTLS shape timed out after 12s
    # with "Connection unexpectedly closed"; port 587 against the exact same account
    # completed in under 2s. Auto-detect instead of assuming: SMTP_SSL for 465, the
    # existing STARTTLS path for everything else.
    if int(port) == 465:
        smtp = smtplib.SMTP_SSL(server, port, timeout=SMTP_CONNECT_TIMEOUT_S)
    else:
        smtp = smtplib.SMTP(server, port, timeout=SMTP_CONNECT_TIMEOUT_S)
        smtp.ehlo()
        smtp.starttls()
    smtp.login(login, password)

    has_attachments = bool(attachments)
    has_inline = bool(inline_images) and bool(html_format)

    # Prepare the email container
    # - if attachments: mixed (body + attachments)
    # - else if inline images: related (html + inline parts)
    # - else: alternative (plain or html only)
    if has_attachments:
        msg = MIMEMultipart("mixed")
    elif has_inline:
        msg = MIMEMultipart("related")
    else:
        msg = MIMEMultipart("alternative")

    msg.set_charset("utf8")
    msg["From"] = _from
    msg["To"] = ", ".join(_to) if isinstance(_to, list) else _to
    if cc:
        msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    msg["Subject"] = Header(_subject, "UTF-8").encode()
    # Message-ID/Date were never set here at all -- most spam filters treat
    # their absence as a strong signal on its own. Confirmed live 2026-08-10:
    # a test broadcast with message-id=<> (visible in Postfix's own cleanup
    # log line) got auto-filed straight into Junk despite passing SPF/DKIM.
    _, from_addr = parseaddr(_from)
    msg["Message-ID"] = make_msgid(domain=(from_addr.split("@", 1)[-1] or "partners.solutions"))
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references) if isinstance(references, list) else references

    # Build body as multipart/alternative (so we can include both text/plain and text/html)
    alternative_part = MIMEMultipart("alternative")
    if html_format:
        pt = plain_text if plain_text is not None else " "
        alternative_part.attach(MIMEText(_sanitize_plain_text(pt), "plain", "utf-8"))
        alternative_part.attach(MIMEText(_msg, "html", "utf-8"))
    else:
        alternative_part.attach(MIMEText(_sanitize_plain_text(_msg), "plain", "utf-8"))

    if has_inline:
        related_part = MIMEMultipart("related")
        related_part.attach(alternative_part)

        for img in (inline_images or []):
            if not isinstance(img, dict):
                continue
            cid = img.get("cid")
            content_b64 = img.get("content")
            content_type = img.get("content_type") or "application/octet-stream"
            filename = img.get("filename")

            if not cid or not content_b64:
                continue

            maintype, subtype = (content_type.split("/", 1) + ["octet-stream"])[:2]
            raw = base64.b64decode(content_b64)

            # For image/*, prefer MIMEImage to maximize client compatibility (Gmail is picky).
            if maintype == "image":
                part = MIMEImage(raw, _subtype=subtype)
            else:
                part = MIMEBase(maintype, subtype)
                part.set_payload(raw)
                encoders.encode_base64(part)

            part.add_header("Content-ID", f"<{cid}>")
            if filename:
                part.add_header("Content-Disposition", f'inline; filename="{filename}"')
            else:
                part.add_header("Content-Disposition", "inline")

            related_part.attach(part)

        msg.attach(related_part)
    else:
        msg.attach(alternative_part)

    # Attachments
    if has_attachments:
        for a in (attachments or []):
            if not isinstance(a, dict):
                continue
            filename = a.get("filename") or "attachment.bin"
            content_b64 = a.get("content")
            content_type = a.get("content_type") or "application/octet-stream"

            if not content_b64:
                continue

            maintype, subtype = (content_type.split("/", 1) + ["octet-stream"])[:2]
            part = MIMEBase(maintype, subtype)
            part.set_payload(base64.b64decode(content_b64))
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)

    # Combine all recipients (To, CC, BCC)
    all_recipients = set(_to if isinstance(_to, list) else [_to])
    if cc:
        all_recipients.update(cc if isinstance(cc, list) else [cc])
    if bcc:
        all_recipients.update(bcc if isinstance(bcc, list) else [bcc])

    # Send the email
    smtp.sendmail(_from, list(all_recipients), msg.as_string())
    # QUIT is a connection teardown courtesy — if it fails the email was already accepted
    # by the server (sendmail returned 250 OK). Suppress quit exceptions so callers
    # don't misinterpret a teardown error as a delivery failure.
    try:
        smtp.quit()
    except Exception:
        pass

    if save_to_sent:
        _append_to_sent(msg, login or Config.EMAIL_USER, password or Config.EMAIL_PASSWORD,
                         imap_server or Config.EMAIL_SERVER, imap_port)

    return msg["Message-ID"]

def _append_to_sent(msg, imap_login, imap_password, host, port):
    """Best-effort IMAP APPEND of an already-sent message into that account's
    Sent folder. Never raises -- see save_to_sent's doc note in send() for why
    this must stay silent-on-failure."""
    try:
        M = imaplib.IMAP4_SSL(host, port, timeout=Config.EMAIL_SOCKET_TIMEOUT if hasattr(Config, "EMAIL_SOCKET_TIMEOUT") else 10)
        try:
            M.login(imap_login, imap_password)
            # Folder name/casing varies by server; "Sent" matches what this
            # account's own \Sent-flagged folder is actually named (verified
            # against cat@navlink.net's mailbox on mail.partners.solutions).
            M.append("Sent", "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        finally:
            try:
                M.logout()
            except Exception:
                pass
    except Exception as e:
        print(f"_append_to_sent: failed to save copy to Sent for {imap_login}: {e}")

def sort_folders(folders):
    # Define sorting priority for each category of folders
    folder_priority = {
        "inbox": 1,
        "outbox": 2,
        "sent": 3,
        "draft": 4,
        "spam": 99,
        "trash": 100
    }
    
    # Sorting function that assigns a priority based on the folder name
    def folder_sort_key(folder):
        # Convert folder name to lowercase for case-insensitive comparison
        folder_name = folder.lower()
        # Return priority based on the folder category, defaulting to 50 for 'other' folders
        return folder_priority.get(folder_name, 50)
    
    # Sort folders using the custom key function
    sorted_folders = sorted(folders, key=folder_sort_key)
    return sorted_folders

def getFromEmail(addresses, email_only = False):
    if not addresses:
        return None
    longest_name = max((addr["name"] for addr in addresses if addr["name"]), key=len, default="")
    shortest_email = min((addr["email"] for addr in addresses if addr["email"]), key=len, default="")

    if email_only:
        if shortest_email:
            return shortest_email
        else:
            return None
    else:
        if longest_name and shortest_email:
            return f'"{longest_name}" <{shortest_email}>'
        elif shortest_email:
            return shortest_email
        else:
            return f'"{longest_name}" <no_mail@no-mail.com>'
    
def getToEmails(addresses):
    if not addresses:
        return None
    unique_emails = set()
    formatted_entries = []

    for addr in addresses:
        email = addr.get("email")
        name = addr.get("name", "")
        if email and email not in unique_emails:
            unique_emails.add(email)
            if name:
                entry = f'"{name}" <{email}>'
            else:
                entry = email
            formatted_entries.append(entry)

    result = "; ".join(formatted_entries)
    return result            

def convertDate(date_str):
    """
    Converts a date string to a struct_time object.
    Attempts common date formats if the input is not in ISO format.
    """
    # Attempt to parse ISO format first
    try:
        date = datetime.fromisoformat(date_str)
        return date.timetuple()
    except ValueError:
        pass

    # Common alternative date formats
    date_formats = [
        "%a, %d %b %Y %H:%M:%S %z",  # e.g., "Thu, 31 Oct 2024 01:22:53 -0500"
        "%a, %d %b %Y %H:%M:%S",  # e.g., "Thu, 31 Oct 2024 01:22:53 -0500"
        "%Y-%m-%d %H:%M:%S",          # e.g., "2024-10-31 01:22:53"
        "%d-%b-%Y %H:%M:%S %z",       # e.g., "31-Oct-2024 01:22:53 +0000"
        "%d-%b-%Y %H:%M:%S",       # e.g., "31-Oct-2024 01:22:53 +0000"
    ]

    # Try parsing using each format in the list
    for fmt in date_formats:
        try:
            date = datetime.strptime(date_str, fmt)
            current_struct_time =  date.timetuple()
            # Чтобы получить значение tm_gmtoff, мы выводим структуру struct_time с нашим offset
            current_struct_time_with_offset = time.struct_time((
                current_struct_time.tm_year,
                current_struct_time.tm_mon,
                current_struct_time.tm_mday,
                current_struct_time.tm_hour,
                current_struct_time.tm_min,
                current_struct_time.tm_sec,
                current_struct_time.tm_wday,
                current_struct_time.tm_yday,
                current_struct_time.tm_isdst,
                "GMT",
                0
            ))
            return current_struct_time_with_offset                
        except ValueError:
            continue

#### end of utility functions ####

class PSMailboxControl:
    """
    Class for managing email accounts using the HTTP API server.
    """

    def __init__(self, api_url : str = Config.EMAIL_PS_API):
        """
        Initialize the controller with the server URL.
        :param api_url: Base URL of the API server (e.g., http://localhost:3737)
        """
        self.api_url = api_url

    def _makeRequest(self, endpoint : str, params=None):
        """
        Make a GET request to the specified endpoint.
        :param endpoint: API command name.
        :param params: Dictionary of query parameters.
        :return: Parsed response or error message.
        """
        try:
            # Construct full URL
            url = f"{self.api_url}/{endpoint}"
            response = requests.get(url, params=params)

            # Raise exception for HTTP errors
            response.raise_for_status()

            # Parse JSON response
            result = response.json()

            # Check for errors in the server response
            if isinstance(result, dict) and 'error' in result:
                return {"error": result['error']}

            # Return successful result
            return result
        except requests.exceptions.ConnectionError:
            return {"error": "Unable to connect to the server."}
        except requests.exceptions.Timeout:
            return {"error": "The request timed out."}
        except requests.exceptions.RequestException as e:
            return {"error": f"Request failed: {e}"}
        except json.JSONDecodeError:
            return {"error": "Invalid JSON response from server."}

    def listDomains(self):
        """
        List all available domains.
        :return: List of domains or error message.
        """
        return self._makeRequest('list-domains')

    def listEmails(self, domain : str):
        """
        List all emails for a given domain.
        :param domain: Domain name.
        :return: List of emails or error message.
        """
        if not domain:
            return {"error": "Domain parameter is required."}
        return self._makeRequest('list-emails', {'domain': domain})

    def addEmail(self, email : str, password : str):
        """
        Add a new email account.
        :param email: Email address.
        :param password: Password for the email account.
        :return: Status message.
        """
        if not email or not password:
            return {"error": "Email and password parameters are required."}
        return self._makeRequest('add-email', {'email': email, 'password': password})

    def changePassword(self, email, new_password):
        """
        Change the password for an email account.
        :param email: Email address.
        :param new_password: New password.
        :return: Status message.
        """
        if not email or not new_password:
            return {"error": "Email and new password parameters are required."}
        return self._makeRequest('change-password', {'email': email, 'password': new_password})

    def removeEmail(self, email : str):
        """
        Remove an email account.
        :param email: Email address.
        :return: Status message.
        """
        if not email:
            return {"error": "Email parameter is required."}
        return self._makeRequest('remove-email', {'email': email})

class Mailbox():
    def __init__(self, server, login, password, port = None):
        self.imap_server = server
        self.login = login
        self.password = password
        self.port = port
        self.connection_attempts = 3
        self.download_attempts = 3
        self.language = None
        self.special_folders = []
        self.delimiter = None
    def _createNewConnection(self):
        """Create a new IMAP connection for each thread."""
        attempts = 0
        while attempts < self.connection_attempts:
            try:
                mail = imaplib.IMAP4_SSL(self.imap_server)
                mail.login(self.login, self.password)
                return mail
            except Exception as e:
                print(f"Connection attempt is not successful for {self.login}")
                time.sleep(5)
            attempts += 1
        return None
    
    def getDelimiter(self, mail):
        """
        Returns the folder delimiter used by the IMAP server.
        
        :param mail: An IMAP connection object.
        :return: The folder delimiter as a string, or None if not found.
        """
        # Cached delimiter
        if self.delimiter:
            return self.delimiter
        try:
            # Try to get the delimiter using NAMESPACE
            status, namespace_data = mail.namespace()
            if status == 'OK' and namespace_data:
                clause = namespace_data[0].decode()
                if '"."' in clause:
                    self.delimiter = "."
                    return "."
                elif '"/"' in clause:
                    self.delimiter = "/"
                    return "/"
        except Exception as e:
            # Handle cases where NAMESPACE is not supported
            print(f"Warning: namespace command failed: {e}. Trying list instead.")

            # Try LIST to infer delimiter
            try:
                status, list_data = mail.list()
                if status == 'OK' and list_data:
                    for item in list_data:
                        decoded_item = item.decode()
                        # Extract delimiter using regex
                        match = re.search(r'"(.+?)"', decoded_item)
                        if match:
                            self.delimiter = match.group(1)
                            return match.group(1) 
            except Exception as e_list:
                print(f"LIST command failed: {e_list}")
        
        # If all else fails, return None
        return None
    
    def _decodeAddress(self, address):
        """Parses the 'To' or 'From' fields into a list of dictionaries with
        'name' and 'email'. Decodes the WHOLE raw header value via
        _decode_header_text() FIRST, before handing it to
        email.utils.getaddresses() to split into (name, addr) pairs --
        getaddresses() expects a plain string and, given the raw
        email.header.Header object msg.get("From")/msg.get("To") actually
        returns for non-ASCII headers, silently stringifies it internally
        the same lossy way str(header) does (confirmed live 2026-09-09:
        getaddresses([raw_header_object]) mangled a Cyrillic display name
        into U+FFFD even with decode_header applied to the EXTRACTED name
        afterward -- the damage was already done by the time getaddresses
        got the raw object). Decoding first, splitting second, avoids
        getaddresses ever seeing anything but a plain, already-correct str."""
        address_list = []
        if address:
            addresses = email.utils.getaddresses([_decode_header_text(address)])
            for name, addr in addresses:
                address_list.append({"name": name or "", "email": addr})
        return address_list
    
    def fetchFolders(self, mail):
        """Fetches a list of all folders (mailboxes) from the server."""
        status, folders = mail.list()
        if status != 'OK':
            print("Failed to retrieve folders")
            return []

        folder_list = []
        for folder in folders:
            # Extract folder name by splitting on the folder hierarchy separator and ignoring flags
            parts = re.findall(r'"[^"]*"|\S+', folder.decode())
            # Remove quotes around quoted parts
            parts = [part.strip('"') for part in parts]
            if len(parts) > 1:
                folder_name = parts[-1].strip().strip('"')
                folder_list.append(folder_name)
        return sort_folders(folder_list)

    def fetchEmailUids(self, folder, mail, since = None):
        """Fetches UIDs of all emails from a specific folder using the provided IMAP connection."""
        try:
            result = mail.select(f'"{folder}"', readonly=True)  # Select folder in read-only mode
            if result[0] != 'OK':
                print(f"Error selecting folder {folder}: {result}")
                return []
        except imaplib.IMAP4.error as e:
            print(f"Error selecting folder {folder}: {e}")
            return []

        if since:
            since_date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%d-%b-%Y")
            status, messages = mail.uid('search', None, f'(SINCE {since_date})')
        else:
            status, messages = mail.uid('search', None, '(ALL)')
        if status != 'OK':
            print(f"Failed to retrieve messages from folder {folder}")
            return []
        return messages[0].split() if messages and messages[0] else []

    def downloadEmail(self, email_id, mail):
        attempt = 0
        while attempt < self.download_attempts:
            attempt += 1
            result = self.__downloadEmail(email_id, mail)
            if result:
                return result
            else:
                mail = self._createNewConnection()                              # Re-establish the connection
            print(f"Attempt {attempt} to download email {email_id}...")
        return None

    def __downloadEmail(self, email_id, mail):

        def process_part(email_message, part):
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if content_type == "multipart/related" or content_type == "multipart/mixed" or content_type == "multipart/alternative":
                payload = part.get_payload()
                for _part in payload:
                    process_part(email_message, _part)
                    if "body" in email_message.keys() and "type" in email_message.keys() and 'html' in email_message['type']:
                        break
            elif "attachment" not in content_disposition and (content_type == "text/plain" or content_type == "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        email_message["body"] = payload.decode('utf-8')
                        email_message["type"] = content_type
                except:
                    payload = part.get_payload(decode=True)
                    if payload:
                        email_message["body"] = payload.decode('latin1')

        try:
            folder, uid = email_id.split(":", 1)
            email_message = None

            status, _ = mail.select(f'"{folder}"', readonly=True)
            if status != 'OK':
                print(f"Error selecting folder {folder}")
                return None
            status, msg_data = mail.uid('fetch', uid, '(RFC822)')
            if status != 'OK':
                print(f"Error fetching email with UID {uid} from folder {folder}")
                return None

            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])

                    subject = msg["Subject"]
                    subject = _decode_header_text(subject) if subject is not None else ""

                    from_ = msg.get("From")
                    to = msg.get("To")
                    cc = msg.get("Cc")  # Get the Cc field
                    reply_to = msg.get("Reply-To")  # Get the Reply-To field
                    date = msg.get("Date")
                    if not date:
                        date = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")

                    message_id = str(msg.get("Message-Id") or "").strip()
                    in_reply_to = str(msg.get("In-Reply-To") or "").strip()
                    references = str(msg.get("References") or "").strip()

                    email_message = {
                        "uid": f"{folder}:{uid}",
                        "subject": subject,
                        "from": self._decodeAddress(from_),
                        "to": self._decodeAddress(to),
                        "cc": self._decodeAddress(cc) if cc else None,  # Decode Cc if present
                        "reply_to": self._decodeAddress(reply_to) if reply_to else None,  # Decode Reply-To if present
                        "date": date,
                        "folder": folder,
                        "body": "",
                        "attachments": [],
                        "Received": msg.get("Received"),
                        "X-NTF": msg.get("X-NTF", None),
                        "X-NTF-ID" : msg.get("X-NTF-ID", None),
                        "X-LANG": msg.get("X-LANG", None),
                        "X-ORIG-LANG": msg.get("X-ORIG-LANG", None),
                        "X-ORIG-SUBJ": msg.get("X-ORIG-SUBJ", None),
                        "message_id": message_id,
                        "Message-Id": message_id,
                        "in_reply_to": in_reply_to,
                        "In-Reply-To": in_reply_to,
                        "references": references,
                        "References": references,
                    }

                    if msg.is_multipart():
                        for part in msg.get_payload():
                            process_part(email_message, part)
                    else:
                        # For non-multipart messages, handle plain text body
                        content_type = msg.get_content_type()
                        try:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                email_message["body"] = payload.decode('utf-8')
                        except:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                email_message["body"] = payload.decode('latin1')
                        email_message["type"] = content_type
            print(f"Email loaded for {email_id}")
            return email_message
        except Exception as e:
            return None

    def downloadEmailAttachments(self, email_data, mail, reload=False):
        """Downloads all attachments for an email by its folder:UID, skips already downloaded attachments."""
        email_id = email_data["uid"]
        folder, uid = email_id.split(":", 1)

        if "attachments_loaded" in email_data and (email_data["attachments_loaded"] == True) and not reload:
            print(f"All attachments already loaded for: {email_id}")
            return email_data

        try:
            result = mail.select(f'"{folder}"', readonly=True)
            if result[0] != 'OK':
                print(f"Error selecting folder {folder} for downloading attachments: {result}")
                return email_data
        except imaplib.IMAP4.error as e:
            print(f"Error selecting folder {folder}: {e}")
            return email_data

        status, msg_data = mail.uid('fetch', uid, '(RFC822)')
        if status != 'OK':
            print(f"Error fetching email with UID {email_id}")
            return email_data

        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])

                if msg.is_multipart():
                    for part in msg.walk():
                        content_disposition = str(part.get("Content-Disposition"))

                        if "attachment" in content_disposition:
                            original_filename = part.get_filename()

                            try:
                                attachment_data = part.get_payload(decode=True)

                                attachment_info = {
                                    "filename": original_filename,
                                    "content": base64.b64encode(attachment_data).decode(),
                                    "content_type": part.get_content_type(),
                                }

                                print(f"Downloaded and saved attachment {original_filename}")
                                if "attachments" not in email_data.keys():
                                    email_data["attachments"] = []
                                email_data["attachments"].append(attachment_info)

                            except Exception as e:
                                print(f"Error saving attachment {original_filename}: {e}")

        email_data["attachments_loaded"] = True
        print(f"Attachments loaded for {email_id}")
        return email_data
    
    def downloadAllEmailsFromFolder(self, folder, mail, with_attachments = False):
        result = []
        uids = self.fetchNonDeletedUIDs(folder, mail)
        for uid in uids:
            email_id = f"{folder}:{uid.decode()}"
            email = self.downloadEmail(email_id, mail)
            if email and with_attachments:
                email = self.downloadEmailAttachments(email, mail)
            result.append(email)
            time.sleep(0.5)
        return result

    def upload(self, email_data, folder, mail):
        # MIME message structure with 'mixed' to handle both body and attachments
        msg = MIMEMultipart('mixed')
        msg["From"] = getFromEmail(email_data.get("from", []))
        msg["To"] = getToEmails(email_data.get("to", []))
        msg["Cc"] = getToEmails(email_data.get("cc", []))
        msg["Subject"] = email_data.get("subject", "")
        now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")
        if 'date' not in email_data or not email_data["date"]:
            email_data["date"] = now
        msg["Date"] = email_data.get("date", now)

        # Add all attributes that shall be preserved during the upload
        for key in email_data.keys():
            if len(key) > 2 and key[0:2] == 'X-':
                msg[key] = email_data.get(key, "")

        # Create alternative part to distinguish text content types
        alternative_part = MIMEMultipart('alternative')
        
        # Adding email body
        body = email_data.get("body", "")
        body_type = email_data.get("type", "plain").replace("text/", "")
        text_part = MIMEText(body, body_type)
        alternative_part.attach(text_part)
        
        # Attach the alternative part (text/plain or text/html) to the main message
        msg.attach(alternative_part)

        # Adding attachments
        for attachment in email_data.get("attachments", []):
            part = MIMEBase(*attachment["content_type"].split("/"))
            part.set_payload(base64.b64decode(attachment["content"]))  # Decode the base64 content
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={attachment['filename']}")
            msg.attach(part)

        # Preparing to upload to the server
        try:
            raw_email = msg.as_bytes()
            now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")
            if 'date' not in email_data or not email_data["date"]:
                email_data["date"] = now
            date_time = convertDate(email_data.get("date", now))

            mail.append(folder, None, date_time, raw_email)

            # Retrieve UID of the uploaded email
            mail.select(f'"{folder}"')
            result, data = mail.search(None, 'ALL')
            if result == 'OK':
                uids = data[0].split()
                uploaded_uid = uids[-1]  # UID of the latest email in the folder
                print(f"Email uploaded successfully with UID: {uploaded_uid}")
                return uploaded_uid
            else:
                print("Failed to retrieve UID after upload.")
                return None

        except Exception as e:
            print("Failed to upload email:", e)
            return None

    # Additional methods for deleting, copying, moving, and managing folders
    def deleteEmail(self, email_id, mail, purge = False):
        """Deletes an email both locally and on the server."""
        folder, uid = email_id.split(":")
        try:
            try:
                # Deleting from the server
                mail.select(f'"{folder}"', readonly=False)
                uid = uid.encode("ascii")
                result, _ = mail.uid('store', uid, '+FLAGS', '\\Deleted')
                if result == 'OK':
                    if purge:
                        mail.expunge()
                    print(f"Email {email_id} successfully deleted from the server.")
                    return True
                else:
                    print(f"Error deleting email {email_id} from the server.")
                    return False
            except Exception as e:
                return False

        except Exception as e:
            return False

    # Additional methods for deleting, copying, moving, and managing folders
    def purge(self, folder, mail):
        """Deletes an email both locally and on the server."""
        try:
            # Deleting from the server
            mail.select(f'"{folder}"', readonly=False)
            mail.expunge()
            print(f"Purge folder {folder}.")
            return True
        except Exception as e:
            return False

    def copyEmail(self, email_id, destination_folder, mail):
        """Copies an email both locally and on the server."""
        folder, uid = email_id.split(":")

        # Copying on the server
        mail.select(f'"{folder}"', readonly=False)
        result = mail.uid('copy', uid, f'"{destination_folder}"')
        if result[0] == 'OK':
            print(f"Email {email_id} successfully copied to {destination_folder} on the server.")
            return True
        else:
            print(f"Error copying email {email_id} on the server.")
            return False

    def moveEmail(self, email_id, destination_folder, mail):
        """Moves an email both locally and on the server."""
        # Moving on the server: copy and then delete
        if self.copyEmail(email_id, destination_folder, mail):
            return self.deleteEmail(email_id, mail)
        else:
            return False

    def createFolder(self, folder_name, mail):
        """Creates a new folder on the server."""
        result = mail.create(f'"{folder_name}"')
        if result[0] == 'OK':
            print(f"Folder {folder_name} successfully created on the server.")
            return True
        else:
            print(f"Error creating folder {folder_name} on the server.")
            return False

    def deleteFolder(self, folder_name, mail):
        """Deletes a folder both on the server and locally."""
        result = mail.delete(f'"{folder_name}"')
        if result[0] == 'OK':
            print(f"Folder {folder_name} successfully deleted on the server.")
            return True
        else:
            print(f"Error deleting folder {folder_name} on the server.")
            return False
        
    def renameFolder(self, current_name, new_name, mail):
        # Rename the folder from `current_name` to `new_name`
        status, response = mail.rename(f'"{current_name}"', f'"{new_name}"')
        
        if status == "OK":
            print(f"Folder renamed from '{current_name}' to '{new_name}' successfully.")
            return True
        else:
            print(f"Failed to rename folder: {response}")   
            return False     

    def fetchNonDeletedUIDs(self, folder, mail, since = None):
        """
        Fetches UIDs of all non-deleted emails in the specified folder.
        :param mail: Active IMAP connection object.
        :param folder: The folder to search (default is "INBOX").
        :return: List of UIDs for non-deleted emails.
        """
        # Select the folder
        status, _ = mail.select(f'"{folder}"', readonly=True)
        if status != "OK":
            return []

        # Search for all non-deleted emails
        if since:
            since_date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%d-%b-%Y")
            status, messages = mail.uid('search', None, f'(UNDELETED SINCE {since_date})')
        else:
            status, messages = mail.uid('search', None, '(UNDELETED)')
        if status == 'OK':
            return messages[0].split() if messages and messages[0] else []
        else:
            print("Error fetching non-deleted UIDs")
            return []
        
    def fetchUnreadUIDs(self, folder, mail, since = None):
        """Fetches UIDs of all unread emails from a specific folder."""
        try:
            result = mail.select(f'"{folder}"', readonly=True)  # Select folder in read-only mode
            if result[0] != 'OK':
                print(f"Error selecting folder {folder}: {result}")
                return []
        except imaplib.IMAP4.error as e:
            print(f"Error selecting folder {folder}: {e}")
            return []

        # Use "UNSEEN" instead of "ALL" to fetch only unread emails
        if since:
            since_date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%d-%b-%Y")
            status, messages = mail.uid('search', None, f'(UNSEEN SINCE {since_date})')
        else:
            status, messages = mail.uid('search', None, '(UNSEEN)')
        if status != 'OK':
            print(f"Failed to retrieve unread messages from folder {folder}")
            return []
        return messages[0].split() if messages and messages[0] else []

    def fetchUnreadMsgIds(self, folder, mail, since=None):
        """
        Fetch unread emails using IMAP UID commands and return message identifiers.

        Returns list of dicts:
          {
            "uid": "<imap_uid>",
            "message_id": "<Message-Id header or empty string>",
            "in_reply_to": "<In-Reply-To header or empty string>",
            "references": "<References header or empty string>",
            "date": "<Date header or empty string>",
            "subject": "<Subject header or empty string>",
            "from": "<From header or empty string>",
          }
        """
        try:
            result = mail.select(f'"{folder}"', readonly=True)
            if result[0] != 'OK':
                print(f"Error selecting folder {folder}: {result}")
                return []
        except imaplib.IMAP4.error as e:
            print(f"Error selecting folder {folder}: {e}")
            return []

        try:
            if since:
                since_date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%d-%b-%Y")
                status, messages = mail.uid('search', None, f'(UNSEEN SINCE {since_date})')
            else:
                status, messages = mail.uid('search', None, '(UNSEEN)')

            if status != 'OK' or not messages:
                print(f"Failed to retrieve unread UIDs from folder {folder}")
                return []

            uid_list = messages[0].split() if messages and messages[0] else []
            result_items = []

            for uid in uid_list:
                uid_text = uid.decode(errors='ignore') if isinstance(uid, (bytes, bytearray)) else str(uid)
                status, msg_data = mail.uid(
                    'fetch',
                    uid,
                    '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES DATE SUBJECT FROM)])'
                )
                if status != 'OK' or not msg_data:
                    result_items.append({
                        "uid": uid_text,
                        "message_id": "",
                        "in_reply_to": "",
                        "references": "",
                        "date": "",
                        "subject": "",
                        "from": "",
                    })
                    continue

                header_bytes = b""
                for part in msg_data:
                    if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray)):
                        header_bytes += bytes(part[1])

                if header_bytes:
                    msg = email.message_from_bytes(header_bytes)
                    result_items.append({
                        "uid": uid_text,
                        "message_id": str(msg.get("Message-Id") or "").strip(),
                        "in_reply_to": str(msg.get("In-Reply-To") or "").strip(),
                        "references": str(msg.get("References") or "").strip(),
                        "date": str(msg.get("Date") or "").strip(),
                        "subject": str(msg.get("Subject") or "").strip(),
                        "from": str(msg.get("From") or "").strip(),
                    })
                else:
                    result_items.append({
                        "uid": uid_text,
                        "message_id": "",
                        "in_reply_to": "",
                        "references": "",
                        "date": "",
                        "subject": "",
                        "from": "",
                    })

            return result_items
        except Exception as e:
            print(f"Error fetching unread message IDs from folder {folder}: {e}")
            return []

    def markEmailsAsRead(self, folder, uids, mail):
        """Marks the specified emails as read (adds the \\Seen flag)."""
        try:
            # Select the specified folder in read-write mode
            result = mail.select(f'"{folder}"', readonly=False)
            if result[0] != 'OK':
                print(f"Error selecting folder {folder} for marking emails as read.")
                return False

            for uid in uids:
                # Mark each email UID as read by setting the \\Seen flag
                status, _ = mail.uid('store', uid, '+FLAGS', '(\Seen)')
                if status == 'OK':
                    print(f"Email UID {uid} in folder {folder} marked as read.")
                    return True
                else:
                    print(f"Failed to mark email UID {uid} as read.")
                time.sleep(0.5)

        except imaplib.IMAP4.error as e:
            print(f"Error marking emails as read: {e}")

        return False

    def markEmailsAsUnread(self, folder, uids, mail):
        """Marks the specified emails as unread (removes the \\Seen flag)."""
        try:
            # Select the specified folder in read-write mode
            result = mail.select(f'"{folder}"', readonly=False)
            if result[0] != 'OK':
                print(f"Error selecting folder {folder} for marking emails as unread.")
                return False

            for uid in uids:
                # Mark each email UID as unread by removing the \\Seen flag
                status, _ = mail.uid('store', uid, '-FLAGS', '(\Seen)')
                if status == 'OK':
                    print(f"Email UID {uid} in folder {folder} marked as unread.")
                    return True
                else:
                    print(f"Failed to mark email UID {uid} as unread.")
                time.sleep(0.5)

        except imaplib.IMAP4.error as e:
            print(f"Error marking emails as unread: {e}")

        return False
    
    def copyFolderContent(self, source_folder, destination_folder, mail):
        uids = self.fetchEmailUids(source_folder, mail)
        for uid in uids:
            email_id = f"{source_folder}:{uid.decode()}"
            self.copyEmail(email_id, destination_folder, mail)
            time.sleep(0.5)

    def moveFolderContent(self, source_folder, destination_folder, mail):
        uids = self.fetchEmailUids(source_folder, mail)
        for uid in uids:
            email_id = f"{source_folder}:{uid.decode()}"
            self.moveEmail(email_id, destination_folder, mail)
            time.sleep(0.5)

    def getSpecialFolder(self, mail, type):
        """Fetches a list of all folders (mailboxes) from the server."""
        status, folders = mail.list()
        if status != 'OK':
            print("Failed to retrieve folders")
            return []

        for folder in folders:
            # Extract folder name by splitting on the folder hierarchy separator and ignoring flags
            parts = folder.decode().split(' ')
            if len(parts) > 2:
                if len(parts) <= 4:
                    folder_name = parts[-1].strip().strip('"')
                else:
                    folder_name = ""
                    i = 3
                    while i < len(parts):
                        if folder_name:
                            folder_name += " "
                        folder_name += parts[i].strip().strip('"')
                        i += 1
                folder_type = parts[1].strip().strip('"').strip(")")
                if folder_type == type:
                    return folder_name
                
        return None

    def getInboxFolder(self, mail):
        inbox = self.getSpecialFolder(mail, "\\Inbox")
        if not inbox:
            folders = self.fetchFolders(mail)
            for folder in folders:
                if folder.lower() == "inbox":
                    return folder
            for folder in folders:
                if "inbox" in folder.lower():
                    return folder
        return inbox
    
    def getSentFolder(self, mail):
        sent = self.getSpecialFolder(mail, "\\Sent")
        if not sent:
            folders = self.fetchFolders(mail)
            for folder in folders:
                if folder.lower() == "sent":
                    return folder
            for folder in folders:
                if "sent" in folder.lower():
                    return folder
        return sent
    
    def getSpamFolder(self, mail):
        spam = self.getSpecialFolder(mail, "\\Junk")
        if not spam:
            folders = self.fetchFolders(mail)
            for folder in folders:
                if folder.lower() == "spam" or folder.lower() == "junk":
                    return folder
            for folder in folders:
                if "spam" in folder.lower() or "junk" in folder.lower():
                    return folder
        return spam
  
    def getTrashFolder(self, mail):
        return self.getSpecialFolder(mail, "\\Trash")
  
    def getDraftsFolder(self, mail):
        return self.getSpecialFolder(mail, "\\Drafts")

    def setFlag(self, folder, uids, flag, add, mail):
        """Generic IMAP flag add/remove for a list of UIDs -- unlike
        markEmailsAsRead/markEmailsAsUnread above (hardcoded to \\Seen, and
        each returns after the FIRST uid due to an early `return True`/
        `return False` inside their loop -- left exactly as-is since other
        callers may already depend on that one-uid-at-a-time behavior), this
        is a new, separate method for any flag and any number of uids, added
        for Camerlengo API v2's email:markFlag command."""
        try:
            result = mail.select(f'"{folder}"', readonly=False)
            if result[0] != 'OK':
                return False
        except imaplib.IMAP4.error:
            return False
        op = '+FLAGS' if add else '-FLAGS'
        ok = True
        for uid in uids:
            status, _ = mail.uid('store', uid, op, f'({flag})')
            if status != 'OK':
                ok = False
        return ok

    def searchUIDs(self, folder, mail, unseen_only = False, query = None, since_date = None, before_date = None):
        """Generic IMAP SEARCH with an optional free-text SUBJECT/FROM/BODY
        substring query, layered on top of fetchNonDeletedUIDs/
        fetchUnreadUIDs above (neither supports a query) -- added for
        Camerlengo API v2's email:listMessages command.

        since_date (2026-10-04, added for email:listMessages's sinceDate
        param, for Caroline's batch mailbox-check tools): a datetime.date --
        IMAP's SINCE criterion only has day granularity, never finer.

        before_date (2026-10-12, added for email:listMessages's untilDate
        param, for an algorithmic whole-folder/date-range read -- see that
        command's own doc comment): same shape and same day-granularity
        caveat as since_date, IMAP's BEFORE criterion. Together they give a
        real [since, until) interval instead of only an open-ended lower
        bound."""
        try:
            result = mail.select(f'"{folder}"', readonly=True)
            if result[0] != 'OK':
                return []
        except imaplib.IMAP4.error:
            return []
        parts = []
        if unseen_only:
            parts.append("UNSEEN")
        if since_date:
            parts.append(f'SINCE {since_date.strftime("%d-%b-%Y")}')
        if before_date:
            parts.append(f'BEFORE {before_date.strftime("%d-%b-%Y")}')
        if query:
            q = query.replace('\\', '\\\\').replace('"', '\\"')
            parts.append(f'OR (SUBJECT "{q}") (OR (FROM "{q}") (BODY "{q}"))')
        criteria = " ".join(parts) if parts else "UNDELETED"
        status, messages = mail.uid('search', criteria)
        if status != 'OK':
            return []
        return messages[0].split() if messages and messages[0] else []

    def fetchHeaders(self, folder, uids, mail):
        """Lightweight header-only fetch (From/Subject/Date/flags) for a
        GIVEN list of UIDs -- much cheaper than downloadEmail's full RFC822
        fetch when only listing-level metadata is needed. Mirrors the
        header-fetch half of fetchUnreadMsgIds above, generalized to an
        arbitrary uid list instead of always searching UNSEEN. Added for
        Camerlengo API v2's email:listMessages command."""
        try:
            result = mail.select(f'"{folder}"', readonly=True)
            if result[0] != 'OK':
                return []
        except imaplib.IMAP4.error:
            return []
        out = []
        for uid in uids:
            uid_text = uid.decode(errors='ignore') if isinstance(uid, (bytes, bytearray)) else str(uid)
            try:
                status, msg_data = mail.uid('fetch', uid, '(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
            except imaplib.IMAP4.error:
                continue
            if status != 'OK' or not msg_data:
                continue
            header_bytes = b""
            flags = []
            for part in msg_data:
                if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray)):
                    header_bytes += bytes(part[1])
                elif isinstance(part, (bytes, bytearray)):
                    m = re.search(rb'FLAGS \(([^)]*)\)', part)
                    if m:
                        flags = m.group(1).decode(errors='ignore').split()
            msg = email.message_from_bytes(header_bytes) if header_bytes else None
            # Pass msg.get(...) STRAIGHT into _decode_header_text -- never
            # str() it first. For a non-ASCII header, msg.get() returns an
            # email.header.Header object, and Header.__str__() is itself
            # the lossy path this whole fix exists to avoid (see
            # _decode_header_text's and _decodeAddress's docstrings).
            out.append({
                "uid": uid_text,
                "from": _decode_header_text(msg.get("From")) if msg and msg.get("From") else None,
                "subject": _decode_header_text(msg.get("Subject")) if msg and msg.get("Subject") else None,
                "date": str(msg.get("Date")) if msg and msg.get("Date") else None,
                "flags": flags,
            })
        return out

    def isSpecialFolder(self, folder, mail = None):
        if not mail:
            mail = self._createNewConnection()
        if not self.special_folders:
            inbox = self.getInboxFolder(mail)
            if inbox:
                self.special_folders.append(inbox.lower())
            sent = self.getSentFolder(mail)
            if sent:
                self.special_folders.append(sent.lower())
            trash = self.getTrashFolder(mail)
            if trash:
                self.special_folders.append(trash.lower())
            drafts = self.getDraftsFolder(mail)
            if drafts:
                self.special_folders.append(drafts.lower())
            junk = self.getSpamFolder(mail)
            if junk:
                self.special_folders.append(junk.lower())
        if folder.lower() in self.special_folders:
            return True
        else:
            return False
