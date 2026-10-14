from django.urls import path

from apps.panel import views

app_name = "panel"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("", views.home, name="home"),
    path("pause/", views.set_pause, name="set_pause"),
    path("rooms/<int:room_id>/toggle/", views.room_toggle, name="room_toggle"),
    path("rooms/<int:room_id>/", views.room_detail, name="room_detail"),
    path(
        "rooms/<int:room_id>/settings/",
        views.room_settings_save,
        name="room_settings_save",
    ),
    path("rooms/<int:room_id>/bindings/add/", views.binding_add, name="binding_add"),
    path(
        "rooms/<int:room_id>/bindings/remove/",
        views.binding_remove,
        name="binding_remove",
    ),
    path("rooms/<int:room_id>/bindings/role/", views.binding_role, name="binding_role"),
    path("rooms/<int:room_id>/nicks/", views.room_load_nicks, name="room_load_nicks"),
    path("books/<str:book>/", views.book_page, name="book"),
    path("books/<str:book>/entries/add/", views.entry_add, name="entry_add"),
    path("books/<str:book>/entries/action/", views.entry_action, name="entry_action"),
    path("books/<str:book>/keywords/add/", views.keyword_add, name="keyword_add"),
    path(
        "books/<str:book>/keywords/toggle/",
        views.keyword_toggle,
        name="keyword_toggle",
    ),
    path(
        "books/<str:book>/keywords/delete/",
        views.keyword_delete,
        name="keyword_delete",
    ),
    path("xinhuo/test/", views.xinhuo_test, name="xinhuo_test"),
    path("logs/", views.logs_page, name="logs"),
    path("logs/raw/", views.logs_raw, name="logs_raw"),
    path("logs/clear/", views.logs_clear, name="logs_clear"),
]
