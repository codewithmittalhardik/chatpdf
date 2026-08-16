import os
import urllib3
import certifi
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pymongo import MongoClient
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
import dns.resolver

from langchain_huggingface import HuggingFaceEmbeddings
from pinecone import Pinecone, ServerlessSpec

# Configure dnspython fallback to public DNS (8.8.8.8 / 1.1.1.1) to fix local ISP SRV lookup failures
try:
    custom_resolver = dns.resolver.Resolver()
    custom_resolver.nameservers = ['8.8.8.8', '1.1.1.1', '8.8.4.4']
    dns.resolver.default_resolver = custom_resolver
except Exception:
    pass

# Disable SSL verification warning (for development only)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import warnings
warnings.filterwarnings("ignore", message=".*sending unauthenticated requests to the HF Hub.*")

# Disable MPS on macOS to prevent crashes
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# --- DATABASE: Auto-select Local MongoDB or Atlas ---
def _try_mongo(uri, label, tls=False):
    """Attempt a quick ping to verify the MongoDB URI is reachable."""
    try:
        kwargs = {"serverSelectionTimeoutMS": 3000, "connectTimeoutMS": 3000}
        if tls:
            kwargs["tlsCAFile"] = certifi.where()
        client = MongoClient(uri, **kwargs)
        client.admin.command("ping")   # will raise if unreachable
        client.close()
        return True
    except Exception as e:
        print(f"   [{label}] not reachable: {e}")
        return False

_local_uri   = os.getenv("LOCAL_MONGO_URI", "mongodb://localhost:27017/chatpdf_db")
_atlas_uri   = os.getenv("MONGO_URI", "")
_atlas_uri_t = (_atlas_uri + ("&" if "?" in _atlas_uri else "?") +
                "serverSelectionTimeoutMS=5000&connectTimeoutMS=5000")

print("🔍 Detecting MongoDB...")
if _try_mongo(_local_uri, "Local MongoDB"):
    _mongo_uri = _local_uri
    _use_tls = False
    print("✅ Using LOCAL MongoDB (mongodb://localhost:27017)")
else:
    _mongo_uri = _atlas_uri_t
    _use_tls = True
    print("☁️  Using MongoDB ATLAS (cloud)")

try:
    if _use_tls:
        mongo_client = MongoClient(_mongo_uri, tlsCAFile=certifi.where())
    else:
        mongo_client = MongoClient(_mongo_uri)
    db = mongo_client['chatpdf_db']
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"⚠️  MongoDB connection failed: {e}")
    print("   App will start but DB operations will fail.")
    mongo_client = None
    db = None

# --- AI SETUP ---
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"), ssl_verify=False)
INDEX_NAME = "pdf-chat"

try:
    existing_indexes = [index.name for index in pc.list_indexes()]
    if INDEX_NAME not in existing_indexes:
        print(f"Index '{INDEX_NAME}' not found. Creating it now...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=384,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
        print("Index created!")
except Exception as e:
    print(f"Pinecone setup notice: {e}")

# Force CPU to prevent macOS Metal/Gunicorn crashes
embeddings = HuggingFaceEmbeddings(
    model_name="all-MiniLM-L6-v2",
    model_kwargs={'device': 'cpu'}
)

# --- EMAIL & TOKEN HELPERS FOR PASSWORD RESET ---
SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-change-me')

def generate_reset_token(email):
    serializer = URLSafeTimedSerializer(SECRET_KEY)
    return serializer.dumps(email, salt='password-reset-salt')

def verify_reset_token(token, expiration=900):  # 15 minutes validity
    serializer = URLSafeTimedSerializer(SECRET_KEY)
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=expiration)
        return email
    except (SignatureExpired, BadTimeSignature):
        return None

def send_reset_email(to_email, reset_url):
    subject = "ChatPDF - Password Reset Link"
    body = f"""Hello,

You requested to reset your password for your ChatPDF account.
Click the link below to reset your password:

{reset_url}

This link will expire in 15 minutes. If you did not request this, please ignore this email.

Best regards,
The ChatPDF Team
"""

    # --- 1. Resend API ---
    resend_api_key = os.getenv('RESEND_API_KEY')
    resend_sender  = os.getenv('MAIL_DEFAULT_SENDER', 'ChatPDF <onboarding@resend.dev>')
    if resend_api_key:
        try:
            res = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={"from": resend_sender, "to": [to_email], "subject": subject, "text": body},
                timeout=10
            )
            if res.status_code in [200, 201, 202]:
                print(f"✅ Reset email sent via Resend to {to_email}")
                return True
            else:
                print(f"⚠️ Resend status {res.status_code} for {to_email}: {res.text}")
        except Exception as e:
            print(f"⚠️ Resend exception: {e}")

    # --- 2. Brevo API (Free 300 emails/day to ANY recipient address) ---
    brevo_api_key = os.getenv('BREVO_API_KEY')
    if brevo_api_key:
        try:
            res = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": brevo_api_key, "Content-Type": "application/json"},
                json={
                    "sender": {"name": "ChatPDF", "email": os.getenv('MAIL_USERNAME', 'support@chatpdf.com')},
                    "to": [{"email": to_email}],
                    "subject": subject,
                    "htmlContent": f"<p>{body.replace(chr(10), '<br>')}</p>"
                },
                timeout=10
            )
            if res.status_code in [200, 201, 202]:
                print(f"✅ Reset email sent via Brevo to {to_email}")
                return True
            else:
                print(f"⚠️ Brevo status {res.status_code}: {res.text}")
        except Exception as e:
            print(f"⚠️ Brevo exception: {e}")

    # --- 3. Gmail SMTP fallback ---
    mail_user = os.getenv('MAIL_USERNAME')
    mail_pass = os.getenv('MAIL_PASSWORD')
    mail_server = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
    mail_port   = int(os.getenv('MAIL_PORT', 587))
    smtp_sender = f"ChatPDF <{mail_user}>" if mail_user else None

    if mail_user and mail_pass:
        try:
            msg = MIMEMultipart()
            msg['From']    = smtp_sender
            msg['To']      = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            server = smtplib.SMTP(mail_server, mail_port)
            server.starttls()
            server.login(mail_user, mail_pass)
            server.send_message(msg)
            server.quit()
            print(f"✅ Reset email sent via Gmail SMTP to {to_email}")
            return True
        except Exception as e:
            print(f"❌ Gmail SMTP Error: {e}")

    # --- 4. Dev console fallback ---
    print(f"\n{'='*55}")
    print(f"[DEV] Reset link for {to_email}:")
    print(f"  {reset_url}")
    print(f"{'='*55}\n")
    return True
