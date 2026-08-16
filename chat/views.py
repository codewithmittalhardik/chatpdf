import os
import uuid
import json
import datetime
import pdfplumber
import bcrypt
from bson.objectid import ObjectId

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.views.decorators.csrf import csrf_exempt

from langchain_text_splitters import CharacterTextSplitter
from langchain_groq import ChatGroq
from langchain_pinecone import PineconeVectorStore
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from chat.db import (
    db, pc, INDEX_NAME, embeddings,
    generate_reset_token, verify_reset_token, send_reset_email
)

class UserWrapper:
    def __init__(self, user_dict):
        self.id = str(user_dict['_id'])
        self.username = user_dict['username']
        self.email = user_dict.get('email', '')
        self.is_authenticated = True

def get_current_user(request):
    user_id = request.session.get('user_id')
    if not user_id or db is None:
        return None
    try:
        user_data = db.users.find_one({"_id": ObjectId(user_id)})
        if user_data:
            return UserWrapper(user_data)
    except Exception:
        pass
    return None

def login_required_view(view_func):
    def wrapper(request, *args, **kwargs):
        current_user = get_current_user(request)
        if not current_user:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json' or request.path in ['/upload', '/ask', '/delete-account']:
                return JsonResponse({"error": "Unauthorized"}, status=401)
            return redirect('login')
        request.current_user = current_user
        return view_func(request, *args, **kwargs)
    return wrapper


# --- AUTH VIEWS ---

def home(request):
    current_user = get_current_user(request)
    if current_user:
        return redirect('dashboard')
    return redirect('login')

def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        if db is not None:
            user_data = db.users.find_one({
                "$or": [
                    {"username": username},
                    {"email": username.lower()}
                ]
            })

            if user_data and bcrypt.checkpw(password.encode('utf-8'), user_data['password'].encode('utf-8')):
                request.session['user_id'] = str(user_data['_id'])
                request.session['username'] = user_data['username']
                return redirect('dashboard')

        messages.error(request, 'Invalid username or password')

    return render(request, 'login.html')

def forgot_password_view(request):
    if request.method == 'POST':
        identity = request.POST.get('identity', '').strip()
        if not identity:
            messages.error(request, 'Please enter your registered email or username.')
            return redirect('login')

        if db is not None:
            user = db.users.find_one({
                "$or": [
                    {"email": identity.lower()},
                    {"username": identity}
                ]
            })

            if user and user.get('email'):
                token = generate_reset_token(user['email'])
                host = request.get_host()
                if "127.0.0.1" in host or "localhost" in host:
                    reset_url = request.build_absolute_uri(f'/reset_password/{token}')
                else:
                    site_url = os.getenv('APP_URL', '').rstrip('/')
                    if site_url:
                        reset_url = f"{site_url}/reset_password/{token}"
                    else:
                        reset_url = request.build_absolute_uri(f'/reset_password/{token}')

                send_reset_email(user['email'], reset_url)
                messages.success(request, f'Reset link sent for {user["email"]}! Check your inbox.')
            elif user and not user.get('email'):
                messages.error(request, 'This account does not have an email registered. Please contact support.')
            else:
                messages.error(request, 'No registered account found with that email or username. Please register first.')

    return redirect('login')

def reset_password_token_view(request, token):
    email = verify_reset_token(token)
    if not email:
        messages.error(request, 'The password reset link is invalid or has expired.')
        return redirect('login')

    if request.method == 'POST':
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')

        if not new_password or not confirm_password:
            messages.error(request, 'Please fill in both password fields.')
            return render(request, 'reset_password.html', {'token': token, 'email': email})

        if new_password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'reset_password.html', {'token': token, 'email': email})

        if db is not None:
            hashed_pw = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            db.users.update_one(
                {"email": email},
                {"$set": {"password": hashed_pw}}
            )

        messages.success(request, 'Your password has been updated successfully! Please log in.')
        return redirect('login')

    return render(request, 'reset_password.html', {'token': token, 'email': email})

def register_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')

        if not username or not email or not password:
            messages.error(request, 'Please fill in all fields.')
            return redirect('register')

        if db is not None:
            if db.users.find_one({"username": username}):
                messages.error(request, 'Username already exists. Please choose another.')
                return redirect('register')

            if db.users.find_one({"email": email}):
                messages.error(request, 'An account with this email already exists. Please login.')
                return redirect('register')

            hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

            db.users.insert_one({
                "username": username,
                "email": email,
                "password": hashed_pw,
                "created_at": datetime.datetime.utcnow()
            })

            messages.success(request, 'Account created successfully! Please login.')
            return redirect('login')

    return render(request, 'register.html')

def logout_view(request):
    request.session.flush()
    return redirect('login')

@csrf_exempt
@login_required_view
def delete_account_view(request):
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)
    try:
        user_id = request.current_user.id

        # 1. Delete all Pinecone vectors for each chat
        if db is not None:
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

        # 4. Log out
        request.session.flush()
        return JsonResponse({"success": True}, status=200)

    except Exception as e:
        print(f"Delete Account Error: {e}")
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(e)}, status=500)


# --- APP VIEWS ---

@login_required_view
def dashboard(request):
    user_chats = []
    if db is not None:
        user_chats = list(db.chats.find({"user_id": request.current_user.id}).sort("created_at", -1))
        for chat in user_chats:
            chat['id'] = str(chat['_id'])
    return render(request, 'index.html', {'user': request.current_user, 'chats': user_chats})

def health(request):
    return JsonResponse({"status": "ok", "message": "Server is running"}, status=200)

@csrf_exempt
@login_required_view
def upload_file(request):
    if request.method == 'OPTIONS':
        response = HttpResponse('', status=204)
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Headers'] = 'Content-Type'
        response['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        return response

    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        if 'pdf_file' not in request.FILES:
            return JsonResponse({"error": "No file"}, status=400)
        file = request.FILES['pdf_file']

        if file.name == '':
            return JsonResponse({"error": "No selected file"}, status=400)

        if not file.name.lower().endswith('.pdf'):
            return JsonResponse({"error": "Only PDF files are allowed"}, status=400)

        # 1. Create unique Namespace
        namespace_id = f"user_{request.current_user.id}_{str(uuid.uuid4())[:8]}"

        # 2. Read PDF using pdfplumber
        text = ""
        try:
            with pdfplumber.open(file) as pdf:
                if len(pdf.pages) == 0:
                    return JsonResponse({"error": "PDF has no readable pages"}, status=400)

                for page in pdf.pages:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted + "\n"
        except Exception as e:
            print(f"PDF Reading Error: {e}")
            return JsonResponse({"error": "Failed to read PDF file. It may be corrupted or encrypted."}, status=400)

        if not text.strip():
            return JsonResponse({"error": "No text found in PDF (it might be an image/scanned PDF)."}, status=400)

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
            "user_id": request.current_user.id,
            "pdf_name": file.name,
            "namespace_id": namespace_id,
            "created_at": datetime.datetime.utcnow(),
            "messages": []
        }
        result = db.chats.insert_one(new_chat)

        return JsonResponse({
            "session_id": str(result.inserted_id),
            "filename": file.name
        }, status=200)

    except Exception as e:
        print(f"Upload Error: {e}")
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(e)}, status=500)

@login_required_view
def get_history(request, session_id):
    try:
        chat = db.chats.find_one({"_id": ObjectId(session_id)})

        if not chat or chat['user_id'] != request.current_user.id:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        return JsonResponse({
            "messages": chat.get('messages', []),
            "pdf_name": chat['pdf_name'],
            "session_id": str(chat['_id'])
        })
    except Exception:
        return JsonResponse({"error": "Invalid Session ID"}, status=400)

@csrf_exempt
@login_required_view
def ask_question(request):
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    user_question = data.get('question')
    session_id = data.get('session_id')

    if not user_question:
        return JsonResponse({"error": "No question provided"}, status=400)

    if not session_id:
        return JsonResponse({"error": "Session ID missing. Please upload a PDF first."}, status=400)

    try:
        chat = db.chats.find_one({"_id": ObjectId(session_id)})
        if not chat or chat['user_id'] != request.current_user.id:
            return JsonResponse({"error": "Unauthorized access to this PDF"}, status=403)

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

        return JsonResponse({"answer": answer})

    except Exception as e:
        print(f"Chat Error: {e}")
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
@login_required_view
def delete_chat(request, session_id):
    if request.method != 'DELETE':
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        chat = db.chats.find_one({"_id": ObjectId(session_id)})

        if not chat:
            return JsonResponse({"error": "Chat not found"}, status=404)

        if chat['user_id'] != request.current_user.id:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        try:
            pc.Index(INDEX_NAME).delete(delete_all=True, namespace=chat['namespace_id'])
        except Exception as pe:
            print(f"Pinecone delete error (continuing): {pe}")

        db.chats.delete_one({"_id": ObjectId(session_id)})

        return JsonResponse({"success": True, "message": "Chat deleted"}, status=200)

    except Exception as e:
        print(f"Delete Error: {e}")
        return JsonResponse({"error": str(e)}, status=500)
