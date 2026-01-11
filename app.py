import os
import uuid
import datetime
import urllib3
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

# Disable SSL verification warning (for development only)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Disable MPS on macOS to prevent crashes
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

load_dotenv()

app = Flask(__name__)

# --- CONFIGURATION ---
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-change-me')
app.config["MONGO_URI"] = os.getenv("MONGO_URI")
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

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
mongo = PyMongo(app)
db = mongo.cx['chatpdf_db']
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
        self.password = user_dict['password']

@login_manager.user_loader
def load_user(user_id):
    user_data = db.users.find_one({"_id": ObjectId(user_id)})
    if user_data:
        return User(user_data)
    return None

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
        
        user_data = db.users.find_one({"username": username})
        
        if user_data and bcrypt.check_password_hash(user_data['password'], password):
            user_obj = User(user_data)
            login_user(user_obj)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if db.users.find_one({"username": username}):
            flash('Username already exists. Please login.', 'error')
            return redirect(url_for('register'))
        
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        
        db.users.insert_one({
            "username": username,
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
    print(f"Access at: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=True)