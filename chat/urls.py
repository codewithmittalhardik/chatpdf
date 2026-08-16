from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('login', views.login_view, name='login'),
    path('register', views.register_view, name='register'),
    path('logout', views.logout_view, name='logout'),
    path('forgot_password', views.forgot_password_view, name='forgot_password'),
    path('reset_password/<str:token>', views.reset_password_token_view, name='reset_password_token'),
    path('delete-account', views.delete_account_view, name='delete_account'),
    path('chat', views.dashboard, name='dashboard'),
    path('health', views.health, name='health'),
    path('upload', views.upload_file, name='upload'),
    path('get_history/<str:session_id>', views.get_history, name='get_history'),
    path('ask', views.ask_question, name='ask'),
    path('delete/<str:session_id>', views.delete_chat, name='delete_chat'),
]
