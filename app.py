import os
import uuid
import datetime
import urllib3
import certifi
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
import pdfplumber  # <--- NEW LIBRARY (Replaces pypdf)

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_pymongo import PyMongo
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_bcrypt import Bcrypt
from flask_cors import CORS
from dotenv import load_dotenv
from bson.objectid import ObjectId

# AI Imports
from langchain_text_splitters import CharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_groq import ChatGroq
from langchain_pinecone import PineconeVectorStore
from pinecone import Pinecone, ServerlessSpec
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

import dns.resolver

# Configure dnspython fallback to public DNS (8.8.8.8 / 1.1.1.1) to fix local ISP SRV lookup failures
try:
    custom_resolver = dns.resolver.Resolver()
    custom_resolver.nameservers = ['8.8.8.8', '1.1.1.1', '8.8.4.4']
    dns.resolver.default_resolver = custom_resolver
except Exception:
    pass

# Disable SSL verification warning (for development only)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Disable MPS on macOS to prevent crashes
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

load_dotenv()

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# --- CONFIGURATION ---
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['PREFERRED_URL_SCHEME'] = 'https'

# --- DATABASE: Auto-select Local MongoDB or Atlas ---
# Tries local MongoDB first (for college networks / offline dev),
# falls back to Atlas (for production / when local is not running)
def _try_mongo(uri, label, tls=False):
    """Attempt a quick ping to verify the MongoDB URI is reachable."""
    from pymongo import MongoClient
    try:
        kwargs = {"serverSelectionTimeoutMS": 3000, "connectTimeoutMS": 3000}
        if tls:
            import certifi as _certifi
            kwargs["tlsCAFile"] = _certifi.where()
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
    app.config["MONGO_URI"] = _local_uri
    _use_tls = False
    print("✅ Using LOCAL MongoDB (mongodb://localhost:27017)")
else:
    app.config["MONGO_URI"] = _atlas_uri_t
    _use_tls = True
    print("☁️  Using MongoDB ATLAS (cloud)")


# Enable CORS for mobile access
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type"],
        "expose_headers": ["Content-Type"],
        "supports_credentials": False
    }
})

# --- DATABASE & AUTH SETUP ---
try:
    if _use_tls:
        mongo = PyMongo(app, tlsCAFile=certifi.where())
    else:
        mongo = PyMongo(app)   # Local MongoDB — no TLS needed
    db = mongo.cx['chatpdf_db']
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"⚠️  MongoDB connection failed: {e}")
    print("   App will start but DB operations will fail.")
    mongo = None
    db = None
bcrypt = Bcrypt(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# --- AI SETUP ---
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"), ssl_verify=False)
INDEX_NAME = "pdf-chat"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# AUTO-CREATE INDEX CHECK
existing_indexes = [index.name for index in pc.list_indexes()]
if INDEX_NAME not in existing_indexes:
    print(f"Index '{INDEX_NAME}' not found. Creating it now...")
    try:
        pc.create_index(
            name=INDEX_NAME,
            dimension=384,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
        print("Index created!")
    except Exception as e:
        print(f"Error creating index: {e}")

# Force CPU to prevent macOS Metal/Gunicorn crashes
embeddings = HuggingFaceEmbeddings(
    model_name="all-MiniLM-L6-v2",
    model_kwargs={'device': 'cpu'}
)

# --- USER MODEL ---
class User(UserMixin):
    def __init__(self, user_dict):
        self.id = str(user_dict['_id'])
        self.username = user_dict['username']
        self.email = user_dict.get('email', '')
        self.password = user_dict['password']

@login_manager.user_loader
def load_user(user_id):
    user_data = db.users.find_one({"_id": ObjectId(user_id)})
    if user_data:
        return User(user_data)
    return None

# --- EMAIL & TOKEN HELPERS FOR PASSWORD RESET ---
def generate_reset_token(email):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(email, salt='password-reset-salt')

def verify_reset_token(token, expiration=900):  # 15 minutes validity
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=expiration)
        return email
    except (SignatureExpired, BadTimeSignature):
        return None

import requests, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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

    # --- 1. Try Resend (works for account owner email only on free tier) ---
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
            elif res.status_code != 403:
                print(f"❌ Resend Error ({res.status_code}): {res.text}")
                # Fall through to Gmail SMTP
        except Exception as e:
            print(f"⚠️ Resend failed: {e}, trying Gmail SMTP...")

    # --- 2. Gmail SMTP fallback (works for ALL recipients in production) ---
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

    # --- 3. Dev console fallback (no email configured) ---
    print(f"\n{'='*55}")
    print(f"[DEV] No email provider reached. Reset link:")
    print(f"  {reset_url}")
    print(f"{'='*55}\n")
    return True

# --- AUTH ROUTES ---

@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user_data = db.users.find_one({
            "$or": [
                {"username": username},
                {"email": username.lower()}
            ]
        })
        
        if user_data and bcrypt.check_password_hash(user_data['password'], password):
            user_obj = User(user_data)
            login_user(user_obj)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')
            
    return render_template('login.html')

@app.route('/forgot_password', methods=['POST'])
def forgot_password():
    identity = request.form.get('identity', '').strip()
    if not identity:
        flash('Please enter your registered email or username.', 'error')
        return redirect(url_for('login'))

    user = db.users.find_one({
        "$or": [
            {"email": identity.lower()},
            {"username": identity}
        ]
    })

    if user and user.get('email'):
        token = generate_reset_token(user['email'])
        site_url = os.getenv('APP_URL', '').rstrip('/')
        if site_url:
            reset_url = f"{site_url}/reset_password/{token}"
        else:
            reset_url = url_for('reset_password_token', token=token, _external=True)
        send_reset_email(user['email'], reset_url)
        flash(f'Verification email sent to {user["email"]}! Check your inbox for the reset link.', 'success')
    elif user and not user.get('email'):
        flash('This account does not have an email registered. Please contact support.', 'error')
    else:
        # Standard response for security
        flash('If an account exists with that email/username, a reset link has been sent.', 'success')

    return redirect(url_for('login'))

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password_token(token):
    email = verify_reset_token(token)
    if not email:
        flash('The password reset link is invalid or has expired.', 'error')
        return redirect(url_for('login'))

    if request.method == 'POST':
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not new_password or not confirm_password:
            flash('Please fill in both password fields.', 'error')
            return render_template('reset_password.html', token=token, email=email)

        if new_password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('reset_password.html', token=token, email=email)

        hashed_pw = bcrypt.generate_password_hash(new_password).decode('utf-8')
        db.users.update_one(
            {"email": email},
            {"$set": {"password": hashed_pw}}
        )

        flash('Your password has been updated successfully! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token, email=email)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        
        if not username or not email or not password:
            flash('Please fill in all fields.', 'error')
            return redirect(url_for('register'))

        if db.users.find_one({"username": username}):
            flash('Username already exists. Please choose another.', 'error')
            return redirect(url_for('register'))

        if db.users.find_one({"email": email}):
            flash('An account with this email already exists. Please login.', 'error')
            return redirect(url_for('register'))
        
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        
        db.users.insert_one({
            "username": username,
            "email": email,
            "password": hashed_pw,
            "created_at": datetime.datetime.utcnow()
        })
        
        flash('Account created successfully! Please login.', 'success')
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    try:
        user_id = current_user.id

        # 1. Delete all Pinecone vectors for each chat
        user_chats = list(db.chats.find({"user_id": user_id}))
        for chat in user_chats:
            try:
                pc.Index(INDEX_NAME).delete(delete_all=True, namespace=chat['namespace_id'])
            except Exception as pe:
                print(f"Pinecone cleanup error (continuing): {pe}")

        # 2. Delete all chat documents from MongoDB
        db.chats.delete_many({"user_id": user_id})

        # 3. Delete the user document
        db.users.delete_one({"_id": ObjectId(user_id)})

        # 4. Log out and redirect
        logout_user()
        flash('Your account has been permanently deleted.', 'info')
        return jsonify({"success": True}), 200

    except Exception as e:
        print(f"Delete Account Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# --- APP ROUTES ---

@app.route('/chat')
@login_required
def dashboard():
    user_chats = list(db.chats.find({"user_id": current_user.id}).sort("created_at", -1))
    for chat in user_chats:
        chat['id'] = str(chat['_id'])
    return render_template('index.html', user=current_user, chats=user_chats)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "Server is running"}), 200

@app.route('/upload', methods=['POST', 'OPTIONS'])
@login_required
def upload_file():
    if request.method == 'OPTIONS':
        return '', 204
        
    try:
        if 'pdf_file' not in request.files:
            return jsonify({"error": "No file"}), 400
        file = request.files['pdf_file']
        
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        if not file.filename.lower().endswith('.pdf'):
            return jsonify({"error": "Only PDF files are allowed"}), 400

        if file:
            # 1. Create unique Namespace
            namespace_id = f"user_{current_user.id}_{str(uuid.uuid4())[:8]}"

            # 2. Read PDF using pdfplumber (MORE ROBUST)
            text = ""
            try:
                with pdfplumber.open(file) as pdf:
                    if len(pdf.pages) == 0:
                         return jsonify({"error": "PDF has no readable pages"}), 400

                    for page in pdf.pages:
                        extracted = page.extract_text()
                        if extracted:
                            text += extracted + "\n"
            except Exception as e:
                print(f"PDF Reading Error: {e}")
                return jsonify({"error": "Failed to read PDF file. It may be corrupted or encrypted."}), 400
            
            if not text.strip():
                return jsonify({"error": "No text found in PDF (it might be an image/scanned PDF)."}), 400
            
            chunks = CharacterTextSplitter(separator="\n", chunk_size=1000, chunk_overlap=200).split_text(text)

            # 3. Save Vectors to Pinecone
            PineconeVectorStore.from_texts(
                texts=chunks, 
                embedding=embeddings, 
                index_name=INDEX_NAME,
                namespace=namespace_id
            )

            # 4. Save Chat Metadata to MongoDB
            new_chat = {
                "user_id": current_user.id,
                "pdf_name": file.filename,
                "namespace_id": namespace_id,
                "created_at": datetime.datetime.utcnow(),
                "messages": []
            }
            result = db.chats.insert_one(new_chat)
            
            return jsonify({
                "session_id": str(result.inserted_id), 
                "filename": file.filename
            }), 200
            
    except Exception as e:
        print(f"Upload Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/get_history/<session_id>')
@login_required
def get_history(session_id):
    try:
        chat = db.chats.find_one({"_id": ObjectId(session_id)})
        
        if not chat or chat['user_id'] != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403
            
        return jsonify({
            "messages": chat.get('messages', []),
            "pdf_name": chat['pdf_name'],
            "session_id": str(chat['_id'])
        })
    except:
        return jsonify({"error": "Invalid Session ID"}), 400

@app.route('/ask', methods=['POST'])
@login_required
def ask_question():
    data = request.get_json()
    user_question = data.get('question')
    session_id = data.get('session_id')
    
    if not user_question:
        return jsonify({"error": "No question provided"}), 400
    
    if not session_id:
        return jsonify({"error": "Session ID missing. Please upload a PDF first."}), 400

    try:
        chat = db.chats.find_one({"_id": ObjectId(session_id)})
        if not chat or chat['user_id'] != current_user.id:
            return jsonify({"error": "Unauthorized access to this PDF"}), 403

        # 1. Search Pinecone
        vectorstore = PineconeVectorStore(index_name=INDEX_NAME, embedding=embeddings, namespace=chat['namespace_id'])
        docs = vectorstore.similarity_search(user_question)
        context_text = "\n\n".join(doc.page_content for doc in docs)

        # 2. AI Answer
        llm = ChatGroq(groq_api_key=os.getenv("GROQ_API_KEY"), model_name="llama-3.3-70b-versatile")
        template = "Answer based on context:\n{context}\nQuestion: {question}"
        prompt = ChatPromptTemplate.from_template(template)
        chain = prompt | llm | StrOutputParser()
        answer = chain.invoke({"context": context_text, "question": user_question})

        # 3. Update MongoDB
        new_messages = [
            {"sender": "user", "text": user_question, "timestamp": datetime.datetime.utcnow()},
            {"sender": "ai", "text": answer, "timestamp": datetime.datetime.utcnow()}
        ]
        
        db.chats.update_one(
            {"_id": ObjectId(session_id)},
            {"$push": {"messages": {"$each": new_messages}}}
        )

        return jsonify({"answer": answer})
        
    except Exception as e:
        print(f"Chat Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/delete/<session_id>', methods=['DELETE'])
@login_required
def delete_chat(session_id):
    try:
        chat = db.chats.find_one({"_id": ObjectId(session_id)})
        
        if not chat:
            return jsonify({"error": "Chat not found"}), 404
        
        if chat['user_id'] != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403
        
        try:
            pc.Index(INDEX_NAME).delete(delete_all=True, namespace=chat['namespace_id'])
        except Exception as pe:
            print(f"Pinecone delete error (continuing): {pe}")
        
        db.chats.delete_one({"_id": ObjectId(session_id)})
        
        return jsonify({"success": True, "message": "Chat deleted"}), 200
        
    except Exception as e:
        print(f"Delete Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=True)