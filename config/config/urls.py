"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconfInsufficient main stock. Available: 49.958. Add received stock in Update main stock first
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import logging

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from loraApi.views import cancellation_history, favicon, index, main_stock, product_movement_history, user_management

logger = logging.getLogger(__name__)


class AuditLoginView(auth_views.LoginView):
    def form_invalid(self, form):
        username = self.request.POST.get('username', '').strip()
        logger.warning(
            'Login rejected: username=%s errors=%s',
            username,
            form.errors.as_json(),
        )
        return super().form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        logger.info(
            'Login accepted: username=%s is_active=%s is_staff=%s is_superuser=%s',
            user.get_username(),
            user.is_active,
            user.is_staff,
            user.is_superuser,
        )
        return super().form_valid(form)


urlpatterns = [
    path('', index, name='dashboard'),
    path('stock/', main_stock, name='main_stock_page'),
    path('stock/movements/history/', product_movement_history, name='product_movement_history_page'),
    path('history/', cancellation_history, name='cancellation_history_page'),
    path('login/', AuditLoginView.as_view(template_name='loraApi/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('users/', user_management, name='user_management'),
    path('favicon.ico', favicon, name='favicon'),
    path('admin/', admin.site.urls),
    path('api/', include('loraApi.urls')),
]
