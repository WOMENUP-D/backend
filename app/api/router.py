"""Aggregate router mounted under the versioned API prefix."""

from fastapi import APIRouter

from app.api import (
    admin,
    ai,
    analytics,
    assessments,
    assistant,
    auth,
    integrations,
    mentors,
    news,
    notifications,
    opportunities,
    plans,
    programs,
    users,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(assessments.router)
api_router.include_router(plans.router)
api_router.include_router(programs.router)
api_router.include_router(opportunities.router)
api_router.include_router(mentors.router)
api_router.include_router(news.router)
api_router.include_router(ai.router)
api_router.include_router(assistant.router)
api_router.include_router(notifications.router)
api_router.include_router(integrations.router)
api_router.include_router(admin.router)
api_router.include_router(analytics.router)
