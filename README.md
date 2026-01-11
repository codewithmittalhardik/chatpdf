# 📄 DocuGPT - Chat with your PDFs

DocuGPT is a smart web application that allows users to upload PDF documents and have natural, context-aware conversations with them. It uses **Llama 3 (via Groq)** for high-speed AI responses and **Pinecone** for vector storage, making it capable of understanding complex documents instantly.

## 🚀 Features

* **PDF Analysis**: robust text extraction using `pdfplumber` (supports complex layouts).
* **AI-Powered Chat**: Uses **Llama 3-70b** via Groq for instant, accurate answers.
* **Vector Search**: Uses **Pinecone** to store and retrieve document context efficiently.
* **User System**: Secure Registration & Login using **MongoDB Atlas**.
* **Chat History**: Saves all your conversations and uploads automatically.
* **Responsive UI**: Modern, mobile-friendly interface built with **Tailwind CSS**.
* **Secure**: Password hashing with Bcrypt.

## 🛠️ Tech Stack

* **Backend:** Python, Flask, Gunicorn
* **Database:** MongoDB Atlas (NoSQL)
* **Vector DB:** Pinecone (Serverless)
* **AI Model:** Llama 3 (via Groq API)
* **Embeddings:** HuggingFace (`all-MiniLM-L6-v2`)
* **Frontend:** HTML5, JavaScript, Tailwind CSS

## ⚙️ Local Installation

Follow these steps to run the project on your machine.

### 1. Clone the Repository
```bash
git clone [https://github.com/yourusername/docugpt.git](https://github.com/yourusername/docugpt.git)
cd docugpt
```
**Create a Virtual Environment**
```bash
python3 -m venv .venv

on macos :
source .venv/bin/activate

On Windows:
.venv\Scripts\activate
```
**Install Dependencies**
```bash
pip install -r requirements.txt
```
**Set Up Environment Variables**
```bash
SECRET_KEY=your_secret_key
MONGO_URI=mongodb+srv://<user>:<password>@cluster0.mongodb.net/chatpdf_db?retryWrites=true&w=majority
PINECONE_API_KEY=your_pinecone_api_key
GROQ_API_KEY=your_groq_api_key
```
**Run the Application**
```bash
# For Development
python3 app.py

# For Production/Testing
gunicorn app:app
```
**Project Structure**
```text
docugpt/
├── app.py              # Main Flask Application
├── requirements.txt    # Python Dependencies
├── templates/
│   ├── index.html      # Dashboard & Chat UI
│   ├── login.html      # Login Page
│   └── register.html   # Registration Page
├── .env                # API Keys (Not pushed to GitHub)
└── README.md           # Documentation
```
