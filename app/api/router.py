"""Aggregate router mounted under the versioned API prefix."""

from fastapi import APIRouter

from app.api import (
    admin,
    ai,
    analytics,
    assessments,
    assistant,
    auth,
    career_paths,
    events,
    integrations,
    learning_paths,
    mentors,
    news,
    notifications,
    opportunities,
    organizations,
    plans,
    portfolio,
    practical_tasks,
    programs,
    results,
    skills,
    users,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(assessments.router)
api_router.include_router(plans.router)
api_router.include_router(programs.router)
api_router.include_router(learning_paths.router)
api_router.include_router(career_paths.router)
api_router.include_router(practical_tasks.router)
api_router.include_router(portfolio.router)
api_router.include_router(skills.router)
api_router.include_router(opportunities.router)
api_router.include_router(events.router)
api_router.include_router(organizations.router)
api_router.include_router(organizations.admin_router)
api_router.include_router(mentors.router)
api_router.include_router(news.router)
api_router.include_router(ai.router)
api_router.include_router(assistant.router)
api_router.include_router(notifications.router)
api_router.include_router(integrations.router)
api_router.include_router(admin.router)
api_router.include_router(results.router)
api_router.include_router(analytics.router)
