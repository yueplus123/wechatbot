from django.urls import path

from apps.gateway_api.views import (
    GatewayChatsSyncPinnedView,
    GatewayChatsView,
    GatewayHeartbeatView,
    GatewayMessagesView,
    GatewayReplyNextView,
    GatewayReplyResultView,
)

urlpatterns = [
    path("messages", GatewayMessagesView.as_view()),
    path("chats", GatewayChatsView.as_view()),
    path("chats/sync-pinned", GatewayChatsSyncPinnedView.as_view()),
    path("replies/next", GatewayReplyNextView.as_view()),
    path("replies/<int:job_id>/result", GatewayReplyResultView.as_view()),
    path("heartbeat", GatewayHeartbeatView.as_view()),
]
